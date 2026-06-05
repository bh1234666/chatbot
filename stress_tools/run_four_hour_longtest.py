from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

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
    validate_greenfield,
    validate_ielts,
)
from run_focused_capability_regression import (
    validate_ielts_extended,
    scenario_consistency_issues,
)


RUNS_DIR = PROJECT_ROOT / "stress_tools" / "runs" / "four_hour_longtest"


def now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def start_service(run_dir: Path, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["DEBUG_MODE"] = "true"
    env["DEBUG_LOG_DIR"] = str(run_dir / "debug_logs")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env.setdefault("LLM_STREAM_FIRST_CHUNK_TIMEOUT_SEC", "120")
    env.setdefault("STARTUP_OCR_WARM_ENABLED", "false")
    args = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    return subprocess.Popen(
        args,
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=(run_dir / "server.out").open("w", encoding="utf-8", errors="replace"),
        stderr=(run_dir / "server.err").open("w", encoding="utf-8", errors="replace"),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )


def stop_service(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


async def wait_health(base_url: str, timeout_sec: float) -> None:
    deadline = time.monotonic() + timeout_sec
    last = ""
    async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                r = await client.get(f"{base_url}/health")
                if r.status_code == 200:
                    return
                last = f"status={r.status_code} body={(r.text or '')[:300]!r}"
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(1.0)
    raise RuntimeError(f"service did not become healthy: {last}")


async def chat_once(base_url: str, turn: int) -> dict[str, Any]:
    group_id = "longtest_lowfreq_group"
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0), trust_env=False) as client:
        archive_resp = await client.post(
            f"{base_url}/v1/archives",
            json={"name": "longtest low frequency group", "persona_id": ""},
        )
        archive_resp.raise_for_status()
        archive_id = str(archive_resp.json()["archive_id"])
        join_resp = await client.post(
            f"{base_url}/v1/bot/groups/{group_id}/join",
            json={"archive_id": archive_id, "group_name": "low frequency regression", "persona_label": "bot"},
        )
        join_resp.raise_for_status()
        payload = {
            "archive_id": archive_id,
            "group_id": group_id,
            "user_id": f"group_user_{turn % 3}",
            "user_name": f"群友{turn % 3}",
            "message": (
                "低频群聊回归：简单聊一句最近项目维护状态，保持人设，不暴露内部流程。"
                if turn % 4
                else "低频群聊回归：如果有人问你能不能看图片、读文件，你应该如何自然说明能力边界？"
            ),
            "client_msg_id": f"low_group_{turn}_{uuid.uuid4().hex[:8]}",
        }
        started = time.monotonic()
        r = await client.post(f"{base_url}/v1/chat/stream", json=payload)
        text = r.content.decode("utf-8", errors="replace")
        return {
            "ok": r.status_code < 400 and "event: error" not in text,
            "status_code": r.status_code,
            "latency_sec": round(time.monotonic() - started, 3),
            "text_tail": text[-2000:],
        }


async def prepare_low_frequency_group(base_url: str, run_id: str) -> tuple[str, str]:
    group_id = f"longtest_lowfreq_group_{run_id}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0), trust_env=False) as client:
        archive_resp = await client.post(
            f"{base_url}/v1/archives",
            json={"name": "longtest low frequency group", "persona_id": ""},
        )
        archive_resp.raise_for_status()
        archive_id = str(archive_resp.json()["archive_id"])
        join_resp = await client.post(
            f"{base_url}/v1/bot/groups/{group_id}/join",
            json={"archive_id": archive_id, "group_name": "low frequency regression", "persona_label": "bot"},
        )
        join_resp.raise_for_status()
        return archive_id, group_id


