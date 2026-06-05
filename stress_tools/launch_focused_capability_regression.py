from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "stress_tools" / "runs" / "focused_launcher_logs"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8031")
    parser.add_argument("--duration-min", type=float, default=30.0)
    parser.add_argument("--gap-sec", type=float, default=5.0)
    parser.add_argument("--monitor-interval-sec", type=float, default=15.0)
    parser.add_argument("--scenario-timeout-sec", type=float, default=900.0)
    parser.add_argument("--drain-sec", type=float, default=300.0)
    parser.add_argument("--app-turns", type=int, default=2)
    parser.add_argument("--ielts-turns", type=int, default=2)
    parser.add_argument("--greenfield-turns", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stdout_path = LOG_DIR / f"focused_{stamp}.out.log"
    stderr_path = LOG_DIR / f"focused_{stamp}.err.log"
    state_path = LOG_DIR / f"focused_{stamp}.state.json"

    cmd = [
        sys.executable,
        str(ROOT / "stress_tools" / "run_focused_capability_regression.py"),
        "--base-url",
        args.base_url,
        "--duration-min",
        str(args.duration_min),
        "--gap-sec",
        str(args.gap_sec),
        "--monitor-interval-sec",
        str(args.monitor_interval_sec),
        "--scenario-timeout-sec",
        str(args.scenario_timeout_sec),
        "--drain-sec",
        str(args.drain_sec),
        "--app-turns",
        str(args.app_turns),
        "--ielts-turns",
        str(args.ielts_turns),
        "--greenfield-turns",
        str(args.greenfield_turns),
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + str(ROOT / ".venv" / "Lib" / "site-packages")

    with stdout_path.open("w", encoding="utf-8", errors="replace") as stdout, stderr_path.open(
        "w", encoding="utf-8", errors="replace"
    ) as stderr:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
            stdout=stdout,
            stderr=stderr,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            close_fds=False,
        )

    state = {
        "pid": proc.pid,
        "cmd": cmd,
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "state_path": str(state_path),
        "status": "started",
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
