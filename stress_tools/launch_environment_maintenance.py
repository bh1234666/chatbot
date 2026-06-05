from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "stress_tools" / "runs" / "environment_launch"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8076")
    parser.add_argument("--duration-min", type=float, default=18.0)
    parser.add_argument("--projects", type=int, default=1)
    parser.add_argument("--project-kinds", default="multilang")
    parser.add_argument("--max-inflight", type=int, default=1)
    parser.add_argument("--review-gap-sec", type=float, default=5.0)
    parser.add_argument("--monitor-interval-sec", type=float, default=10.0)
    parser.add_argument("--drain-sec", type=float, default=180.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = LOG_DIR / f"environment_maintenance_{stamp}.log"
    cmd = [
        str(ROOT / ".venv" / "Scripts" / "python.exe"),
        str(ROOT / "stress_tools" / "run_environment_maintenance.py"),
        "--base-url", args.base_url,
        "--duration-min", str(args.duration_min),
        "--projects", str(args.projects),
        "--project-kinds", args.project_kinds,
        "--max-inflight", str(args.max_inflight),
        "--review-gap-sec", str(args.review_gap_sec),
        "--monitor-interval-sec", str(args.monitor_interval_sec),
        "--drain-sec", str(args.drain_sec),
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    with out.open("w", encoding="utf-8", errors="replace") as fh:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
    print(f"pid={proc.pid}")
    print(f"log={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
