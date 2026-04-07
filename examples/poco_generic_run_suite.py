from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run_command(command: list[str], cwd: Path) -> None:
    result = subprocess.run(command, cwd=str(cwd), check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(description="两阶段 UI 跑测：第一轮正常建图，第二轮地图回放并定时 kill")
    parser.add_argument("--device-uri", default="Android:///emulator-5554")
    parser.add_argument("--device-serial", default="emulator-5554")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--package", default="com.NetEase")
    parser.add_argument("--activity", default="com.NetEase/com.unity3d.player.UnityPlayerActivity")
    parser.add_argument("--output-root", default="outputs/generic_suite_run")
    parser.add_argument("--boot-wait", type=float, default=8.0)
    parser.add_argument("--action-wait", type=float, default=2.0)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--max-actions-per-page", type=int, default=20)
    parser.add_argument("--kill-after-seconds", type=float, default=6.0)
    args = parser.parse_args()

    output_root = Path(args.output_root)
    normal_output = output_root / "normal"
    crash_output = output_root / "crash"
    map_file = normal_output / "map.json"

    common = [
        sys.executable,
        ".\\examples\\poco_generic_game_runner.py",
        "--device-uri",
        args.device_uri,
        "--device-serial",
        args.device_serial,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--package",
        args.package,
        "--activity",
        args.activity,
        "--boot-wait",
        str(args.boot_wait),
        "--action-wait",
        str(args.action_wait),
        "--max-pages",
        str(args.max_pages),
        "--max-actions-per-page",
        str(args.max_actions_per_page),
    ]

    run_command([*common, "--mode", "discover", "--output", str(normal_output)], ROOT)
    run_command(
        [
            *common,
            "--mode",
            "replay",
            "--output",
            str(crash_output),
            "--map-file",
            str(map_file),
            "--kill-after-seconds",
            str(args.kill_after_seconds),
        ],
        ROOT,
    )


if __name__ == "__main__":
    main()
