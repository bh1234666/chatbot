from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from stress_tools import run_app_clone_maintenance
from stress_tools.export_clawbench_partner_trace import export as export_partner_trace


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_trace_stats(trace_path: Path) -> dict[str, Any]:
    records = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tool_calls = [
        call
        for record in records
        for message in record.get("transcript", {}).get("messages", [])
        for call in message.get("tool_calls", [])
    ]
    return {
        "trace_records": len(records),
        "tool_calls": len(tool_calls),
        "tool_families": sorted({call.get("family") for call in tool_calls if call.get("family")}),
        "final_status": [
            record.get("artifacts", {}).get("final_status")
            for record in records
        ],
        "trace_completeness": sorted({
            record.get("metadata", {}).get("trace_completeness", "unknown")
            for record in records
        }),
    }


def summarize_run(run_dir: Path, trace_path: Path | None = None) -> dict[str, Any]:
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(summary_path)
    summary = _read_json(summary_path)
    trace_path = trace_path or (run_dir / "clawbench_partner_traces.jsonl")
    trace_stats = _load_trace_stats(trace_path) if trace_path.exists() else {}
    return {
        "run_dir": str(run_dir),
        "summary_path": str(summary_path),
        "report_path": str(run_dir / "report.md"),
        "trace_path": str(trace_path) if trace_path.exists() else "",
        "calls": summary.get("calls"),
        "errors": summary.get("errors"),
        "quality_issue_count": summary.get("quality_issue_count"),
        "quality_issues": summary.get("quality_issues", []),
        "latency_sec": summary.get("latency_sec", []),
        "event_counts": summary.get("event_counts", []),
        "validation": summary.get("validation", {}),
        "trace": trace_stats,
    }


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    harness_args = argparse.Namespace(
        duration_min=args.duration_min,
        port=args.port,
        start_service=args.start_service,
        health_timeout_sec=args.health_timeout_sec,
        review_gap_sec=args.review_gap_sec,
        idle_after_tasks_sec=args.idle_after_tasks_sec,
        stop_on_quality_issue=args.stop_on_quality_issue,
        auto_continue=args.auto_continue,
        max_auto_continue_turns=args.max_auto_continue_turns,
        max_auto_continue_sec=args.max_auto_continue_sec,
    )
    run_dir = await run_app_clone_maintenance.run(harness_args)
    trace_path = export_partner_trace(run_dir, args.trace_output)
    result = summarize_run(run_dir, trace_path)
    result_path = args.result_json or (run_dir / "clawbench_interface_result.json")
    result["result_json"] = str(result_path)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def cmd_run(args: argparse.Namespace) -> None:
    result = asyncio.run(run_benchmark(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_export(args: argparse.Namespace) -> None:
    trace_path = export_partner_trace(args.run_dir, args.output)
    result = summarize_run(args.run_dir, trace_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_summarize(args: argparse.Namespace) -> None:
    result = summarize_run(args.run_dir, args.trace_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Chatbot-to-ClawBench local interface. Runs the app-clone harness "
            "and exports ClawBench partner trace JSONL without changing business code."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run the app-clone harness and export partner trace.")
    run.add_argument("--duration-min", type=float, default=1.0)
    run.add_argument("--port", type=int, default=8125)
    run.add_argument("--start-service", action=argparse.BooleanOptionalAction, default=True)
    run.add_argument("--health-timeout-sec", type=float, default=120.0)
    run.add_argument("--review-gap-sec", type=float, default=5.0)
    run.add_argument("--idle-after-tasks-sec", type=float, default=2.0)
    run.add_argument("--stop-on-quality-issue", action=argparse.BooleanOptionalAction, default=True)
    run.add_argument("--auto-continue", action=argparse.BooleanOptionalAction, default=True)
    run.add_argument("--max-auto-continue-turns", type=int, default=1)
    run.add_argument("--max-auto-continue-sec", type=float, default=180.0)
    run.add_argument("--trace-output", type=Path)
    run.add_argument("--result-json", type=Path)
    run.set_defaults(func=cmd_run)

    export = sub.add_parser("export", help="Export an existing app-clone run to partner trace.")
    export.add_argument("run_dir", type=Path)
    export.add_argument("--output", type=Path)
    export.set_defaults(func=cmd_export)

    summarize = sub.add_parser("summarize", help="Print machine-readable run summary.")
    summarize.add_argument("run_dir", type=Path)
    summarize.add_argument("--trace-path", type=Path)
    summarize.set_defaults(func=cmd_summarize)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