async def clean_group_chat_once(base_url: str, archive_id: str, group_id: str, turn: int) -> dict[str, Any]:
    message = (
        "Low-frequency group regression: say one natural sentence about recent project maintenance status while staying in character and without exposing internal workflow."
        if turn % 4
        else "Low-frequency group regression: if someone asks whether you can inspect images and read files, explain the capability boundary naturally."
    )
    payload = {
        "archive_id": archive_id,
        "group_id": group_id,
        "user_id": f"group_user_{turn % 3}",
        "user_name": f"group_user_{turn % 3}",
        "message": message,
        "client_msg_id": f"low_group_{turn}_{uuid.uuid4().hex[:8]}",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0), trust_env=False) as client:
        started = time.monotonic()
        r = await client.post(f"{base_url}/v1/chat/stream", json=payload)
        text = r.content.decode("utf-8", errors="replace")
        return {
            "ok": r.status_code < 400 and "event: error" not in text,
            "status_code": r.status_code,
            "latency_sec": round(time.monotonic() - started, 3),
            "text_tail": text[-2000:],
        }


async def group_loop(
    base_url: str,
    recorder: Recorder,
    stop: asyncio.Event,
    *,
    run_id: str,
    gap_sec: float,
    end_at: float,
) -> None:
    """Run low-frequency group chat independently from long scenario turns."""
    try:
        archive_id, group_id = await prepare_low_frequency_group(base_url, run_id)
        await recorder.record({"kind": "group_setup", "archive_id": archive_id, "group_id": group_id})
    except Exception as exc:
        await recorder.record({"kind": "group_setup", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return

    turn = 0
    while not stop.is_set() and time.monotonic() < end_at:
        wait_sec = max(0.0, min(gap_sec, end_at - time.monotonic()))
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait_sec)
            break
        except asyncio.TimeoutError:
            pass
        if stop.is_set() or time.monotonic() >= end_at:
            break
        try:
            group_result = await clean_group_chat_once(base_url, archive_id, group_id, turn)
        except Exception as exc:
            group_result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "latency_sec": 0}
        await recorder.record({"kind": "group_result", "turn": turn, **group_result})
        turn += 1


def final_validation(projects: dict[str, Path]) -> dict[str, Any]:
    return {
        "app": validate_app_clone(projects["app"]),
        "ielts": validate_ielts_extended(projects["ielts"]),
        "greenfield": validate_greenfield(projects["greenfield"]),
    }


async def write_report(run_dir: Path, projects: dict[str, Path], *, partial: bool) -> None:
    events_path = run_dir / "events.jsonl"
    events = []
    if events_path.exists():
        events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    results = [e for e in events if e.get("kind") == "scenario_result"]
    group_results = [e for e in events if e.get("kind") == "group_result"]
    summary = {
        "run_dir": str(run_dir),
        "partial": partial,
        "events": len(events),
        "scenario_results": len(results),
        "scenario_issue_results": sum(1 for e in results if e.get("issues") or not e.get("ok")),
        "group_results": len(group_results),
        "group_issue_results": sum(1 for e in group_results if not e.get("ok")),
        "projects": {k: str(v) for k, v in projects.items()},
        "final_validation": final_validation(projects),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Four Hour Long Test Report",
        "",
        f"- Partial: {partial}",
        f"- Events: {summary['events']}",
        f"- Scenario results: {summary['scenario_results']}",
        f"- Scenario results with issues: {summary['scenario_issue_results']}",
        f"- Low-frequency group results: {summary['group_results']}",
        f"- Low-frequency group issues: {summary['group_issue_results']}",
        "",
        "## Scenario Results",
        "",
    ]
    for e in results[-60:]:
        text = str(e.get("text") or "").replace("\n", " ")
        lines.append(
            f"- `{e.get('label')}` turn={e.get('turn')} ok={e.get('ok')} "
            f"latency={e.get('latency_sec')}s issues={e.get('issues')} text={text[:500]}"
        )
    lines.extend(["", "## Group Results", ""])
    for e in group_results[-30:]:
        lines.append(
            f"- turn={e.get('turn')} ok={e.get('ok')} status={e.get('status_code')} "
            f"latency={e.get('latency_sec')}s"
        )
    lines.extend(["", "## Final Validation", "", "```json", json.dumps(summary["final_validation"], ensure_ascii=False, indent=2), "```"])
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def scenario_plan() -> list[tuple[str, str, list[str], Callable[[Path], dict[str, Any]], bool]]:
    return [
        ("app", "long_app", APP_TASKS, validate_app_clone, True),
        ("ielts", "long_ielts", IELTS_TASKS, validate_ielts_extended, True),
        ("greenfield", "long_greenfield", GREENFIELD_TASKS, validate_greenfield, False),
    ]


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


