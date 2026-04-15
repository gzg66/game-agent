"""冷启动探索 Demo 入口脚本。

用法：
    # 使用默认配置（demo Unity 游戏）
    python examples/cold_start_demo.py

    # 指定配置文件（切换不同游戏只需更换配置）
    python examples/cold_start_demo.py --config examples/cold_start_game_config.yaml

    # 命令行覆盖部分参数
    python examples/cold_start_demo.py --config examples/cold_start_game_config.yaml \
        --max-steps 100 --max-pages 20 --output outputs/my_cold_start

    # 默认启用视觉大模型；也可显式传入 API Key
    python examples/cold_start_demo.py --llm-api-key "sk-xxxxxx"

切换游戏的方式：
    1. 复制 cold_start_game_config.yaml 并修改 engine_type / package_name / activity_name 等
    2. 运行时指定 --config 为新配置文件即可
    3. 支持的引擎类型: unity3d / cocos_creator / cocos2dx_js / cocos2dx_lua / android_uiautomation
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
from pathlib import Path

import base64
from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poco_ui_automation.cold_start import (  # noqa: E402
    ColdStartExplorer,
    ColdStartReportBuilder,
    GameConfig,
)
from poco_ui_automation.cold_start.config import (  # noqa: E402
    ENGINE_ANDROID_UIAUTOMATION,
    ENGINE_COCOS2DX_JS,
    ENGINE_COCOS2DX_LUA,
    ENGINE_UNITY3D,
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


def load_env_file(env_path: Path) -> None:
    """从简单的 .env 文件加载环境变量，不覆盖已有系统环境变量。"""
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def adb_path() -> str:
    """优先使用 Airtest 自带 adb，回退到系统 adb。"""
    try:
        import airtest

        runtime_adb = (
            Path(airtest.__file__).resolve().parent
            / "core"
            / "android"
            / "static"
            / "adb"
            / "windows"
            / "adb.exe"
        )
        if runtime_adb.exists():
            return str(runtime_adb)
    except Exception:
        pass
    return "adb"


def run_adb(device_serial: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [adb_path(), "-s", device_serial, *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )


def run_adb_global(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [adb_path(), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )


def adb_output(device_serial: str, *args: str) -> str:
    result = run_adb(device_serial, *args)
    return (result.stdout or result.stderr or "").strip()


def adb_global_output(*args: str) -> str:
    result = run_adb_global(*args)
    return (result.stdout or result.stderr or "").strip()


def device_serial_from_uri(device_uri: str) -> str:
    if ":///" in device_uri:
        return device_uri.split(":///", 1)[1]
    if "://" in device_uri:
        return device_uri.split("://", 1)[1]
    return device_uri


def is_adb_device_ready(device_serial: str, devices_output: str) -> bool:
    return f"{device_serial}\tdevice" in devices_output


def try_auto_connect_device(device_serial: str) -> bool:
    """对本地 adb tcp 设备执行一次 adb connect，降低模拟器首次未注册的失败率。"""
    if not device_serial or ":" not in device_serial:
        return False

    host, _, port_text = device_serial.rpartition(":")
    if not host or not port_text.isdigit():
        return False

    connect_output = adb_global_output("connect", device_serial)
    if connect_output:
        print(f"[冷启动][检测] 自动执行 adb connect {device_serial}: {connect_output}")

    normalized = connect_output.lower()
    return "connected to" in normalized or "already connected to" in normalized


def resolve_launch_activity(device_serial: str, package_name: str) -> str:
    output = adb_output(
        device_serial,
        "shell",
        "cmd",
        "package",
        "resolve-activity",
        "--brief",
        package_name,
    )
    for line in reversed(output.splitlines()):
        candidate = line.strip()
        if "/" in candidate and package_name in candidate:
            return candidate
    return ""


def detect_engine_from_runtime(config: GameConfig) -> tuple[str, int, str]:
    listening_ports = adb_output(config.device_serial, "shell", "ss", "-ltn")
    poco_service_installed = bool(
        adb_output(config.device_serial, "shell", "pm", "path", "com.netease.open.pocoservice")
    )

    if "5003" in listening_ports:
        return ENGINE_COCOS2DX_JS, 5003, "detected_listen_port_5003"
    if "5001" in listening_ports:
        return ENGINE_UNITY3D, 5001, "detected_listen_port_5001"
    if "15004" in listening_ports:
        return ENGINE_COCOS2DX_LUA, 15004, "detected_listen_port_15004"
    if poco_service_installed and config.engine_type == ENGINE_ANDROID_UIAUTOMATION:
        return ENGINE_COCOS2DX_JS, 5003, "detected_pocoservice_package"
    return config.engine_type, config.effective_poco_port(), "keep_config"


def detect_runtime_config(config: GameConfig) -> None:
    if not config.device_serial and config.device_uri:
        config.device_serial = device_serial_from_uri(config.device_uri)

    devices_output = adb_global_output("devices")
    if not is_adb_device_ready(config.device_serial, devices_output):
        try_auto_connect_device(config.device_serial)
        devices_output = adb_global_output("devices")

    if not is_adb_device_ready(config.device_serial, devices_output):
        raise RuntimeError(
            f"设备未连接或状态异常: {config.device_serial}。请先确认 adb devices 可见该设备。"
        )

    package_path = adb_output(config.device_serial, "shell", "pm", "path", config.package_name)
    if not package_path.startswith("package:"):
        raise RuntimeError(f"设备上未安装目标游戏包: {config.package_name}")

    resolved_activity = resolve_launch_activity(config.device_serial, config.package_name)
    if resolved_activity:
        if config.activity_name != resolved_activity:
            print(f"[冷启动][检测] 启动 Activity 已修正: {config.activity_name} -> {resolved_activity}")
        config.activity_name = resolved_activity

    detected_engine, detected_port, detect_reason = detect_engine_from_runtime(config)
    if detected_engine != config.engine_type:
        print(f"[冷启动][检测] 引擎类型已修正: {config.engine_type} -> {detected_engine}")
        config.engine_type = detected_engine
    if detected_port > 0 and config.poco_port != detected_port:
        print(f"[冷启动][检测] Poco 端口已修正: {config.poco_port} -> {detected_port}")
        config.poco_port = detected_port

    pid = adb_output(config.device_serial, "shell", "pidof", config.package_name) or "未运行"
    focus = adb_output(config.device_serial, "shell", "dumpsys", "window")
    focus_lines = [
        line.strip()
        for line in focus.splitlines()
        if "mCurrentFocus" in line or "mFocusedApp" in line
    ]
    print("[冷启动][检测] 设备与游戏配置探测完成:")
    print(f"  设备序列号: {config.device_serial}")
    print(f"  包名: {config.package_name}")
    print(f"  Activity: {config.activity_name or '<unknown>'}")
    print(f"  引擎: {config.engine_type}")
    print(f"  Poco: {config.poco_host}:{config.effective_poco_port()} ({detect_reason})")
    print(f"  当前 PID: {pid}")
    if focus_lines:
        print("  当前前台窗口:")
        for line in focus_lines:
            print(f"    {line}")


def local_port_open(host: str, port: int, timeout_s: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def print_rpc_diagnostics(config: GameConfig) -> None:
    """在 Poco RPC 建连失败时输出关键诊断信息。"""
    print("[冷启动] ========== RPC 诊断 ==========")
    print(f"[冷启动] 目标包名: {config.package_name}")
    print(f"[冷启动] 目标 Activity: {config.activity_name}")
    print(f"[冷启动] 目标设备: {config.device_serial}")
    print(f"[冷启动] 目标 Poco: {config.poco_host}:{config.effective_poco_port()}")
    print(f"[冷启动] 本地端口连通: {local_port_open(config.poco_host, config.effective_poco_port())}")

    pid = adb_output(config.device_serial, "shell", "pidof", config.package_name)
    launcher = adb_output(
        config.device_serial,
        "shell",
        "cmd",
        "package",
        "resolve-activity",
        "--brief",
        config.package_name,
    )
    focus = adb_output(config.device_serial, "shell", "dumpsys", "window")
    port_listen = adb_output(config.device_serial, "shell", "ss", "-ltn")

    focus_lines = [
        line.strip()
        for line in focus.splitlines()
        if "mCurrentFocus" in line or "mFocusedApp" in line
    ]
    port_lines = [
        line.strip()
        for line in port_listen.splitlines()
        if any(token in line for token in ("5003", "5001", "15004"))
    ]

    print(f"[冷启动] 进程 PID: {pid or '未找到'}")
    print("[冷启动] Launcher Activity:")
    print(launcher or "  <empty>")
    print("[冷启动] 前台窗口:")
    if focus_lines:
        for line in focus_lines:
            print(f"  {line}")
    else:
        print("  <empty>")
    print("[冷启动] 设备监听端口(筛选 5003/5001/15004):")
    if port_lines:
        for line in port_lines:
            print(f"  {line}")
    else:
        print("  <none>")
    print()


def main() -> None:
    load_env_file(ROOT / ".env")
    default_config = GameConfig()
    parser = argparse.ArgumentParser(
        description="冷启动探索 Demo：低风险、结构化地建立初始世界模型"
    )
    parser.add_argument(
        "--config",
        default=str(ROOT / "examples" / "cold_start_game_config.yaml"),
        help="游戏配置文件路径（YAML 或 JSON）",
    )
    # 允许命令行覆盖关键参数
    parser.add_argument("--device-uri", default=argparse.SUPPRESS, help=f"设备 URI（默认: {default_config.device_uri}）")
    parser.add_argument("--device-serial", default=argparse.SUPPRESS, help=f"设备序列号（默认: {default_config.device_serial}）")
    parser.add_argument("--host", default=argparse.SUPPRESS, help=f"Poco 主机地址（默认: {default_config.poco_host}）")
    parser.add_argument("--port", type=int, default=argparse.SUPPRESS, help=f"Poco 端口（默认: {default_config.effective_poco_port()}）")
    parser.add_argument("--package", default=argparse.SUPPRESS, help=f"游戏包名（默认: {default_config.package_name}）")
    parser.add_argument("--activity", default=argparse.SUPPRESS, help=f"游戏 Activity（默认: {default_config.activity_name}）")
    parser.add_argument("--engine", default=argparse.SUPPRESS, help=f"引擎类型（默认: {default_config.engine_type}）")
    parser.add_argument("--max-steps", type=int, default=argparse.SUPPRESS, help=f"最大探索步数（默认: {default_config.max_steps}）")
    parser.add_argument("--max-pages", type=int, default=argparse.SUPPRESS, help=f"最大页面数（默认: {default_config.max_pages}）")
    parser.add_argument("--max-actions-per-page", type=int, default=argparse.SUPPRESS, help=f"每页最大动作数（默认: {default_config.max_actions_per_page}）")
    parser.add_argument("--boot-wait", type=float, default=argparse.SUPPRESS, help=f"启动等待秒数（默认: {default_config.boot_wait_s}）")
    parser.add_argument("--action-wait", type=float, default=argparse.SUPPRESS, help=f"动作等待秒数（默认: {default_config.action_wait_s}）")
    parser.add_argument("--output", default=argparse.SUPPRESS, help=f"输出目录（默认: {default_config.output_dir}）")
    
    # 【新增】视觉大模型相关参数
    parser.add_argument(
        "--enable-vision",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否启用视觉大模型进行 SoM 语义分析（默认开启，可用 --no-enable-vision 关闭）",
    )
    parser.add_argument(
        "--llm-api-key",
        default=os.environ.get("LLM_API_KEY"),
        help="大模型 API Key（默认读取环境变量或项目根目录 .env 中的 LLM_API_KEY）",
    )
    parser.add_argument(
        "--vision-mode",
        choices=["rule_first", "vision_first"],
        default=argparse.SUPPRESS,
        help=f"视觉模式（默认: {default_config.vision_mode}）",
    )
    parser.add_argument(
        "--vision-max-candidates",
        type=int,
        default=argparse.SUPPRESS,
        help=f"送入视觉模型的最大候选数（默认: {default_config.vision_max_candidates}）",
    )
    parser.add_argument(
        "--vision-min-confidence",
        type=float,
        default=argparse.SUPPRESS,
        help=f"执行动作的最低视觉置信度（默认: {default_config.vision_min_confidence}）",
    )
    parser.add_argument(
        "--vision-allow-low-confidence",
        action="store_true",
        default=argparse.SUPPRESS,
        help="允许执行低于阈值的低置信动作",
    )
    parser.add_argument(
        "--vision-max-calls-per-page",
        type=int,
        default=argparse.SUPPRESS,
        help=f"每页最多同步视觉调用次数（默认: {default_config.vision_max_calls_per_page}）",
    )

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
    if hasattr(args, "device_uri"):
        config.device_uri = args.device_uri
    if hasattr(args, "device_serial"):
        config.device_serial = args.device_serial
    if hasattr(args, "host"):
        config.poco_host = args.host
    if hasattr(args, "port"):
        config.poco_port = args.port
    if hasattr(args, "package"):
        config.package_name = args.package
    if hasattr(args, "activity"):
        config.activity_name = args.activity
    if hasattr(args, "engine"):
        config.engine_type = args.engine
    if hasattr(args, "max_steps"):
        config.max_steps = args.max_steps
    if hasattr(args, "max_pages"):
        config.max_pages = args.max_pages
    if hasattr(args, "max_actions_per_page"):
        config.max_actions_per_page = args.max_actions_per_page
    if hasattr(args, "boot_wait"):
        config.boot_wait_s = args.boot_wait
    if hasattr(args, "action_wait"):
        config.action_wait_s = args.action_wait
    if hasattr(args, "output"):
        config.output_dir = args.output
    if hasattr(args, "vision_mode"):
        config.vision_mode = args.vision_mode
    if hasattr(args, "vision_max_candidates"):
        config.vision_max_candidates = args.vision_max_candidates
    if hasattr(args, "vision_min_confidence"):
        config.vision_min_confidence = args.vision_min_confidence
    if hasattr(args, "vision_allow_low_confidence"):
        config.vision_allow_low_confidence = args.vision_allow_low_confidence
    if hasattr(args, "vision_max_calls_per_page"):
        config.vision_max_calls_per_page = args.vision_max_calls_per_page

    detect_runtime_config(config)
    if args.enable_vision and not hasattr(args, "vision_mode"):
        config.vision_mode = "vision_first"
    config.validate()

    # 打印配置摘要
    print(f"[冷启动] 项目: {config.project_name}")
    print(f"[冷启动] 引擎: {config.engine_type}")
    print(f"[冷启动] 包名: {config.package_name}")
    print(f"[冷启动] 设备: {config.device_serial}")
    print(f"[冷启动] Poco: {config.poco_host}:{config.effective_poco_port()}")
    print(f"[冷启动] 最大步数: {config.max_steps}, 最大页面: {config.max_pages}")
    print(f"[冷启动] 视觉模式: {config.vision_mode}")
    print(f"[冷启动] 输出目录: {config.output_dir}")
    
    # 【新增】初始化 LLM Client
    llm_client = None
    if args.enable_vision:
        api_key = args.llm_api_key or os.environ.get("LLM_API_KEY")
        if not api_key:
            raise ValueError("已默认启用视觉大模型，但未找到 API Key。请在项目根目录 .env 中设置 LLM_API_KEY，或通过 --llm-api-key 传入。")
        print(f"[冷启动] 已启用视觉大模型增强，API_KEY: {api_key[:5]}***")
        print(
            "[冷启动] 视觉参数: "
            f"max_candidates={config.vision_max_candidates}, "
            f"min_confidence={config.vision_min_confidence}, "
            f"allow_low_confidence={config.vision_allow_low_confidence}"
        )
        llm_client = VisionLLMClient(api_key=api_key)
    else:
        print("[冷启动] 未启用视觉大模型，仅使用纯规则快车道进行探索。")
    print()

    # 执行冷启动探索
    print("[冷启动] ========== 开始冷启动探索 ==========")
    # 【修改】将 llm_client 注入到 Explorer 中
    explorer = ColdStartExplorer(config, llm_client=llm_client)
    result = explorer.run()

    stop_reason_lower = result.stop_reason.lower()
    if "rpc" in stop_reason_lower or "connection closed" in stop_reason_lower:
        print_rpc_diagnostics(config)

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
        print(f"[冷启动] 警告: 探索过程中发生 {len(result.crashes)} 次崩溃！")

    if result.status != "completed":
        sys.exit(1)


if __name__ == "__main__":
    main()