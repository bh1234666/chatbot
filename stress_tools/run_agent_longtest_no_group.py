from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from run_capability_regression import (
        APP_TASKS,
        GREENFIELD_TASKS,
        IELTS_TASKS,
        EnvClient,
        PROJECT_ROOT,
        Recorder,
        response_issues,
        setup_projects,
        static_inventory,
        validate_app_clone,
        validate_engineering,
        validate_greenfield,
    )
    from run_focused_capability_regression import (
        scenario_consistency_issues,
        validate_ielts_extended,
    )
    from run_four_hour_longtest import start_service, stop_service, wait_health
except ModuleNotFoundError:
    from .run_capability_regression import (
        APP_TASKS,
        GREENFIELD_TASKS,
        IELTS_TASKS,
        EnvClient,
        PROJECT_ROOT,
        Recorder,
        response_issues,
        setup_projects,
        static_inventory,
        validate_app_clone,
        validate_engineering,
        validate_greenfield,
    )
    from .run_focused_capability_regression import (
        scenario_consistency_issues,
        validate_ielts_extended,
    )
    from .run_four_hour_longtest import start_service, stop_service, wait_health

"""
Keep imports compatible with both direct script execution and `python -m`.
The long-test launcher uses module mode so background runs inherit a stable
package root; older manual runs used script mode from `stress_tools/`.
"""


RUNS_DIR = PROJECT_ROOT / "stress_tools" / "runs" / "agent_longtest_no_group"


def now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


EXTRA_COMPLEX_TASKS: dict[str, list[str]] = {
    "app": [
        (
            "In this isolated app clone, trace how environment workflow events move from helper execution "
            "to SSE output. Produce a concise architecture note in `analysis_outputs/workflow_event_path.md` "
            "with exact file references, verified by reading source files."
        ),
        (
            "Inspect helper delegation prompts and guard behavior in this clone. Identify whether helper tasks "
            "are framed with a clear shared framework before large work is delegated. Write "
            "`analysis_outputs/helper_framework_review.md` with evidence and recommendations."
        ),
    ],
    "ielts": [
        (
            "Create `analysis_outputs/ielts_four_skills_plan.md`. Read available material evidence and organize "
            "IELTS preparation into listening, reading, writing, and speaking. Include which materials were actually "
            "read, what was not readable, and what still needs OCR or manual review."
        ),
        (
            "Create `analysis_outputs/ielts_vocab_and_templates.md`. Extract high-value vocabulary and template "
            "patterns from readable files. If evidence is insufficient, state the gap instead of inventing content."
        ),
    ],
    "engineering": [
        (
            "Inventory all engineering management materials in this directory. Produce "
            "`analysis_outputs/engineering_material_index.md` with verified counts by file type, "
            "notable source filenames, and which files were actually read or require extraction."
        ),
        (
            "Using the readable engineering management materials, write "
            "`analysis_outputs/engineering_management_report.md`. Organize project-management concepts, "
            "coursework requirements, reusable templates, and unresolved evidence gaps with source filenames."
        ),
        (
            "Create `analysis_outputs/engineering_deliverable_plan.md` that turns the material set into a "
            "practical assignment workflow. Reuse prior indexes or reports when present, verify files before "
            "claiming coverage, and mark unreadable files explicitly."
        ),
    ],
    "greenfield": [
        (
            "Extend the mixed-language project with a documented benchmark pipeline: Python orchestrates runs, "
            "the native utility produces deterministic data, and the browser UI can load a JSON report. Add tests "
            "and update `scripts/check_project.py`."
        ),
        (
            "Perform a maintenance pass on the project: improve module boundaries, add failure-mode documentation, "
            "run the self-check, and write `docs/maintenance_report.md` with exact verification output."
        ),
    ],
}


