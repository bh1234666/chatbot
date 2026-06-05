from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable
import re
import sys

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from run_capability_regression import (
    EnvClient,
    PROJECT_ROOT,
    RUNS_DIR,
    Recorder,
    now_id,
    response_issues,
    setup_projects,
    static_inventory,
    validate_app_clone,
    validate_greenfield,
    validate_ielts,
)


APP_TASKS = [
    (
        "Read the isolated app clone with a full recursive traversal. "
        "Count Python files under `app/` only, list the 12 largest `app/` Python files by byte size, "
        "and explain how you avoided relying on a truncated directory tree. "
        "Report only verified facts."
    ),
    (
        "Analyze helper consistency in this app clone. Focus on readhelper, "
        "edithelper, codehelper, resource requests, freeze/resume, and final "
        "evidence verification. Use actual files and call sites; do not rely "
        "on guessed module names."
    ),
    (
        "Make one low-risk improvement in this clone that improves helper/result "
        "verification or complex-task reliability. Apply the change inside this "
        "isolated project only, run the narrowest meaningful validation, and "
        "report changed project files plus command output summary."
    ),
    (
        "Continue from the previous change. Inspect whether the implementation, "
        "tests, and prompt guidance agree. If a mismatch exists, fix the smallest "
        "real cause and verify it. If no mismatch exists, add one focused regression "
        "test for a realistic edge path."
    ),
]


IELTS_TASKS = [
    (
        "Inventory all materials in this directory. Separate text, images, audio, "
        "PDF, Word, zip, and other files. Produce "
        "`analysis_outputs/ielts_material_index.md` with verified counts, notable "
        "filenames, and what can be read directly versus what needs OCR/audio "
        "transcription."
    ),
    (
        "Using readable material from the directory, write "
        "`analysis_outputs/ielts_learning_report.md`. Cover writing, speaking, "
        "listening, reading, missing evidence, contradictions, and source filenames. "
        "Read file contents rather than guessing from names."
    ),
    (
        "Create a visual study artifact from the verified IELTS material: "
        "`analysis_outputs/ielts_study_map.png` or `.svg`, plus a short "
        "`analysis_outputs/ielts_visual_notes.md` explaining what data supports "
        "the visual. Verify the image file exists in the project directory."
    ),
    (
        "Generate a high-quality IELTS improvement plan in "
        "`analysis_outputs/ielts_4week_plan.md`. It should reuse the material "
        "index and learning report when present, cite source filenames, and mark "
        "uncertain items clearly."
    ),
]


GREENFIELD_TASKS = [
    (
        "This directory is empty. Build a complete small local agent project from "
        "scratch with mixed languages: Python backend/core, JavaScript browser UI, "
        "a small C or C++ native utility, tests, docs, fixtures, and "
        "`scripts/check_project.py`. Keep it dependency-light and runnable locally."
    ),
    (
        "Continue the project. Add a second capability that requires multiple files "
        "to cooperate, improve tests, run the self-check, and document architecture "
        "and verification. Do not restart from scratch."
    ),
    (
        "Perform a maintenance pass on the project you built. Find one real weakness "
        "in structure, validation, or user workflow; fix it; run the self-check; "
        "and update docs to match the implementation."
    ),
    (
        "Add a small cross-language integration check that proves the Python and "
        "native utility parts agree on one shared data contract. Keep the project "
        "portable on Windows and verify it."
    ),
]


def validate_ielts_extended(root: Path) -> dict[str, Any]:
    data = validate_ielts(root)
    outputs = root / "analysis_outputs"
    data["analysis_outputs"] = static_inventory(outputs) if outputs.exists() else {"exists": False}
    data["expected_outputs"] = {
        name: (outputs / name).exists()
        for name in [
            "ielts_material_index.md",
            "ielts_learning_report.md",
            "ielts_visual_notes.md",
            "ielts_4week_plan.md",
        ]
    }
    data["visual_outputs"] = [
        str(p.relative_to(root)).replace("\\", "/")
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".svg", ".webp"}
        and "analysis_outputs" in p.parts
    ][:20]
    return data


