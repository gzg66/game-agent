"""手动点击 UI 树探针 Demo。

用途：
1. 连接当前设备与 Poco
2. 持续轮询 UI 树
3. 当 UI 树发生变化时，把新增文本和疑似提示文案输出到终端

适用场景：
- 用户手动点击未解锁按钮
- 验证临时提示是否真的进入 Poco UI 树
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poco_ui_automation.cold_start.config import GameConfig  # noqa: E402
from poco_ui_automation.cold_start.explorer import GameConnector  # noqa: E402
from poco_ui_automation.cold_start.observation import ObservationCapture, ObservedNode, PageObservation  # noqa: E402


def load_env_file(env_path: Path) -> None:
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


def run_adb_global(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [adb_path(), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )


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
    if not device_serial or ":" not in device_serial:
        return False

    host, _, port_text = device_serial.rpartition(":")
    if not host or not port_text.isdigit():
        return False

    connect_output = adb_global_output("connect", device_serial)
    if connect_output:
        print(f"[探针] adb connect {device_serial}: {connect_output}")

    normalized = connect_output.lower()
    return "connected to" in normalized or "already connected to" in normalized


def ensure_device_ready(config: GameConfig) -> None:
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


def load_config(config_path: Path) -> GameConfig:
    if config_path.exists():
        print(f"[探针] 加载配置: {config_path}")
        return GameConfig.load(config_path)
    print(f"[探针] 配置文件不存在，使用默认配置: {config_path}")
    return GameConfig()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "").strip().lower()


def cleanup_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120]


def is_hint_like(text: str) -> bool:
    cleaned = cleanup_text(text)
    if not cleaned:
        return False
    patterns = [
        r"\d+\s*级",
        r"解锁",
        r"开启",
        r"开放",
        r"暂未开放",
        r"尚未开放",
        r"未开启",
        r"敬请期待",
    ]
    return any(re.search(pattern, cleaned) for pattern in patterns)


def node_center_distance(node: ObservedNode) -> float:
    if not isinstance(node.pos, list) or len(node.pos) != 2:
        return 999.0
    x, y = node.pos
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return 999.0
    return abs(x - 0.5) + abs(y - 0.35)


def top_center_texts(observation: PageObservation, limit: int = 12) -> list[str]:
    texts = []
    for node in sorted(observation.text_nodes, key=node_center_distance):
        cleaned = cleanup_text(node.text)
        if not cleaned:
            continue
        texts.append(cleaned)
    deduped: list[str] = []
    seen: set[str] = set()
    for text in texts:
        key = normalize_text(text)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(text)
        if len(deduped) >= limit:
            break
    return deduped


def diff_new_texts(previous: PageObservation | None, current: PageObservation) -> list[str]:
    current_texts = [cleanup_text(node.text) for node in current.text_nodes if cleanup_text(node.text)]
    current_pairs = [(normalize_text(text), text) for text in current_texts]

    if previous is None:
        seen: set[str] = set()
        result: list[str] = []
        for key, text in current_pairs:
            if key in seen:
                continue
            seen.add(key)
            result.append(text)
        return result

    previous_keys = {
        normalize_text(cleanup_text(node.text))
        for node in previous.text_nodes
        if cleanup_text(node.text)
    }
    result: list[str] = []
    seen: set[str] = set()
    for key, text in current_pairs:
        if not key or key in previous_keys or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def compute_file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    hasher = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def capture_screenshot(
    connector: GameConnector,
    output_dir: Path,
    sample_idx: int,
) -> tuple[str, str]:
    temp_path = output_dir / "_latest_screen.png"
    if not connector.snapshot(str(temp_path)):
        return "", ""

    image_hash = compute_file_hash(temp_path)
    if not image_hash:
        return "", ""

    final_path = output_dir / f"sample_{sample_idx:04d}.png"
    shutil.copyfile(temp_path, final_path)
    return image_hash, str(final_path)


def save_snapshot(
    output_dir: Path,
    sample_idx: int,
    observation: PageObservation,
    change_kind: str,
    screenshot_hash: str = "",
    screenshot_path: str = "",
) -> None:
    payload = {
        "sample_idx": sample_idx,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "change_kind": change_kind,
        "signature": observation.signature,
        "title": observation.title,
        "screenshot_hash": screenshot_hash,
        "screenshot_path": screenshot_path,
        "text_nodes": [
            {
                "name": node.name,
                "text": cleanup_text(node.text),
                "path": node.path,
                "pos": node.pos,
                "size": node.size,
            }
            for node in observation.text_nodes
            if cleanup_text(node.text)
        ],
        "actionable_candidates": [
            {
                "name": node.name,
                "text": cleanup_text(node.text),
                "path": node.path,
                "pos": node.pos,
                "size": node.size,
                "clickable": node.clickable,
                "interactive": node.interactive,
            }
            for node in observation.actionable_candidates[:80]
        ],
    }
    target = output_dir / f"sample_{sample_idx:04d}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def print_observation(
    previous: PageObservation | None,
    current: PageObservation,
    sample_idx: int,
    change_kind: str,
    screenshot_hash: str = "",
    screenshot_path: str = "",
) -> None:
    new_texts = diff_new_texts(previous, current)
    hint_texts = [text for text in new_texts if is_hint_like(text)]
    if not hint_texts:
        hint_texts = [text for text in top_center_texts(current) if is_hint_like(text)]

    print()
    print(f"[探针] ---------- 样本 {sample_idx} ----------")
    print(f"[探针] 变化类型: {change_kind}")
    print(f"[探针] 页面标题: {current.title or '<empty>'}")
    print(f"[探针] 页面签名: {current.signature}")
    print(
        f"[探针] 节点统计: all={len(current.all_nodes)} "
        f"text={len(current.text_nodes)} actionable={len(current.actionable_candidates)}"
    )
    if screenshot_hash:
        print(f"[探针] 截图哈希: {screenshot_hash[:12]}")
    if screenshot_path:
        print(f"[探针] 截图文件: {screenshot_path}")

    if hint_texts:
        print("[探针] 疑似提示文本:")
        for text in hint_texts[:8]:
            print(f"  - {text}")
    else:
        print("[探针] 疑似提示文本: <none>")

    if new_texts:
        print("[探针] 新增文本:")
        for text in new_texts[:15]:
            print(f"  - {text}")
    else:
        print("[探针] 新增文本: <none>")

    center_texts = top_center_texts(current, limit=10)
    if center_texts:
        print("[探针] 中心区域文本候选:")
        for text in center_texts:
            print(f"  - {text}")


def monitor_ui_tree(
    connector: GameConnector,
    interval_s: float,
    duration_s: float,
    output_dir: Path,
) -> None:
    observer = ObservationCapture()
    previous_obs: PageObservation | None = None
    previous_screenshot_hash = ""
    start_time = time.time()
    sample_idx = 0
    stable_count = 0

    print()
    print("[探针] 已开始监听 UI 树与截图。你现在可以手动点击未解锁按钮。")
    print(f"[探针] 轮询间隔: {interval_s:.2f}s, 监听时长: {duration_s:.1f}s")
    print("[探针] 当 UI 树或截图变化时，我会打印新增文本、疑似提示文本和变化类型。")

    while time.time() - start_time < duration_s:
        hierarchy = connector.dump_hierarchy(retries=1, wait_s=0.05)
        if hierarchy is None:
            print("[探针] 本轮未获取到 UI 树。")
            time.sleep(interval_s)
            continue

        sample_idx += 1
        obs = observer.capture(hierarchy)
        screenshot_hash, screenshot_path = capture_screenshot(connector, output_dir, sample_idx)

        ui_changed = previous_obs is None or obs.signature != previous_obs.signature
        screenshot_changed = bool(screenshot_hash) and screenshot_hash != previous_screenshot_hash

        change_kind = ""
        if previous_obs is None:
            change_kind = "initial"
        elif ui_changed and screenshot_changed:
            change_kind = "ui_and_screenshot_changed"
        elif ui_changed:
            change_kind = "ui_only_changed"
        elif screenshot_changed:
            change_kind = "screenshot_only_changed"

        if change_kind:
            stable_count = 0
            print_observation(
                previous_obs,
                obs,
                sample_idx,
                change_kind,
                screenshot_hash=screenshot_hash,
                screenshot_path=screenshot_path,
            )
            save_snapshot(
                output_dir,
                sample_idx,
                obs,
                change_kind,
                screenshot_hash=screenshot_hash,
                screenshot_path=screenshot_path,
            )
            previous_obs = obs
            if screenshot_hash:
                previous_screenshot_hash = screenshot_hash
        else:
            stable_count += 1
            if stable_count % max(1, int(2.0 / max(interval_s, 0.05))) == 0:
                print(
                    f"[探针] UI树/截图均未变化: signature={obs.signature}, "
                    f"已稳定 {stable_count} 轮"
                )
        time.sleep(interval_s)

    print()
    print("[探针] 监听结束。")
    print(f"[探针] 快照目录: {output_dir}")


def main() -> None:
    load_env_file(ROOT / ".env")

    parser = argparse.ArgumentParser(description="手动点击 UI 树探针 Demo")
    parser.add_argument(
        "--config",
        default=str(ROOT / "examples" / "cold_start_game_config.yaml"),
        help="游戏配置文件路径（YAML 或 JSON）",
    )
    parser.add_argument("--device-uri", default=argparse.SUPPRESS)
    parser.add_argument("--device-serial", default=argparse.SUPPRESS)
    parser.add_argument("--interval", type=float, default=0.25, help="轮询间隔秒数")
    parser.add_argument("--duration", type=float, default=60.0, help="监听时长秒数")
    parser.add_argument(
        "--output",
        default=str(ROOT / "outputs" / "ui_tree_probe"),
        help="输出目录，用于保存每次变化的 UI 树快照",
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))
    if hasattr(args, "device_uri"):
        config.device_uri = args.device_uri
    if hasattr(args, "device_serial"):
        config.device_serial = args.device_serial

    ensure_device_ready(config)
    config.validate()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[探针] 设备: {config.device_serial}")
    print(f"[探针] 引擎: {config.engine_type}")
    print(f"[探针] 包名: {config.package_name}")
    print(f"[探针] Poco: {config.poco_host}:{config.effective_poco_port()}")

    connector = GameConnector(config)
    connector.connect()
    print(f"[探针] 连接成功，屏幕尺寸: {connector.get_screen_size()}")

    monitor_ui_tree(
        connector=connector,
        interval_s=max(0.05, args.interval),
        duration_s=max(1.0, args.duration),
        output_dir=output_dir,
    )


if __name__ == "__main__":
    main()
