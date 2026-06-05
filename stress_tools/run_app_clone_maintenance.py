from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
import re
import codecs
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
RUNS_DIR = ROOT / "runs" / "app_clone"
_CODE_PATH_RE = re.compile(r"`([^`]+)`")


async def _iter_utf8_sse_lines(response: httpx.Response):
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        pending += decoder.decode(chunk)
        while True:
            marker = pending.find("\n")
            if marker < 0:
                break
            line = pending[:marker]
            pending = pending[marker + 1:]
            yield line.rstrip("\r")
    tail = pending + decoder.decode(b"", final=True)
    if tail:
        yield tail.rstrip("\r")


def now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def copy_app_clone(run_dir: Path) -> Path:
    clone_root = run_dir / "projects" / "chatbot_app_clone"
    src = PROJECT_ROOT / "app"
    dst = clone_root / "app"
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for rel in ("pytest.ini", "pyproject.toml", "requirements.txt", "requirements-dev.txt", ".env.example"):
        p = PROJECT_ROOT / rel
        if p.exists():
            shutil.copy2(p, clone_root / rel)
    (clone_root / "CLONE_NOTICE.md").write_text(
        "This is an isolated copy of F:/chatbot/app for environment-mode maintenance stress tests.\n"
        "The real project app/ directory must not be modified by this run.\n",
        encoding="utf-8",
    )
    return clone_root


def start_service(run_dir: Path, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["DEBUG_MODE"] = "true"
    env["DEBUG_LOG_DIR"] = str(run_dir / "debug_logs")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["STARTUP_OCR_WARM_ENABLED"] = "false"
    env["DREAM_ENABLED"] = "false"
    env.setdefault("LLM_STREAM_FIRST_CHUNK_TIMEOUT_SEC", "90")
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
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=20)
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
                last = f"status={r.status_code}"
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(1.0)
    raise RuntimeError(f"service did not become healthy: {last}")