async def run_scenario(
    *,
    label: str,
    user_id: str,
    root: Path,
    tasks: list[str],
    validate: Callable[[Path], dict[str, Any]],
    client: EnvClient,
    recorder: Recorder,
    end_at: float,
    gap_sec: float,
    max_turns: int,
    scenario_timeout_sec: float,
    scenario_timeout_by_label: dict[str, float] | None = None,
    report_writer: Callable[[], Any] | None = None,
) -> None:
    turn = 0
    while time.monotonic() < end_at and turn < max_turns:
        base = tasks[turn % len(tasks)]
        cycle = turn // len(tasks) + 1
        message = (
            f"{base}\n\n"
            f"Regression cycle: {cycle}. Continue from the actual current project state. "
            "Before claiming completion, verify concrete project files or command output."
        )
        before = validate(root)
        await recorder.record(
            {
                "kind": "scenario_request",
                "label": label,
                "turn": turn,
                "cycle": cycle,
                "root": str(root),
                "before": before,
                "message": message,
            }
        )
        if report_writer is not None:
            await report_writer()
        started = time.monotonic()
        timeout_sec = (
            scenario_timeout_by_label.get(label, scenario_timeout_sec)
            if scenario_timeout_by_label
            else scenario_timeout_sec
        )
        try:
            result = await asyncio.wait_for(
                client.ask(
                    user_id=user_id,
                    current_dir=root,
                    message=message,
                    label=label,
                    turn=turn,
                ),
                timeout=timeout_sec,
            )
            after = validate(root)
            issues = response_issues(result.get("text") or "", must_reference_files=label != "greenfield")
            issues.extend(scenario_consistency_issues(label, result.get("text") or "", after))
        except asyncio.TimeoutError:
            after = validate(root)
            issues = ["scenario_timeout"]
            result = {
                "ok": False,
                "label": label,
                "turn": turn,
                "latency_sec": round(time.monotonic() - started, 2),
                "text": "",
                "error": f"scenario timed out after {timeout_sec}s",
            }
        except Exception as exc:
            after = validate(root)
            issues = ["scenario_exception"]
            result = {
                "ok": False,
                "label": label,
                "turn": turn,
                "latency_sec": round(time.monotonic() - started, 2),
                "text": "",
                "error": f"{type(exc).__name__}: {exc}",
            }
        await recorder.record(
            {
                "kind": "scenario_result",
                "label": label,
                "turn": turn,
                "cycle": cycle,
                "root": str(root),
                "after": after,
                "issues": issues,
                **result,
            }
        )
        if report_writer is not None:
            await report_writer()
        turn += 1
        await asyncio.sleep(gap_sec)


def scenario_consistency_issues(label: str, text: str, after: dict[str, Any]) -> list[str]:
    """Check answer claims against independent project validation."""
    issues: list[str] = []
    if label == "app":
        expected = after.get("py_count")
        asks_python_count = any(
            marker in text.lower()
            for marker in (
                "python file",
                "python files",
                ".py file",
                ".py files",
                "python 文件",
                ".py 文件",
            )
        )
        if isinstance(expected, int) and asks_python_count:
            count_match = None
            patterns = (
                r"(?:Python|\.py)[^0-9\n]{0,30}(?:files?|文件总数|文件)[^0-9\n]{0,20}([0-9][0-9,]*)",
                r"([0-9][0-9,]*)[^0-9\n]{0,20}(?:Python|\.py)[^0-9\n]{0,20}(?:files?|文件)",
            )
            for pattern in patterns:
                for match in re.finditer(pattern, text, re.I):
                    claimed = int(match.group(1).replace(",", ""))
                    if claimed == expected:
                        count_match = match
                        break
                if count_match:
                    break
            if not count_match:
                for line in text.splitlines():
                    lower = line.lower()
                    if "python" not in lower and ".py" not in lower:
                        continue
                    if not re.search(r"(total|count|files?|总数|共有|共计|文件数)", lower):
                        continue
                    if re.search(r"(largest|top|最大|排名|bytes|字节)", lower):
                        continue
                    rough = re.search(r"([0-9][0-9,]*)", line)
                    if rough:
                        claimed = int(rough.group(1).replace(",", ""))
                        if claimed != expected:
                            issues.append(f"py_count_mismatch: claimed={claimed} validated={expected}")
                        count_match = rough
                        break
            if not count_match:
                issues.append("py_count_missing")
    elif label == "ielts":
        expected_outputs = after.get("expected_outputs") or {}
        if isinstance(expected_outputs, dict):
            missing_named = [
                name for name, exists in expected_outputs.items()
                if name in text and not exists
            ]
            if missing_named:
                issues.append("claimed_missing_ielts_outputs:" + ",".join(missing_named))
    elif label == "greenfield":
        inv = after.get("inventory") or {}
        if int(inv.get("file_count") or 0) <= 0:
            issues.append("greenfield_no_project_files")
        check = after.get("check_result") or {}
        check_ran = bool(check.get("attempted")) or isinstance(check.get("returncode"), int)
        if not check_ran:
            issues.append("greenfield_no_self_check")
        elif check.get("environment_issue") == "missing_python_dependency":
            issues.append("greenfield_self_check_missing_dependency:" + ",".join(check.get("missing_dependencies") or []))
        elif check.get("ok") is False:
            issues.append("greenfield_self_check_failed")
    return issues