async def run(args: argparse.Namespace) -> Path:
    run_dir = RUNS_DIR / ("long_" + now_id())
    run_dir.mkdir(parents=True, exist_ok=True)
    base_url = f"http://127.0.0.1:{args.port}"
    service = start_service(run_dir, args.port) if args.start_service else None
    projects: dict[str, Path] = {}
    client: EnvClient | None = None
    try:
        await wait_health(base_url, args.health_timeout_sec)
        projects = setup_projects(run_dir)
        recorder = Recorder(run_dir)
        client = EnvClient(base_url)
        await recorder.record({
            "kind": "run_start",
            "base_url": base_url,
            "duration_min": args.duration_min,
            "projects": {k: str(v) for k, v in projects.items()},
            "initial_inventory": {k: static_inventory(v) for k, v in projects.items()},
        })
        stop = asyncio.Event()
        monitor = asyncio.create_task(monitor_loop(client, recorder, stop, args.monitor_interval_sec))
        end_at = time.monotonic() + args.duration_min * 60
        group_task = asyncio.create_task(
            group_loop(
                base_url,
                recorder,
                stop,
                run_id=run_dir.name,
                gap_sec=args.group_gap_sec,
                end_at=end_at,
            )
        )
        turn_by_label = {label: 0 for label, *_ in scenario_plan()}

        try:
            while time.monotonic() < end_at:
                made_progress = False
                for label, user_id, tasks, validate, must_reference_files in scenario_plan():
                    if time.monotonic() >= end_at:
                        break
                    turn = turn_by_label[label]
                    message = (
                        tasks[turn % len(tasks)]
                        + f"\n\nLong-test cycle: {turn // len(tasks) + 1}. Continue from the actual current project state. "
                        "Before claiming completion, verify concrete files or command output."
                    )
                    root = projects[label]
                    before = validate(root)
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
                            client.ask(user_id=user_id, current_dir=root, message=message, label=label, turn=turn),
                            timeout=args.scenario_timeout_sec,
                        )
                        after = validate(root)
                        issues = response_issues(result.get("text") or "", must_reference_files=must_reference_files)
                        issues.extend(scenario_consistency_issues(label, result.get("text") or "", after))
                    except asyncio.TimeoutError:
                        after = validate(root)
                        issues = ["scenario_timeout"]
                        result = {
                            "ok": False,
                            "status_code": 0,
                            "latency_sec": round(time.monotonic() - started, 3),
                            "text": "",
                            "error": f"timeout after {args.scenario_timeout_sec}s",
                        }
                    except Exception as exc:
                        after = validate(root)
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
            await asyncio.gather(monitor, group_task, return_exceptions=True)
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
    parser.add_argument("--port", type=int, default=8037)
    parser.add_argument("--start-service", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--stop-service", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--health-timeout-sec", type=float, default=120.0)
    parser.add_argument("--scenario-timeout-sec", type=float, default=1800.0)
    parser.add_argument("--gap-sec", type=float, default=5.0)
    parser.add_argument("--group-gap-sec", type=float, default=60.0)
    parser.add_argument("--monitor-interval-sec", type=float, default=30.0)
    return parser.parse_args()


if __name__ == "__main__":
    print(asyncio.run(run(parse_args())))