class AppCloneClient:
    def __init__(self, base_url: str, run_dir: Path):
        self.base_url = base_url.rstrip("/")
        self.run_dir = run_dir
        self.events_path = run_dir / "events.jsonl"
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(3600.0, connect=20.0), trust_env=False)

    async def close(self) -> None:
        await self.client.aclose()

    async def record(self, event: dict[str, Any]) -> None:
        event = {"ts": now_iso(), **event}
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    async def ask_environment(self, *, project: Path, message: str, turn: int) -> dict[str, Any]:
        payload = {
            "user_id": "app_clone_maintainer",
            "user_name": "App Clone Maintainer",
            "message": message,
            "current_dir": str(project),
            "client_msg_id": f"app_clone_{turn}_{uuid.uuid4().hex[:8]}",
        }
        started = time.monotonic()
        tokens: list[str] = []
        events: dict[str, list[Any]] = {"progress": [], "workflow": [], "command": [], "error": [], "done": [], "meta": []}
        try:
            async with self.client.stream("POST", f"{self.base_url}/v1/environment/stream", json=payload) as r:
                r.encoding = "utf-8"
                if r.status_code >= 400:
                    body = await r.aread()
                    return {
                        "ok": False,
                        "status_code": r.status_code,
                        "latency_sec": round(time.monotonic() - started, 3),
                        "error": body.decode("utf-8", errors="replace")[:3000],
                    }
                current_event = "message"
                data_lines: list[str] = []
                async for line in _iter_utf8_sse_lines(r):
                    if line == "":
                        if data_lines:
                            raw = "\n".join(data_lines)
                            try:
                                data = json.loads(raw)
                            except Exception:
                                data = {"raw": raw}
                            if current_event == "token":
                                tokens.append(str(data.get("text") or ""))
                            elif current_event in events:
                                events[current_event].append(data)
                            elif current_event == "complete":
                                break
                        current_event = "message"
                        data_lines = []
                        continue
                    if line.startswith("event:"):
                        current_event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line.split(":", 1)[1].strip())
        except Exception as exc:
            return {
                "ok": False,
                "status_code": 0,
                "latency_sec": round(time.monotonic() - started, 3),
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {
            "ok": not events["error"],
            "status_code": 200,
            "latency_sec": round(time.monotonic() - started, 3),
            "text": "".join(tokens).strip(),
            "event_counts": {k: len(v) for k, v in events.items()},
            "errors": events["error"],
            "done": events["done"][-1] if events["done"] else {},
            "meta": events["meta"][-1] if events["meta"] else {},
        }

    async def auto_continue_check(
        self,
        *,
        user_message: str,
        assistant_reply: str,
        recent_context: str,
        elapsed_sec: float,
        max_sec: float,
    ) -> dict[str, Any]:
        payload = {
            "user_message": user_message,
            "assistant_reply": assistant_reply,
            "recent_context": recent_context,
            "auto_continue_elapsed_sec": elapsed_sec,
            "max_auto_continue_sec": max_sec,
        }
        try:
            r = await self.client.post(f"{self.base_url}/v1/chat/auto-continue/check", json=payload)
            return {"ok": r.status_code == 200, "status_code": r.status_code, **r.json()}
        except Exception as exc:
            return {"ok": False, "status_code": 0, "error": f"{type(exc).__name__}: {exc}"}


def evaluate_response_quality(turn: int, text: str, validation: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    stripped = (text or "").strip()
    lower = stripped.lower()
    if not stripped:
        issues.append("empty_reply")
    if "\ufffd" in stripped or "??" in stripped:
        issues.append("mojibake_or_replacement_text")
    if "DSML" in stripped or "<tool_call>" in stripped or "<env_" in stripped:
        issues.append("tool_markup_leaked")
    if "novelcraft" in stripped:
        issues.append("nonexistent_project_path_hallucination")
    if any(marker in lower for marker in (
        "if you want me to continue",
        "要我接着",
        "not yet actually",
        "还没实际",
        "not yet verified",
        "没有取得可核查",
        "not completed",
        "尚未完成",
        "still need to",
    )):
        issues.append("unfinished_reply_waiting_for_continue")
    if turn >= 2 and any(marker in lower for marker in (
        "i will first",
        "先确认",
        "let me first",
        "先看",
        "first inspect",
        "先检查",
        "before editing",
        "再动手",
        "find the relevant file",
        "找到相关文件",
        "跑一下目录结构",
    )):
        if not any(marker in lower for marker in (
            "modified",
            "已修改",
            "created",
            "已创建",
            "changed file",
            "变更文件",
            "verification",
            "验证结果",
            "pytest",
            "passed",
            "通过",
            "failed",
            "失败",
        )):
            issues.append("implementation_reply_only_preparation")
    if any(marker in stripped for marker in ("app/backend/", "app\\backend\\", "system_state_v2.py", "ai_brain.py")):
        issues.append("imagined_project_path_leaked")
    if turn in {1, 4}:
        allowed_top_dirs = set(validation.get("app_top_dirs") or [])
        if allowed_top_dirs:
            claimed_dirs: set[str] = set()
            for raw in _CODE_PATH_RE.findall(stripped):
                if not (raw.endswith(("/", "\\")) or raw.startswith(("app/", "app\\"))):
                    continue
                path = raw.replace("\\", "/").strip("/")
                if not path or any(ch in path for ch in "\n\r\t*"):
                    continue
                parts = [part for part in path.split("/") if part]
                if not parts:
                    continue
                if parts[0] == "app" and len(parts) > 1:
                    claimed_dirs.add(parts[1])
                elif raw.endswith(("/", "\\")) and len(parts) == 1:
                    claimed_dirs.add(parts[0])
            nonexistent = sorted(
                d
                for d in claimed_dirs
                if d not in allowed_top_dirs
                and d not in {"app", ".", ".."}
                and not d.endswith((".py", ".md", ".toml", ".txt", ".json", ".yaml", ".yml"))
            )
            if nonexistent:
                issues.append("nonexistent_top_level_directory_claim:" + ",".join(nonexistent[:6]))
    if turn >= 2 and validation.get("has_environment") and "environment" in lower:
        if any(marker in lower for marker in (
            "no related code",
            "not found",
            "does not exist",
            "missing module",
            "not in this project",
        )) or (
            "没有" in stripped and any(marker in stripped for marker in ("相关代码", "对应", "测试目录"))
        ):
            issues.append("false_missing_environment_code_blocker")
    if turn == 0:
        expected = [rel for _, rel in validation.get("top", [])[:4]]
        hits = sum(1 for rel in expected if rel.replace("/", "\\") in stripped or rel in stripped)
        if hits < 3:
            issues.append(f"top_file_ranking_mismatch:hits={hits}")
        if len(stripped) < 300 or "let me take a look" in lower:
            issues.append("incomplete_statistical_answer")
    if turn in {1, 4} and len(stripped) < 500:
        issues.append("architecture_review_too_shallow")
    if turn in {1, 4} and any(marker in lower for marker in (
        "first read",
        "先读取",
        "first inspect",
        "先看",
        "let me review",
        "我来做一次",
        "look at the real",
        "看看真实",
    )):
        if not any(marker in lower for marker in (
            "candidate",
            "候选",
            "split risk",
            "拆分风险",
            "split recommendation",
            "建议拆分",
            "evidence",
            "证据",
        )):
            issues.append("analysis_reply_only_preparation")
    if turn in {5, 6} and "snake_env_demo" not in stripped:
        issues.append("project_maintenance_target_missing")
    if turn >= 2 and any(marker in stripped for marker in ("pytest", "FAILED", "KeyError")):
        if any(marker in lower for marker in (
            "want me to continue",
            "要我接着",
            "needs to be fixed",
            "需要先修",
            "still need",
            "还需要",
            "verification failed",
            "验证没通过",
            "continue fixing",
            "继续修",
        )):
            issues.append("implementation_validation_failed_needs_continue")
    return {"ok": not issues, "issues": issues}

def evaluate_runtime_quality(result: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if not result.get("ok", False):
        issues.append("request_failed")
    if result.get("latency_sec", 0) and float(result.get("latency_sec") or 0) > 180:
        issues.append("slow_turn_over_180s")
    errors = result.get("errors") or []
    if errors:
        issues.append("sse_error_event")
    return issues


def validate_clone(project: Path) -> dict[str, Any]:
    app = project / "app"
    py_files = [p for p in app.rglob("*.py") if "__pycache__" not in p.parts]
    top = sorted(
        ((p.stat().st_size, p.relative_to(app).as_posix()) for p in py_files),
        reverse=True,
    )[:8]
    return {
        "exists": app.is_dir(),
        "py_count": len(py_files),
        "has_delegate": (app / "llm" / "tools" / "delegate.py").is_file(),
        "has_environment": (app / "llm" / "tools" / "environment.py").is_file(),
        "app_top_dirs": sorted(p.name for p in app.iterdir() if p.is_dir() and p.name != "__pycache__") if app.is_dir() else [],
        "top": top,
    }


TASKS = [
    (
        "Count the 12 largest Python files under `app/`. Use a full recursive traversal, not a truncated "
        "directory tree. Explain how you verified the result was not based on a truncated `env_list_tree`, "
        "and say what should happen whenever `env_list_tree` reports `truncated=true`."
    ),
    (
        "Perform a read-only architecture review of this app backup. Identify the 3 large files that are "
        "the best split candidates and explain the split risks. Read real project file evidence; do not "
        "guess from file names. Do not modify files."
    ),
    (
        "Make one low-risk improvement in the backup project: add or improve tests around environment-tool "
        "behavior so truncated directory trees cannot be used for file-size rankings, and multi-line Python "
        "statistical scripts must produce real output. Modify only this backup project and run the relevant pytest."
    ),
    (
        "Continue from the previous maintenance result. Check whether the new tests cover the just-found "
        "failure path. If coverage is missing, add it. Finish with changed files, verification commands, "
        "and remaining risks."
    ),
    (
        "Use toolchain continuation ability: treat the previous toolchain as evidence and continue toward "
        "a small refactor plan. Rank items by risk, benefit, and verification cost, and say which parts "
        "need helper parallel reading. Report only verified facts and decisions, not internal cache mechanics."
    ),
    (
        "Inside this backup project, create an independent `examples/snake_env_demo/` mini project with at "
        "least 8 source or asset files. Implement a browser-openable Snake web game. Plan the file structure, "
        "write files, then run a static check or self-check script."
    ),
    (
        "Continue maintaining `examples/snake_env_demo/`: add one new game mode and a readable developer "
        "note. Reuse the existing directory from the previous turn; do not start a second project. Finish "
        "with changed files and verification results."
    ),
    (
        "Do a data-analysis style task in the backup project: create a small CSV dataset under `data/`, "
        "write a Python analysis script that outputs statistical conclusions, and then create a report that "
        "summarizes those computed results. Complex calculations should be done by code, not by report prose."
    ),
    (
        "Inspect this backup project for temporary scripts, caches, mojibake files, or leftover test debris "
        "created by this run. Clean only disposable files inside this test workspace. Do not delete real "
        "project files."
    ),
]



async def run(args: argparse.Namespace) -> Path:
    run_id = now_id()
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project = copy_app_clone(run_dir)
    base_url = f"http://127.0.0.1:{args.port}"
    proc = None
    client = AppCloneClient(base_url, run_dir)
    try:
        if args.start_service:
            proc = start_service(run_dir, args.port)
        await wait_health(base_url, args.health_timeout_sec)
        await client.record({
            "kind": "run_start",
            "run_id": run_id,
            "project": str(project),
            "duration_min": args.duration_min,
            "validation": validate_clone(project),
        })
        end_at = time.monotonic() + args.duration_min * 60
        turn = 0
        while time.monotonic() < end_at:
            if turn >= len(TASKS):
                await asyncio.sleep(args.idle_after_tasks_sec)
                break
            message = TASKS[turn]
            turn_started = time.monotonic()
            recent_context = ""
            continuation_count = 0
            await client.record({"kind": "supervisor_request", "turn": turn, "message": message})
            while True:
                result = await client.ask_environment(project=project, message=message, turn=turn)
                validation_after = validate_clone(project)
                quality = evaluate_response_quality(turn, result.get("text") or "", validation_after)
                runtime_issues = evaluate_runtime_quality(result)
                if runtime_issues:
                    quality["ok"] = False
                    quality["issues"].extend(runtime_issues)
                await client.record({
                    "kind": "environment_call",
                    "turn": turn,
                    "message": message,
                    "continuation_count": continuation_count,
                    "validation_after": validation_after,
                    "quality": quality,
                    **result,
                })
                if quality.get("ok", True):
                    break
                if not args.auto_continue or continuation_count >= args.max_auto_continue_turns:
                    if args.stop_on_quality_issue:
                        await client.record({
                            "kind": "quality_stop",
                            "turn": turn,
                            "issues": quality.get("issues", []),
                        })
                        await write_summary(run_dir, project)
                        return run_dir
                    break
                decision = await client.auto_continue_check(
                    user_message=message,
                    assistant_reply=result.get("text") or "",
                    recent_context=recent_context,
                    elapsed_sec=time.monotonic() - turn_started,
                    max_sec=args.max_auto_continue_sec,
                )
                await client.record({
                    "kind": "auto_continue_decision",
                    "turn": turn,
                    "continuation_count": continuation_count,
                    "decision": decision,
                    "quality_issues": quality.get("issues", []),
                })
                if not decision.get("should_continue"):
                    if args.stop_on_quality_issue:
                        await client.record({
                            "kind": "quality_stop",
                            "turn": turn,
                            "issues": quality.get("issues", []),
                            "auto_continue": decision,
                        })
                        await write_summary(run_dir, project)
                        return run_dir
                    break
                continuation_count += 1
                recent_context = (recent_context + "\n\n" + (result.get("text") or ""))[-12000:]
                message = str(decision.get("continue_message") or "Continue.").strip() or "Continue."
                if "implementation_validation_failed_needs_continue" in quality.get("issues", []):
                    message = ("Continue fixing the validation failure from the previous turn. Rerun the relevant pytest command until it passes, or provide a specific blocker that cannot be fixed.")
                await client.record({
                    "kind": "supervisor_auto_continue",
                    "turn": turn,
                    "continuation_count": continuation_count,
                    "message": message,
                })
            turn += 1
            await asyncio.sleep(args.review_gap_sec)
        await write_summary(run_dir, project)
    finally:
        await client.close()
        if args.start_service:
            stop_service(proc)
    return run_dir


async def write_summary(run_dir: Path, project: Path) -> None:
    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    calls = [e for e in events if e.get("kind") == "environment_call"]
    errors = [e for e in events if e.get("ok") is False or e.get("errors")]
    quality_issues = [
        {"turn": e.get("turn"), **(e.get("quality") or {})}
        for e in calls
        if not (e.get("quality") or {}).get("ok", True)
    ]
    summary = {
        "events": len(events),
        "calls": len(calls),
        "errors": len(errors),
        "quality_issue_count": len(quality_issues),
        "quality_issues": quality_issues,
        "project": str(project),
        "validation": validate_clone(project),
        "latency_sec": [e.get("latency_sec") for e in calls],
        "event_counts": [e.get("event_counts") for e in calls],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# App Clone Maintenance Stress Report",
        "",
        f"- Project: `{project}`",
        f"- Calls: {summary['calls']}",
        f"- Errors: {summary['errors']}",
        f"- Quality issues: {summary['quality_issue_count']}",
        f"- Validation: `{json.dumps(summary['validation'], ensure_ascii=False)}`",
        "",
        "## Calls",
        "",
    ]
    for e in calls:
        text = str(e.get("text") or "").replace("\n", " ")
        quality = e.get("quality") or {}
        lines.append(
            f"- turn={e.get('turn')} ok={e.get('ok')} latency={e.get('latency_sec')}s "
            f"quality={quality.get('ok', True)} issues={quality.get('issues', [])} "
            f"text={text[:500]}"
        )
    if errors:
        lines.extend(["", "## Errors", ""])
        for e in errors:
            lines.append(f"- `{e.get('ts')}` {str(e.get('error') or e.get('errors') or e)[:800]}")
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--duration-min", type=float, default=60.0)
    p.add_argument("--port", type=int, default=8023)
    p.add_argument("--start-service", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--health-timeout-sec", type=float, default=90.0)
    p.add_argument("--review-gap-sec", type=float, default=15.0)
    p.add_argument("--idle-after-tasks-sec", type=float, default=5.0)
    p.add_argument("--stop-on-quality-issue", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--auto-continue", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--max-auto-continue-turns", type=int, default=4)
    p.add_argument("--max-auto-continue-sec", type=float, default=900.0)
    return p.parse_args()


if __name__ == "__main__":
    run_dir = asyncio.run(run(parse_args()))
    print(run_dir)