async def monitor_loop(client: EnvClient, recorder: Recorder, stop: asyncio.Event, interval: float) -> None:
    while not stop.is_set():
        try:
            r = await client.client.get(f"{client.base_url}/v1/chat/active")
            active = r.json() if r.status_code == 200 else {"status_code": r.status_code, "text": r.text[:500]}
        except Exception as exc:
            active = {"error": f"{type(exc).__name__}: {exc}"}
        await recorder.record({"kind": "monitor", "active": active})
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def write_report(run_dir: Path, projects: dict[str, Path], *, partial: bool = False) -> None:
    events_path = run_dir / "events.jsonl"
    events = (
        [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if events_path.exists()
        else []
    )
    results = [e for e in events if e.get("kind") == "scenario_result"]
    summary = {
        "run_dir": str(run_dir),
        "events": len(events),
        "results": len(results),
        "issue_results": sum(1 for e in results if e.get("issues") or not e.get("ok")),
        "partial": partial,
        "projects": {k: str(v) for k, v in projects.items()},
        "final_validation": {
            "app": validate_app_clone(projects["app"]) if "app" in projects else {"missing_project_key": True},
            "ielts": validate_ielts_extended(projects["ielts"]) if "ielts" in projects else {"missing_project_key": True},
            "greenfield": validate_greenfield(projects["greenfield"]) if "greenfield" in projects else {"missing_project_key": True},
        },
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Focused Capability Regression Report",
        "",
        "This run excludes group simulation and only tests isolated project-maintenance paths.",
        "",
        f"- Partial: {summary['partial']}",
        f"- Events: {summary['events']}",
        f"- Scenario results: {summary['results']}",
        f"- Results with issues: {summary['issue_results']}",
        "",
        "## Scenario Results",
        "",
    ]
    for e in results:
        text = str(e.get("text") or "").replace("\n", " ")
        lines.append(
            f"- `{e.get('label')}` turn={e.get('turn')} cycle={e.get('cycle')} "
            f"ok={e.get('ok')} latency={e.get('latency_sec')}s "
            f"issues={e.get('issues')} text={text[:500]}"
        )
    lines.extend(["", "## Final Validation", "", "```json", json.dumps(summary["final_validation"], ensure_ascii=False, indent=2), "```"])
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


async def run(args: argparse.Namespace) -> Path:
    run_dir = RUNS_DIR / ("focused_" + now_id())
    run_dir.mkdir(parents=True, exist_ok=True)
    projects = setup_projects(run_dir)
    recorder = Recorder(run_dir)
    client = EnvClient(args.base_url)
    if not await client.health():
        raise RuntimeError(f"backend not healthy: {args.base_url}")
    await recorder.record(
        {
            "kind": "run_start",
            "base_url": args.base_url,
            "duration_min": args.duration_min,
            "note": "No group simulation is launched by this runner.",
            "projects": {k: str(v) for k, v in projects.items()},
            "initial_inventory": {k: static_inventory(v) for k, v in projects.items()},
        }
    )
    stop = asyncio.Event()
    monitor = asyncio.create_task(monitor_loop(client, recorder, stop, args.monitor_interval_sec))
    report_lock = asyncio.Lock()

    async def flush_report() -> None:
        async with report_lock:
            await write_report(run_dir, projects, partial=True)

    end_at = time.monotonic() + args.duration_min * 60
    scenarios = [
        ("app", "focused_app", projects["app"], APP_TASKS, validate_app_clone, args.app_turns),
        ("ielts", "focused_ielts", projects["ielts"], IELTS_TASKS, validate_ielts_extended, args.ielts_turns),
        ("greenfield", "focused_greenfield", projects["greenfield"], GREENFIELD_TASKS, validate_greenfield, args.greenfield_turns),
    ]
    per_label_timeout = {
        "greenfield": max(args.scenario_timeout_sec, args.greenfield_timeout_sec),
    }
    tasks = [
        asyncio.create_task(
            run_scenario(
                label=label,
                user_id=user_id,
                root=root,
                tasks=scenario_tasks,
                validate=validate,
                client=client,
                recorder=recorder,
                end_at=end_at,
                gap_sec=args.gap_sec,
                max_turns=max_turns,
                scenario_timeout_sec=args.scenario_timeout_sec,
                scenario_timeout_by_label=per_label_timeout,
                report_writer=flush_report,
            )
        )
        for label, user_id, root, scenario_tasks, validate, max_turns in scenarios
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=args.duration_min * 60 + args.drain_sec)
    except asyncio.TimeoutError:
        await recorder.record({"kind": "run_timeout"})
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        stop.set()
        await monitor
        await client.close()
        await write_report(run_dir, projects, partial=False)
    return run_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8031")
    parser.add_argument("--duration-min", type=float, default=300.0)
    parser.add_argument("--gap-sec", type=float, default=20.0)
    parser.add_argument("--monitor-interval-sec", type=float, default=30.0)
    parser.add_argument("--drain-sec", type=float, default=1800.0)
    parser.add_argument("--scenario-timeout-sec", type=float, default=1800.0)
    parser.add_argument("--greenfield-timeout-sec", type=float, default=1800.0)
    parser.add_argument("--app-turns", type=int, default=12)
    parser.add_argument("--ielts-turns", type=int, default=12)
    parser.add_argument("--greenfield-turns", type=int, default=12)
    return parser.parse_args()


if __name__ == "__main__":
    print(asyncio.run(run(parse_args())))
