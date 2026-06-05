import argparse
import asyncio
import json
from pathlib import Path

try:
    from .run_complex_long_stress import (
        RUNS_DIR,
        now_id,
        run_direct_tasks,
        start_service,
        stop_service,
        summarize_direct,
        wait_health,
    )
except ImportError:
    from run_complex_long_stress import (
        RUNS_DIR,
        now_id,
        run_direct_tasks,
        start_service,
        stop_service,
        summarize_direct,
        wait_health,
    )


async def run(args: argparse.Namespace) -> Path:
    run_id = now_id()
    run_dir = RUNS_DIR / f"direct_only_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    base_url = f"http://127.0.0.1:{args.port}"
    proc = None
    try:
        if args.start_service:
            proc = start_service(run_dir, "127.0.0.1", args.port)
        await wait_health(base_url, args.health_timeout_sec)
        try:
            await asyncio.wait_for(
                run_direct_tasks(
                    base_url,
                    run_dir,
                    args.duration_min,
                    args.direct_gap_sec,
                    args.direct_task_timeout_sec,
                    args.max_turns,
                    set(args.task) if args.task else None,
                ),
                timeout=max(60.0, args.duration_min * 60 + args.direct_drain_sec),
            )
        except asyncio.TimeoutError:
            (run_dir / "direct_timeout.txt").write_text("direct-only stress timed out\n", encoding="utf-8")

        summary = {
            "run_id": run_dir.name,
            "base_url": base_url,
            "duration_min": args.duration_min,
            "mode": "direct_only_complex",
            "group_summary": {},
            "environment_summary": {},
            "direct_summary": summarize_direct(run_dir),
            "server_returncode": proc.poll() if proc else None,
        }
        (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            "# Direct Complex Stress Report",
            "",
            f"- Run: {run_dir.name}",
            f"- Duration min: {args.duration_min}",
            "- Scope: direct complex tasks only; no group simulation and no environment maintenance supervisor.",
            "",
            "```json",
            json.dumps(summary["direct_summary"], ensure_ascii=False, indent=2),
            "```",
        ]
        (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    finally:
        if args.start_service:
            stop_service(proc)
    return run_dir


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--duration-min", type=float, default=30.0)
    p.add_argument("--port", type=int, default=8061)
    p.add_argument("--start-service", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--health-timeout-sec", type=float, default=90.0)
    p.add_argument("--direct-gap-sec", type=float, default=3.0)
    p.add_argument("--direct-task-timeout-sec", type=float, default=1800.0)
    p.add_argument("--direct-drain-sec", type=float, default=1200.0)
    p.add_argument("--max-turns", type=int, default=None)
    p.add_argument("--task", action="append", default=[])
    p.epilog = "Direct complex tasks only. This script never starts group simulation or environment maintenance supervisor."
    return p.parse_args()


if __name__ == "__main__":
    print(asyncio.run(run(parse_args())))