def scenario_plan() -> list[dict[str, Any]]:
    return [
        {
            "label": "app",
            "user_id": "long_app",
            "tasks": APP_TASKS + EXTRA_COMPLEX_TASKS["app"],
            "validate": validate_app_clone,
            "must_reference_files": True,
        },
        {
            "label": "ielts",
            "user_id": "long_ielts",
            "tasks": IELTS_TASKS + EXTRA_COMPLEX_TASKS["ielts"],
            "validate": validate_ielts_extended,
            "must_reference_files": True,
        },
        {
            "label": "engineering",
            "user_id": "long_engineering",
            "tasks": EXTRA_COMPLEX_TASKS["engineering"],
            "validate": validate_engineering,
            "must_reference_files": True,
        },
        {
            "label": "greenfield",
            "user_id": "long_greenfield",
            "tasks": GREENFIELD_TASKS + EXTRA_COMPLEX_TASKS["greenfield"],
            "validate": validate_greenfield,
            "must_reference_files": False,
        },
    ]


def _workflow_issue_summary(result: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    workflow = result.get("workflow_tail") or []
    if result.get("latency_sec", 0) >= 60 and not workflow:
        issues.append("no_workflow_tail_for_long_turn")
    text = json.dumps(workflow, ensure_ascii=False)
    if "completed" in text.lower() and any(marker in text.lower() for marker in ("failed", "blocked", "missing")):
        issues.append("workflow_mixes_completion_with_blockers")
    return issues


async def monitor_loop(client: EnvClient, recorder: Recorder, stop: asyncio.Event, interval: float) -> None:
    while not stop.is_set():
        try:
            r = await client.client.get(f"{client.base_url}/v1/environment/active")
            active = r.json() if r.status_code == 200 else {"status_code": r.status_code, "text": r.text[:500]}
        except Exception as exc:
            active = {"error": f"{type(exc).__name__}: {exc}"}
        await recorder.record({"kind": "monitor", "active": active})
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def write_report(run_dir: Path, projects: dict[str, Path], *, partial: bool) -> None:
    events_path = run_dir / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()] if events_path.exists() else []
    results = [e for e in events if e.get("kind") == "scenario_result"]
    summary = {
        "run_dir": str(run_dir),
        "partial": partial,
        "events": len(events),
        "scenario_results": len(results),
        "scenario_issue_results": sum(1 for e in results if e.get("issues") or not e.get("ok")),
        "projects": {k: str(v) for k, v in projects.items()},
        "final_validation": {
            "app": validate_app_clone(projects["app"]),
            "ielts": validate_ielts_extended(projects["ielts"]),
            "engineering": validate_engineering(projects["engineering"]),
            "greenfield": validate_greenfield(projects["greenfield"]),
        },
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Agent Long Test Without Group Chat",
        "",
        f"- Partial: {partial}",
        f"- Events: {summary['events']}",
        f"- Scenario results: {summary['scenario_results']}",
        f"- Results with issues: {summary['scenario_issue_results']}",
        "",
        "## Scenario Results",
        "",
    ]
    for e in results[-80:]:
        text = str(e.get("text") or "").replace("\n", " ")
        lines.append(
            f"- `{e.get('label')}` turn={e.get('turn')} ok={e.get('ok')} "
            f"latency={e.get('latency_sec')}s issues={e.get('issues')} text={text[:600]}"
        )
    lines.extend(["", "## Final Validation", "", "```json", json.dumps(summary["final_validation"], ensure_ascii=False, indent=2), "```"])
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


