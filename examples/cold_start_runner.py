"""冷启动探索 runner（支持 MockDriver 本地调试 + 真机运行）。"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poco_ui_automation import (  # noqa: E402
    AirtestPocoDriver,
    EngineType,
    ProjectProfile,
)
from poco_ui_automation.cold_start import ColdStartConfig, ColdStartExplorer  # noqa: E402
from poco_ui_automation.drivers import MockDriver, MockGraphState  # noqa: E402
from poco_ui_automation.models import UiNode  # noqa: E402


# ---------------------------------------------------------------------------
# 引擎类型常量（与 integration.EngineType 对齐，但这里用纯字符串以降低耦合）
# ---------------------------------------------------------------------------
ENGINE_UNITY3D = "unity3d"
ENGINE_COCOS_CREATOR = "cocos_creator"
ENGINE_COCOS2DX_JS = "cocos2dx_js"
ENGINE_COCOS2DX_LUA = "cocos2dx_lua"
ENGINE_ANDROID_UIAUTOMATION = "android_uiautomation"

# 引擎 -> 默认 Poco 端口映射
_DEFAULT_PORTS: dict[str, int] = {
    ENGINE_UNITY3D: 5001,
    ENGINE_COCOS_CREATOR: 5003,
    ENGINE_COCOS2DX_JS: 5003,
    ENGINE_COCOS2DX_LUA: 15004,
    ENGINE_ANDROID_UIAUTOMATION: 0,
}


@dataclass
class GameConfig:
    """冷启动探索的完整游戏配置。

    设计目标：只改本配置即可切换不同游戏 / 不同引擎 / 不同设备。
    """

    # ---- 游戏标识 ----
    project_name: str = "星途天城(poco)"
    engine_type: str = ENGINE_COCOS2DX_JS
    package_name: str = "com.xttc.release.poco"
    activity_name: str = "com.xttc.release.poco/org.cocos2dx.javascript.AppActivity"

    # ---- 设备 ----
    device_uri: str = "Android:///127.0.0.1:16384"
    device_serial: str = "127.0.0.1:16384"

    # ---- Poco 连接 ----
    poco_host: str = "127.0.0.1"
    poco_port: int = 5003


def _airtest_adb_path() -> str:
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
    return str(runtime_adb)


def device_serial_from_uri(device_uri: str, explicit_serial: str = "") -> str:
    if explicit_serial.strip():
        return explicit_serial.strip()
    if ":///" in device_uri:
        return device_uri.split(":///", 1)[1].strip()
    return device_uri.strip()


def run_adb(device_serial: str, *args: str) -> subprocess.CompletedProcess[str]:
    command = [_airtest_adb_path(), "-s", device_serial, *args]
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )


def app_pid(device_serial: str, package_name: str) -> str | None:
    result = run_adb(device_serial, "shell", "pidof", package_name)
    pid = (result.stdout or "").strip()
    return pid or None


def app_is_running(device_serial: str, package_name: str) -> bool:
    return app_pid(device_serial, package_name) is not None


def current_focus_activity(device_serial: str) -> str:
    result = run_adb(device_serial, "shell", "dumpsys", "window", "windows")
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    for line in output.splitlines():
        stripped = line.strip()
        if "mCurrentFocus" in stripped or "mFocusedApp" in stripped:
            return stripped
    return ""


def probe_tcp_port(host: str, port: int, timeout_seconds: float = 1.0) -> tuple[bool, str]:
    if port <= 0:
        return False, "端口未配置"
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True, ""
    except OSError as exc:
        return False, str(exc)


def wait_for_tcp_port(
    host: str,
    port: int,
    timeout_seconds: float,
    interval_seconds: float = 0.5,
) -> tuple[bool, str]:
    deadline = time.time() + max(timeout_seconds, 0.0)
    last_error = ""
    while True:
        ok, error = probe_tcp_port(host, port)
        if ok:
            return True, ""
        last_error = error
        if time.time() >= deadline:
            return False, last_error
        time.sleep(interval_seconds)


def build_runtime_diagnostics(
    game_config: GameConfig,
    package_name: str,
    poco_port: int,
) -> dict[str, str | bool]:
    device_serial = device_serial_from_uri(
        game_config.device_uri,
        explicit_serial=game_config.device_serial,
    )
    pid = app_pid(device_serial, package_name)
    port_open, port_error = probe_tcp_port(game_config.poco_host, poco_port)
    return {
        "device_serial": device_serial,
        "package_name": package_name,
        "activity_name": game_config.activity_name,
        "app_running": pid is not None,
        "app_pid": pid or "",
        "focused_activity": current_focus_activity(device_serial),
        "poco_host": game_config.poco_host,
        "poco_port": str(poco_port),
        "poco_port_open": port_open,
        "poco_port_error": port_error,
    }


def format_runtime_diagnostics(diagnostics: dict[str, str | bool]) -> str:
    return ", ".join(f"{key}={value}" for key, value in diagnostics.items())


def start_game_app(device_serial: str, package_name: str, activity_name: str) -> None:
    if activity_name.strip():
        result = run_adb(device_serial, "shell", "am", "start", "-n", activity_name)
        if result.returncode == 0:
            return
        activity_error = result.stderr.strip() or result.stdout.strip()
    else:
        activity_error = "未配置 activity_name"

    result = run_adb(
        device_serial,
        "shell",
        "monkey",
        "-p",
        package_name,
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
    )
    if result.returncode == 0:
        return

    monkey_error = result.stderr.strip() or result.stdout.strip()
    raise RuntimeError(
        "自动启动游戏失败。"
        f" activity 启动结果: {activity_error or 'unknown'};"
        f" monkey 启动结果: {monkey_error or 'unknown'}"
    )


def ensure_game_ready(game_config: GameConfig, startup_wait_seconds: float) -> None:
    device_serial = device_serial_from_uri(
        game_config.device_uri,
        explicit_serial=game_config.device_serial,
    )
    if app_is_running(device_serial, game_config.package_name):
        return

    start_game_app(device_serial, game_config.package_name, game_config.activity_name)
    if startup_wait_seconds > 0:
        time.sleep(startup_wait_seconds)


def ensure_poco_port_ready(
    game_config: GameConfig,
    package_name: str,
    poco_port: int,
    wait_seconds: float,
) -> None:
    port_ready, port_error = wait_for_tcp_port(
        game_config.poco_host,
        poco_port,
        timeout_seconds=max(wait_seconds, 1.0),
    )
    if port_ready:
        return

    diagnostics = build_runtime_diagnostics(game_config, package_name, poco_port)
    raise SystemExit(
        "Poco 连接前检查失败：游戏进程已启动，但目标端口未就绪。"
        f" diagnostics: {format_runtime_diagnostics(diagnostics)}."
        f" 最近一次端口探测错误: {port_error or 'unknown'}."
        " 这通常表示游戏未初始化 Poco SDK、端口配置不匹配，或端口转发尚未建立。"
    )


def _node(name: str, text: str = "", clickable: bool = True) -> UiNode:
    return UiNode(name=name, text=text, clickable=clickable)


def build_mock_scenario() -> tuple[dict[str, MockGraphState], str]:
    """模拟冷启动场景：login → popup → lobby → battle_prep → battle → result → lobby。"""
    states: dict[str, MockGraphState] = {
        "login": MockGraphState(
            name="login",
            nodes=[
                _node("root", clickable=False),
                _node("title", "星途天城", clickable=False),
                _node("btn_start", "开始游戏"),
                _node("btn_guest", "游客登录"),
            ],
            transitions={
                "btn_start": "popup",
                "btn_guest": "popup",
                "开始游戏": "popup",
                "游客登录": "popup",
            },
        ),
        "popup": MockGraphState(
            name="popup",
            nodes=[
                _node("root", clickable=False),
                _node("dialog_bg", "公告", clickable=False),
                _node("btn_close", "关闭"),
                _node("btn_confirm", "确认"),
            ],
            transitions={
                "btn_close": "lobby",
                "btn_confirm": "lobby",
                "关闭": "lobby",
                "确认": "lobby",
            },
        ),
        "lobby": MockGraphState(
            name="lobby",
            nodes=[
                _node("root", clickable=False),
                _node("title", "大厅", clickable=False),
                _node("btn_adventure", "冒险"),
                _node("btn_bag", "背包"),
                _node("btn_task", "任务"),
                _node("btn_mail", "邮件"),
                _node("btn_shop", "商城"),
                _node("btn_recharge", "充值"),
            ],
            transitions={
                "btn_adventure": "battle_prep",
                "冒险": "battle_prep",
                "btn_bag": "reward",
                "背包": "reward",
                "btn_task": "guide",
                "任务": "guide",
                "btn_shop": "shop",
                "商城": "shop",
            },
        ),
        "battle_prep": MockGraphState(
            name="battle_prep",
            nodes=[
                _node("root", clickable=False),
                _node("title", "出战准备", clickable=False),
                _node("btn_fight", "挑战"),
                _node("btn_auto", "自动"),
                _node("btn_back", "返回"),
            ],
            transitions={
                "btn_fight": "battle",
                "挑战": "battle",
                "btn_back": "lobby",
                "返回": "lobby",
            },
        ),
        "battle": MockGraphState(
            name="battle",
            nodes=[
                _node("root", clickable=False),
                _node("title", "战斗中", clickable=False),
                _node("btn_auto", "自动"),
                _node("btn_pause", "暂停"),
                _node("btn_skip", "跳过"),
            ],
            transitions={
                "btn_skip": "battle_result",
                "跳过": "battle_result",
                "btn_auto": "battle",
                "自动": "battle",
            },
        ),
        "battle_result": MockGraphState(
            name="battle_result",
            nodes=[
                _node("root", clickable=False),
                _node("title", "胜利", clickable=False),
                _node("label_gold", "金币 +500", clickable=False),
                _node("btn_claim", "领取"),
                _node("btn_again", "再次挑战"),
            ],
            transitions={
                "btn_claim": "lobby",
                "领取": "lobby",
                "btn_again": "battle",
                "再次挑战": "battle",
            },
        ),
        "reward": MockGraphState(
            name="reward",
            nodes=[
                _node("root", clickable=False),
                _node("title", "背包", clickable=False),
                _node("btn_close", "关闭"),
            ],
            transitions={
                "btn_close": "lobby",
                "关闭": "lobby",
            },
        ),
        "guide": MockGraphState(
            name="guide",
            nodes=[
                _node("root", clickable=False),
                _node("title", "新手引导", clickable=False),
                _node("btn_next", "下一步"),
                _node("btn_skip", "跳过"),
            ],
            transitions={
                "btn_next": "lobby",
                "下一步": "lobby",
                "btn_skip": "lobby",
                "跳过": "lobby",
            },
        ),
        "shop": MockGraphState(
            name="shop",
            nodes=[
                _node("root", clickable=False),
                _node("title", "商城", clickable=False),
                _node("btn_buy", "购买"),
                _node("btn_close", "关闭"),
            ],
            transitions={
                "btn_close": "lobby",
                "关闭": "lobby",
            },
        ),
    }
    return states, "login"


def build_mock_profile() -> ProjectProfile:
    return ProjectProfile(
        project_name="mock_cold_start",
        engine_type=EngineType.UNITY3D,
        package_name="com.mock.game",
        ui_root_candidates=["root"],
        critical_pages=["login", "lobby", "battle"],
        dangerous_actions=["充值", "支付", "购买"],
        page_signatures={
            "login": ["开始游戏", "游客登录"],
            "lobby": ["冒险", "背包", "任务", "邮件"],
            "battle": ["自动", "暂停", "跳过"],
        },
    )


def build_device_profile(game_config: GameConfig) -> ProjectProfile:
    return ProjectProfile(
        project_name=game_config.project_name,
        engine_type=_to_engine_type(game_config.engine_type),
        package_name=game_config.package_name,
        ui_root_candidates=["root", "Canvas"],
    )


def _to_engine_type(engine_type: str) -> EngineType:
    supported = {
        ENGINE_UNITY3D: EngineType.UNITY3D,
        ENGINE_COCOS_CREATOR: EngineType.COCOS_CREATOR,
        ENGINE_COCOS2DX_JS: EngineType.COCOS2DX_JS,
        ENGINE_COCOS2DX_LUA: EngineType.COCOS2DX_LUA,
    }
    if engine_type == ENGINE_ANDROID_UIAUTOMATION:
        raise ValueError(
            "当前 cold_start_runner 的 ProjectProfile.engine_type 尚不支持 "
            "'android_uiautomation'。如需使用该模式，请先扩展 integration.EngineType。"
        )
    try:
        return supported[engine_type]
    except KeyError as exc:
        raise ValueError(f"不支持的 engine_type: {engine_type}") from exc


def main() -> None:
    default_game_config = GameConfig()
    parser = argparse.ArgumentParser(description="冷启动探索 runner")
    parser.add_argument("--mode", choices=["mock", "device"], default="device")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--project-name", default=default_game_config.project_name)
    parser.add_argument(
        "--engine-type",
        choices=list(_DEFAULT_PORTS.keys()),
        default=default_game_config.engine_type,
    )
    parser.add_argument("--package", default=default_game_config.package_name)
    parser.add_argument("--activity", default=default_game_config.activity_name)
    parser.add_argument("--device-uri", default=default_game_config.device_uri)
    parser.add_argument("--device-serial", default=default_game_config.device_serial)
    parser.add_argument("--host", default=default_game_config.poco_host)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--output", default=str(ROOT / "outputs" / "cold_start"))
    parser.add_argument("--max-steps", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--max-actions-per-page", type=int, default=8)
    parser.add_argument("--goal", default="")
    parser.add_argument("--startup-wait", type=float, default=8.0)
    parser.add_argument("--action-wait", type=float, default=2.0)
    parser.add_argument(
        "--consecutive-no-new-page-limit",
        type=int,
        default=10,
    )
    args = parser.parse_args()
    game_config = GameConfig(
        project_name=args.project_name,
        engine_type=args.engine_type,
        package_name=args.package,
        activity_name=args.activity,
        device_uri=args.device_uri,
        device_serial=args.device_serial,
        poco_host=args.host,
        poco_port=(
            args.port
            if args.port is not None
            else _DEFAULT_PORTS.get(args.engine_type, default_game_config.poco_port)
        ),
    )

    config = ColdStartConfig(
        max_steps=args.max_steps,
        max_pages=args.max_pages,
        max_actions_per_page=args.max_actions_per_page,
        consecutive_no_new_page_limit=args.consecutive_no_new_page_limit,
        action_wait_seconds=args.action_wait if args.mode == "device" else 0.0,
        startup_wait_seconds=args.startup_wait if args.mode == "device" else 0.0,
        goal=args.goal,
    )

    if args.mode == "mock":
        states, start = build_mock_scenario()
        driver = MockDriver(states, start)
        profile = build_mock_profile()
    else:
        if args.profile:
            profile = ProjectProfile.load(args.profile)
            poco_port = (
                args.port
                if args.port is not None
                else _DEFAULT_PORTS.get(profile.engine_type.value, game_config.poco_port)
            )
        else:
            profile = build_device_profile(game_config)
            poco_port = game_config.poco_port
        ensure_game_ready(game_config, startup_wait_seconds=config.startup_wait_seconds)
        ensure_poco_port_ready(
            game_config,
            package_name=profile.package_name,
            poco_port=poco_port,
            wait_seconds=max(config.startup_wait_seconds, 2.0),
        )
        try:
            driver = AirtestPocoDriver(
                engine_type=profile.engine_type,
                device_uri=game_config.device_uri,
                package_name=profile.package_name,
                unity_addr=(game_config.poco_host, poco_port),
                cocos_addr=(game_config.poco_host, poco_port),
                std_port=poco_port,
            )
        except Exception as exc:
            diagnostics = build_runtime_diagnostics(game_config, profile.package_name, poco_port)
            raise SystemExit(
                "Poco 连接失败，请检查游戏是否已正确集成并启动 Poco 服务。"
                f" engine_type={profile.engine_type.value},"
                f" device_uri={game_config.device_uri},"
                f" host={game_config.poco_host},"
                f" port={poco_port},"
                f" package={profile.package_name},"
                f" activity={game_config.activity_name}."
                f" diagnostics: {format_runtime_diagnostics(diagnostics)}."
                " 若设备已连接但仍失败，通常表示目标端口未监听或 Poco SDK 未初始化。"
                f" 原始错误: {exc}"
            ) from exc

    explorer = ColdStartExplorer(
        profile=profile,
        driver=driver,
        output_dir=Path(args.output),
        config=config,
    )

    result = explorer.run()
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
