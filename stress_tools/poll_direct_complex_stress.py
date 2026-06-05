import argparse
import json
import subprocess
from pathlib import Path

from run_complex_long_stress import RUNS_DIR


def _newest_launch() -> Path | None:
    dirs = [p for p in RUNS_DIR.glob("direct_launch_*") if p.is_dir()]
    return max(dirs, key=lambda p: p.stat().st_mtime) if dirs else None


def _process_alive(pid: int) -> bool:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Id"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return str(pid) in result.stdout


def _tail(path: Path, n: int) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-n:]


def _resolve_run_dir(launch_dir: Path) -> Path | None:
    stdout = launch_dir / "launcher.stdout.log"
    for line in reversed(_tail(stdout, 20)):
        text = line.strip()
        if text and (Path(text).exists() or text.startswith(str(RUNS_DIR))):
            p = Path(text)
            if p.exists():
                return p
    candidates = [
        p for p in RUNS_DIR.glob("direct_only_*")
        if p.is_dir() and p.stat().st_mtime >= launch_dir.stat().st_mtime
    ]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--launch-dir", default="")
    p.add_argument("--tail", type=int, default=40)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    launch_dir = Path(args.launch_dir) if args.launch_dir else _newest_launch()
    if not launch_dir or not launch_dir.exists():
        print(json.dumps({"ok": False, "error": "no launch dir found"}, ensure_ascii=False, indent=2))
        return
    state_path = launch_dir / "launch_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    pid = int(state.get("pid") or 0)
    run_dir = _resolve_run_dir(launch_dir)
    out = {
        "ok": True,
        "launch_dir": str(launch_dir),
        "launcher_alive": _process_alive(pid) if pid else None,
        "pid": pid or None,
        "run_dir": str(run_dir) if run_dir else None,
        "summary_exists": bool(run_dir and (run_dir / "summary.json").exists()),
        "direct_events_tail": _tail(run_dir / "direct_events.jsonl", args.tail) if run_dir else [],
        "server_err_tail": _tail(run_dir / "server.err", args.tail) if run_dir else [],
        "launcher_stderr_tail": _tail(launch_dir / "launcher.stderr.log", args.tail),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
