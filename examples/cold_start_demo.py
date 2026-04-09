"""冷启动探索 Demo 入口脚本。

用法：
    # 使用默认配置（demo Unity 游戏）
    python examples/cold_start_demo.py

    # 指定配置文件（切换不同游戏只需更换配置）
    python examples/cold_start_demo.py --config examples/cold_start_game_config.yaml

    # 命令行覆盖部分参数
    python examples/cold_start_demo.py --config examples/cold_start_game_config.yaml \
        --max-steps 100 --max-pages 20 --output outputs/my_cold_start

    # 启用视觉大模型进行 SoM 语义分析 (新增)
    python examples/cold_start_demo.py --enable-vision --llm-api-key "sk-xxxxxx"

切换游戏的方式：
    1. 复制 cold_start_game_config.yaml 并修改 engine_type / package_name / activity_name 等
    2. 运行时指定 --config 为新配置文件即可
    3. 支持的引擎类型: unity3d / cocos_creator / cocos2dx_js / cocos2dx_lua
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import base64
from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poco_ui_automation.cold_start import (
    ColdStartExplorer,
    ColdStartReportBuilder,
    GameConfig,
)


# =======================================================================
# 【修改】使用 google-genai SDK 接入 gemini-3-flash-preview
# =======================================================================
class VisionLLMClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        # 根据官方 SDK，初始化 Client
        self.client = genai.Client(
            vertexai=True,
            api_key=self.api_key,
            http_options=types.HttpOptions(api_version='v1')
        )

    def chat(self, prompt: str, image_base64: str) -> str:
        print("[视觉大模型] 正在后台异步分析页面未知节点...")
        
        try:
            # 将 base64 字符串解码回图片原始字节流
            image_bytes = base64.b64decode(image_base64)
            
            # 使用截图中的方式构造请求
            response = self.client.models.generate_content(
                model='gemini-3-flash-preview',
                contents=[
                    types.Part.from_bytes(
                        data=image_bytes,
                        mime_type='image/jpeg',
                    ),
                    prompt,
                ],
                # 强迫模型严格按照 Prompt 要求的 JSON 格式输出，防止包含 Markdown 代码块标记
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            
            print("[视觉大模型] 分析完成！")
            return response.text
            
        except Exception as e:
            print(f"[视觉大模型] 请求失败: {e}")
            return "{}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="冷启动探索 Demo：低风险、结构化地建立初始世界模型"
    )
    parser.add_argument(
        "--config",
        default=str(ROOT / "examples" / "cold_start_game_config.yaml"),
        help="游戏配置文件路径（YAML 或 JSON）",
    )
    # 允许命令行覆盖关键参数
    parser.add_argument("--device-uri", default=None, help="设备 URI")
    parser.add_argument("--device-serial", default=None, help="设备序列号")
    parser.add_argument("--host", default=None, help="Poco 主机地址")
    parser.add_argument("--port", type=int, default=None, help="Poco 端口")
    parser.add_argument("--package", default=None, help="游戏包名")
    parser.add_argument("--activity", default=None, help="游戏 Activity")
    parser.add_argument("--engine", default=None, help="引擎类型")
    parser.add_argument("--max-steps", type=int, default=None, help="最大探索步数")
    parser.add_argument("--max-pages", type=int, default=None, help="最大页面数")
    parser.add_argument("--max-actions-per-page", type=int, default=None, help="每页最大动作数")
    parser.add_argument("--boot-wait", type=float, default=None, help="启动等待秒数")
    parser.add_argument("--action-wait", type=float, default=None, help="动作等待秒数")
    parser.add_argument("--output", default=None, help="输出目录")
    
    # 【新增】视觉大模型相关参数
    parser.add_argument("--enable-vision", action="store_true", help="是否启用视觉大模型进行 SoM 语义分析")
    parser.add_argument("--llm-api-key", default=None, help="大模型 API Key（可选，也可通过环境变量 LLM_API_KEY 配置）")

    args = parser.parse_args()

    # 加载配置
    config_path = Path(args.config)
    if config_path.exists():
        print(f"[冷启动] 加载配置: {config_path}")
        config = GameConfig.load(config_path)
    else:
        print(f"[冷启动] 配置文件不存在，使用默认配置: {config_path}")
        config = GameConfig()

    # 命令行参数覆盖
    if args.device_uri is not None:
        config.device_uri = args.device_uri
    if args.device_serial is not None:
        config.device_serial = args.device_serial
    if args.host is not None:
        config.poco_host = args.host
    if args.port is not None:
        config.poco_port = args.port
    if args.package is not None:
        config.package_name = args.package
    if args.activity is not None:
        config.activity_name = args.activity
    if args.engine is not None:
        config.engine_type = args.engine
    if args.max_steps is not None:
        config.max_steps = args.max_steps
    if args.max_pages is not None:
        config.max_pages = args.max_pages
    if args.max_actions_per_page is not None:
        config.max_actions_per_page = args.max_actions_per_page
    if args.boot_wait is not None:
        config.boot_wait_s = args.boot_wait
    if args.action_wait is not None:
        config.action_wait_s = args.action_wait
    if args.output is not None:
        config.output_dir = args.output

    config.validate()

    # 打印配置摘要
    print(f"[冷启动] 项目: {config.project_name}")
    print(f"[冷启动] 引擎: {config.engine_type}")
    print(f"[冷启动] 包名: {config.package_name}")
    print(f"[冷启动] 设备: {config.device_serial}")
    print(f"[冷启动] Poco: {config.poco_host}:{config.effective_poco_port()}")
    print(f"[冷启动] 最大步数: {config.max_steps}, 最大页面: {config.max_pages}")
    print(f"[冷启动] 输出目录: {config.output_dir}")
    
    # 【新增】初始化 LLM Client
    llm_client = None
    if args.enable_vision:
        api_key = args.llm_api_key or os.environ.get("LLM_API_KEY", "dummy_key")
        print(f"[冷启动] 👁️ 已启用视觉大模型增强，API_KEY: {api_key[:5]}***")
        llm_client = VisionLLMClient(api_key=api_key)
    else:
        print("[冷启动] ℹ️ 未启用视觉大模型，仅使用纯规则快车道进行探索。")
    print()

    # 执行冷启动探索
    print("[冷启动] ========== 开始冷启动探索 ==========")
    # 【修改】将 llm_client 注入到 Explorer 中
    explorer = ColdStartExplorer(config, llm_client=llm_client)
    result = explorer.run()

    # 生成报告
    print("[冷启动] ========== 生成报告 ==========")
    report_builder = ColdStartReportBuilder(Path(config.output_dir))
    report_paths = report_builder.build(result)

    # 打印结果摘要
    print()
    print("[冷启动] ========== 探索完成 ==========")
    print(f"  状态: {result.status}")
    print(f"  停止原因: {result.stop_reason}")
    print(f"  总步数: {result.total_steps}")
    print(f"  发现页面: {result.new_pages_found}")
    print(f"  状态图节点: {result.graph.page_count}")
    print(f"  状态图边: {result.graph.edge_count}")
    print(f"  执行动作: {len(result.executions)}")
    print(f"  崩溃次数: {len(result.crashes)}")
    print()
    print("[冷启动] 输出文件:")
    print(f"  状态图:   {config.output_dir}/state_graph.json")
    print(f"  Mermaid:  {config.output_dir}/state_graph.mmd")
    print(f"  执行记录: {config.output_dir}/executions.json")
    print(f"  探索摘要: {config.output_dir}/exploration_summary.json")
    print(f"  报告 JSON: {report_paths['json']}")
    print(f"  报告 MD:   {report_paths['markdown']}")
    print()

    if result.crashes:
        print(f"[冷启动] ⚠ 探索过程中发生 {len(result.crashes)} 次崩溃！")

    if result.status != "completed":
        sys.exit(1)


if __name__ == "__main__":
    main()