import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from run_complex_long_stress import RUNS_DIR, now_id


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--duration-min", type=float, default=10.0)
    p.add_argument("--port", type=int, default=8062)
    p.add_argument("--health-timeout-sec", type=float, default=90.0)
    p.add_argument("--direct-gap-sec", type=float, default=1.0)
    p.add_argument("--direct-task-timeout-sec", type=float, default=600.0)
    p.add_argument("--direct-drain-sec", type=float, default=120.0)
    p.add_argument("--max-turns", type=int, default=1)
    p.add_argument("--task", action="append", default=[])
    return p.parse_args()


def main() -> Path:
    args = parse_args()
    launch_id = now_id()
    launch_dir = RUNS_DIR / f"direct_launch_{launch_id}"
    launch_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = launch_dir / "launcher.stdout.log"
    stderr_path = launch_dir / "launcher.stderr.log"
    cmd = [
        sys.executable,
        str(Path(__file__).with_name("run_direct_complex_stress.py")),
        "--duration-min",
        str(args.duration_min),
        "--port",
        str(args.port),
        "--health-timeout-sec",
        str(args.health_timeout_sec),
        "--direct-gap-sec",
        str(args.direct_gap_sec),
        "--direct-task-timeout-sec",
        str(args.direct_task_timeout_sec),
        "--direct-drain-sec",
        str(args.direct_drain_sec),
        "--max-turns",
        str(args.max_turns),
    ]
    tasks = args.task or ["small_algorithm_probe"]
    for task in tasks:
        cmd.extend(["--task", task])
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    with stdout_path.open("w", encoding="utf-8", errors="replace") as stdout, stderr_path.open("w", encoding="utf-8", errors="replace") as stderr:
        proc = subprocess.Popen(
            cmd,
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            stdout=stdout,
            stderr=stderr,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    state = {
        "pid": proc.pid,
        "cmd": cmd,
        "launch_dir": str(launch_dir),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "status": "started",
    }
    (launch_dir / "launch_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return launch_dir


if __name__ == "__main__":
    main()
