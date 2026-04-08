"""冷启动探索 Demo 入口脚本。

用法：
    # 使用默认配置（demo Unity 游戏）
    python examples/cold_start_demo.py

    # 指定配置文件（切换不同游戏只需更换配置）
    python examples/cold_start_demo.py --config examples/cold_start_game_config.yaml

    # 命令行覆盖部分参数
    python examples/cold_start_demo.py --config examples/cold_start_game_config.yaml \
        --max-steps 100 --max-pages 20 --output outputs/my_cold_start

切换游戏的方式：
    1. 复制 cold_start_game_config.yaml 并修改 engine_type / package_name / activity_name 等
    2. 运行时指定 --config 为新配置文件即可
    3. 支持的引擎类型: unity3d / cocos_creator / cocos2dx_js / cocos2dx_lua
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poco_ui_automation.cold_start import (
    ColdStartExplorer,
    ColdStartReportBuilder,
    GameConfig,
)


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
    print()

    # 执行冷启动探索
    print("[冷启动] ========== 开始冷启动探索 ==========")
    explorer = ColdStartExplorer(config)
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