async def run(args: argparse.Namespace) -> Path:
    run_dir = RUNS_DIR / ("long_" + now_id())
    run_dir.mkdir(parents=True, exist_ok=True)
    base_url = f"http://127.0.0.1:{args.port}"
    service = start_service(run_dir, args.port) if args.start_service else None
    client: EnvClient | None = None
    projects: dict[str, Path] = {}
    try:
        await wait_health(base_url, args.health_timeout_sec)
        projects = setup_projects(run_dir)
        recorder = Recorder(run_dir)
        client = EnvClient(base_url)
        await recorder.record({
            "kind": "run_start",
            "base_url": base_url,
            "duration_min": args.duration_min,
            "scope": "environment_agent_only_no_group_chat",
            "projects": {k: str(v) for k, v in projects.items()},
            "initial_inventory": {k: static_inventory(v) for k, v in projects.items()},
        })
        stop = asyncio.Event()
        monitor = asyncio.create_task(monitor_loop(client, recorder, stop, args.monitor_interval_sec))
        end_at = time.monotonic() + args.duration_min * 60
        plan = scenario_plan()
        if args.scenario:
            available = {item["label"]: item for item in plan}
            selected = list(dict.fromkeys(args.scenario))
            plan = [available[label] for label in selected if label in available]
            unknown = set(selected).difference(available)
            if unknown:
                raise ValueError(f"unknown scenario(s): {', '.join(sorted(unknown))}")
        turn_by_label = {item["label"]: args.start_turn for item in plan}
        try:
            while time.monotonic() < end_at:
                made_progress = False
                for item in plan:
                    if time.monotonic() >= end_at:
                        break
                    label = item["label"]
                    turn = turn_by_label[label]
                    if (
                        args.max_turns_per_scenario is not None
                        and turn - args.start_turn >= args.max_turns_per_scenario
                    ):
                        continue
                    task_list = item["tasks"]
                    message = (
                        task_list[turn % len(task_list)]
                        + f"\n\nLong-test cycle: {turn // len(task_list) + 1}. Continue from the actual current project state. "
                        "Use helpers for broad reading or broad execution, keep the main process focused on coordination, "
                        "and verify concrete files or command output before claiming completion."
                    )
                    root = projects[label]
                    before = item["validate"](root)
                    await recorder.record({
                        "kind": "scenario_request",
                        "label": label,
                        "turn": turn,
                        "root": str(root),
                        "before": before,
                        "message": message,
                    })
                    started = time.monotonic()
                    try:
                        result = await asyncio.wait_for(
                            client.ask(
                                user_id=item["user_id"],
                                current_dir=root,
                                message=message,
                                label=label,
                                turn=turn,
                            ),
                            timeout=args.scenario_timeout_sec,
                        )
                        after = item["validate"](root)
                        issues = response_issues(result.get("text") or "", must_reference_files=item["must_reference_files"])
                        issues.extend(scenario_consistency_issues(label, result.get("text") or "", after))
                        issues.extend(_workflow_issue_summary(result))
                    except asyncio.TimeoutError:
                        after = item["validate"](root)
                        issues = ["scenario_timeout"]
                        result = {
                            "ok": False,
                            "status_code": 0,
                            "latency_sec": round(time.monotonic() - started, 3),
                            "text": "",
                            "error": f"timeout after {args.scenario_timeout_sec}s",
                        }
                    except Exception as exc:
                        after = item["validate"](root)
                        issues = ["scenario_exception"]
                        result = {
                            "ok": False,
                            "status_code": 0,
                            "latency_sec": round(time.monotonic() - started, 3),
                            "text": "",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    await recorder.record({
                        "kind": "scenario_result",
                        "label": label,
                        "turn": turn,
                        "root": str(root),
                        "after": after,
                        "issues": issues,
                        **result,
                    })
                    turn_by_label[label] += 1
                    made_progress = True
                    await write_report(run_dir, projects, partial=True)
                    await asyncio.sleep(args.gap_sec)
                if not made_progress:
                    await asyncio.sleep(5.0)
        finally:
            stop.set()
            await monitor
            await write_report(run_dir, projects, partial=False)
    finally:
        if client is not None:
            await client.close()
        if args.stop_service:
            stop_service(service)
    return run_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-min", type=float, default=240.0)
    parser.add_argument("--port", type=int, default=8074)
    parser.add_argument("--start-service", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--stop-service", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--health-timeout-sec", type=float, default=120.0)
    parser.add_argument("--scenario-timeout-sec", type=float, default=1800.0)
    parser.add_argument("--scenario", action="append", choices=["app", "ielts", "engineering", "greenfield"])
    parser.add_argument("--max-turns-per-scenario", type=int)
    parser.add_argument("--start-turn", type=int, default=0)
    parser.add_argument("--gap-sec", type=float, default=5.0)
    parser.add_argument("--monitor-interval-sec", type=float, default=20.0)
    return parser.parse_args()


if __name__ == "__main__":
    print(asyncio.run(run(parse_args())))
