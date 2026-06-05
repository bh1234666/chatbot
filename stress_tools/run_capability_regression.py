from __future__ import annotations

import argparse
import asyncio
import codecs
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.llm.tools.delegate_quality import _mojibake_score

RUNS_DIR = PROJECT_ROOT / "stress_tools" / "runs" / "capability_matrix"


def now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


async def iter_utf8_sse_lines(response: httpx.Response):
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
            pending = pending[marker + 1 :]
            yield line.rstrip("\r")
    tail = pending + decoder.decode(b"", final=True)
    if tail:
        yield tail.rstrip("\r")


@dataclass
class Recorder:
    run_dir: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def record(self, event: dict[str, Any]) -> None:
        event = {"ts": now_iso(), **event}
        async with self.lock:
            with (self.run_dir / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")


class EnvClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(3600.0, connect=20.0), trust_env=False)

    async def close(self) -> None:
        await self.client.aclose()

    async def health(self) -> bool:
        try:
            r = await self.client.get(f"{self.base_url}/health")
            return r.status_code == 200
        except Exception:
            return False

    async def ask(self, *, user_id: str, current_dir: Path, message: str, label: str, turn: int) -> dict[str, Any]:
        payload = {
            "user_id": user_id,
            "user_name": user_id,
            "message": message,
            "current_dir": str(current_dir),
            "client_msg_id": f"{label}_{turn}_{uuid.uuid4().hex[:8]}",
        }
        started = time.monotonic()
        tokens: list[str] = []
        buckets: dict[str, list[Any]] = {
            "progress": [],
            "workflow": [],
            "command": [],
            "error": [],
            "done": [],
            "meta": [],
        }
        try:
            async with self.client.stream("POST", f"{self.base_url}/v1/environment/stream", json=payload) as r:
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
                async for line in iter_utf8_sse_lines(r):
                    if line == "":
                        if data_lines:
                            raw = "\n".join(data_lines)
                            try:
                                data = json.loads(raw)
                            except Exception:
                                data = {"raw": raw}
                            if current_event == "token":
                                tokens.append(str(data.get("text") or ""))
                            elif current_event in buckets:
                                buckets[current_event].append(data)
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
            "ok": not buckets["error"],
            "status_code": 200,
            "latency_sec": round(time.monotonic() - started, 3),
            "text": "".join(tokens).strip(),
            "event_counts": {k: len(v) for k, v in buckets.items()},
            "errors": buckets["error"],
            "done": buckets["done"][-1] if buckets["done"] else {},
            "meta": buckets["meta"][-1] if buckets["meta"] else {},
            "workflow_tail": buckets["workflow"][-20:],
            "progress_tail": buckets["progress"][-20:],
        }


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache", ".git")
    shutil.copytree(src, dst, ignore=ignore)


def find_ielts_source() -> Path | None:
    candidates = [
        PROJECT_ROOT / "5月雅思",
        PROJECT_ROOT / "五月雅思",
        PROJECT_ROOT / "del" / "cleanup_20260601_110342" / "root" / "5月雅思",
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate
    del_root = PROJECT_ROOT / "del"
    if del_root.exists():
        matches = [
            p for p in del_root.rglob("*")
            if p.is_dir() and p.name in {"5月雅思", "五月雅思"}
        ]
        if matches:
            return sorted(matches, key=lambda p: p.stat().st_mtime, reverse=True)[0]
    return None


def find_engineering_source() -> Path | None:
    candidates = [
        PROJECT_ROOT / "电子231工程管理",
        PROJECT_ROOT / "del" / "cleanup_20260601_110342" / "root" / "电子231工程管理",
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate
    del_root = PROJECT_ROOT / "del"
    if del_root.exists():
        matches = [
            p for p in del_root.rglob("*")
            if p.is_dir() and p.name == "电子231工程管理"
        ]
        if matches:
            return sorted(matches, key=lambda p: p.stat().st_mtime, reverse=True)[0]
    return None


def setup_projects(run_dir: Path) -> dict[str, Path]:
    projects = run_dir / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    app_clone = projects / "chatbot_app_clone"
    copy_tree(PROJECT_ROOT / "app", app_clone / "app")
    for rel in ("pyproject.toml", "pytest.ini", "requirements.txt", "requirements-dev.txt", ".env.example"):
        src = PROJECT_ROOT / rel
        if src.exists():
            shutil.copy2(src, app_clone / rel)
    (app_clone / "CLONE_NOTICE.md").write_text(
        "Isolated app clone for capability regression. Do not modify F:/chatbot/app from this project.\n",
        encoding="utf-8",
    )

    ielts_src = find_ielts_source()
    ielts_clone = projects / "ielts_may_materials"
    if ielts_src is not None:
        copy_tree(ielts_src, ielts_clone)
    else:
        ielts_clone.mkdir(parents=True, exist_ok=True)
        (ielts_clone / "MISSING_SOURCE.txt").write_text("F:/chatbot/5月雅思 was not found.\n", encoding="utf-8")

    engineering_src = find_engineering_source()
    engineering_clone = projects / "engineering_management"
    if engineering_src is not None:
        copy_tree(engineering_src, engineering_clone)
    else:
        engineering_clone.mkdir(parents=True, exist_ok=True)
        (engineering_clone / "MISSING_SOURCE.txt").write_text(
            "F:/chatbot/电子231工程管理 was not found.\n",
            encoding="utf-8",
        )

    greenfield = projects / "greenfield_mixed_agent"
    if greenfield.exists():
        shutil.rmtree(greenfield)
    greenfield.mkdir(parents=True, exist_ok=True)
    return {"app": app_clone, "ielts": ielts_clone, "engineering": engineering_clone, "greenfield": greenfield}


def static_inventory(root: Path) -> dict[str, Any]:
    files = [p for p in root.rglob("*") if p.is_file()]
    by_ext: dict[str, int] = {}
    total = 0
    for p in files:
        total += p.stat().st_size
        by_ext[p.suffix.lower() or "<none>"] = by_ext.get(p.suffix.lower() or "<none>", 0) + 1
    largest = sorted(
        ((p.stat().st_size, str(p.relative_to(root)).replace("\\", "/")) for p in files),
        reverse=True,
    )[:12]
    return {"file_count": len(files), "bytes": total, "by_ext": by_ext, "largest": largest}


MOJIBAKE_MARKERS = (
    "\u6d94",
    "\u935a",
    "\u9354",
    "\u9366",
    "\u5bf0",
    "\ue1bb",
    "\u20ac",
    "\ufffd",
)

LATIN_MOJIBAKE_RE = re.compile("[\u00c3\u00c2\u00c4\u00c5\u00c6\u00c7\u00c8\u00c9\u00ca\u00cb\u00cc\u00cd\u00ce\u00cf""\u00d0\u00d1\u00d2\u00d3\u00d4\u00d5\u00d6\u00d8\u00d9\u00da\u00db\u00dc\u00dd\u00de\u00df""\u00e0\u00e1\u00e2\u00e3\u00e4\u00e5\u00e6\u00e7\u00e8\u00e9\u00ea\u00eb\u00ec\u00ed\u00ee\u00ef""\u00f0\u00f1\u00f2\u00f3\u00f4\u00f5\u00f6\u00f8\u00f9\u00fa\u00fb\u00fc\u00fd\u00fe\u00ff""\u00b3\u00b5\u00bc\u00bd\u00be\u00bf]{4,}")


def looks_like_mojibake(text: str) -> bool:
    if any(marker in text for marker in MOJIBAKE_MARKERS):
        return True
    if not text:
        return False
    if _mojibake_score(text) >= 3:
        return True
    latin_runs = LATIN_MOJIBAKE_RE.findall(text)
    latin_suspicious = sum(len(run) for run in latin_runs)
    latin_letters = sum(1 for ch in text if ch.isalpha() and ord(ch) < 256)
    if latin_suspicious >= 8 and latin_suspicious / max(latin_letters, 1) >= 0.35:
        return True
    bopomofo_or_radicals = sum(
        1
        for ch in text
        if (
            "\u3100" <= ch <= "\u312f"
            or "\u31a0" <= ch <= "\u31bf"
            or "\u2e80" <= ch <= "\u2eff"
            or "\u2f00" <= ch <= "\u2fdf"
        )
    )
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk >= 12 and bopomofo_or_radicals >= 4 and bopomofo_or_radicals / max(cjk, 1) >= 0.04


def run_command(cwd: Path, command: list[str], timeout: float = 90.0) -> dict[str, Any]:
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
    }
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def validate_app_clone(root: Path) -> dict[str, Any]:
    app = root / "app"
    py_files = [p for p in app.rglob("*.py") if "__pycache__" not in p.parts]
    top = sorted(((p.stat().st_size, p.relative_to(root).as_posix()) for p in py_files), reverse=True)[:12]
    compile_result = run_command(root, [sys.executable, "-m", "compileall", "-q", "app"], timeout=120)
    helper_kinds = app / "llm" / "tools" / "helper_kinds.py"
    helper_kinds_text = helper_kinds.read_text(encoding="utf-8", errors="replace") if helper_kinds.exists() else ""
    return {
        "exists": app.is_dir(),
        "py_count": len(py_files),
        "top_py_files": top,
        "has_read_helper_kind": '"read"' in helper_kinds_text or "'read'" in helper_kinds_text,
        "compile": compile_result,
    }


def validate_ielts(root: Path) -> dict[str, Any]:
    inv = static_inventory(root)
    reports = list(root.rglob("*ielts*report*.md")) + list(root.rglob("*index*.md")) + list(root.rglob("*报告*.md"))
    files = [p for p in root.rglob("*") if p.is_file()]
    mojibake_paths = [
        str(p.relative_to(root)).replace("\\", "/")
        for p in files
        if looks_like_mojibake(str(p.relative_to(root)).replace("\\", "/"))
    ]
    return {
        "inventory": inv,
        "report_files": [str(p.relative_to(root)).replace("\\", "/") for p in reports[:20]],
        "mojibake_path_count": len(mojibake_paths),
        "mojibake_path_samples": mojibake_paths[:20],
    }


def validate_engineering(root: Path) -> dict[str, Any]:
    inv = static_inventory(root)
    outputs = root / "analysis_outputs"
    report_patterns = ("*工程*.md", "*管理*.md", "*report*.md", "*index*.md", "*summary*.md")
    reports: list[Path] = []
    for pattern in report_patterns:
        reports.extend(root.rglob(pattern))
    files = [p for p in root.rglob("*") if p.is_file()]
    return {
        "inventory": inv,
        "analysis_outputs": static_inventory(outputs) if outputs.exists() else {"exists": False},
        "report_files": sorted({str(p.relative_to(root)).replace("\\", "/") for p in reports})[:30],
        "office_like_count": sum(1 for p in files if p.suffix.lower() in {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf"}),
    }


def validate_greenfield(root: Path) -> dict[str, Any]:
    inv = static_inventory(root)
    check_candidates = [
        root / "scripts" / "check_project.py",
        root / "check_project.py",
        root / "tools" / "check_project.py",
    ]
    check_result = {"attempted": False}
    for script in check_candidates:
        if script.exists():
            check_result = run_command(root, [sys.executable, str(script.relative_to(root))], timeout=180)
            stderr = str(check_result.get("stderr") or "")
            stdout = str(check_result.get("stdout") or "")
            missing = re.findall(r"ModuleNotFoundError: No module named '([^']+)'", stdout + "\n" + stderr)
            if missing:
                check_result["environment_issue"] = "missing_python_dependency"
                check_result["missing_dependencies"] = sorted(set(missing))
            native_dependency_issue = (
                not check_result.get("ok")
                and re.search(r"(neither\s+make\s+nor\s+gcc|gcc.*not\s+found|make.*not\s+found|compiler.*not\s+found)", stdout + "\n" + stderr, re.I)
                and any((root / "native").glob("*.exe"))
            )
            if native_dependency_issue:
                check_result["environment_issue"] = "missing_native_compiler_but_existing_binary_verified"
                check_result["ok"] = True
            break
    py_compile = run_command(root, [sys.executable, "-m", "compileall", "-q", "."], timeout=120)
    return {"inventory": inv, "check_result": check_result, "compileall": py_compile}


INTERNAL_LEAK_RE = re.compile(
    r"(_env/|_delegate_|tool_call|DSML|round[123]|trace_id|archive_id|group_id|env_[a-z_]+|workspace\.(?:mkdir|write|run|locate|read|edit|search))",
    re.I,
)


def _allows_internal_terms(text: str) -> bool:
    lowered = (text or "").lower()
    # Some regression tasks explicitly ask the agent to inspect helper,
    # delegation, OCR, and toolchain implementation files. In those replies,
    # terms such as helper, delegate, round, and call-site names are source-code
    # evidence rather than user-facing implementation leakage.
    return (
        "helper" in lowered
        and any(marker in lowered for marker in ("source", "code", "file", "call", "boundary", "verification"))
    )


def response_issues(text: str, *, must_reference_files: bool = False) -> list[str]:
    issues: list[str] = []
    stripped = (text or "").strip()
    if not stripped:
        issues.append("empty_reply")
    if "\ufffd" in stripped:
        issues.append("replacement_character")
    if looks_like_mojibake(stripped):
        issues.append("mojibake_text")
    if INTERNAL_LEAK_RE.search(stripped) and not _allows_internal_terms(stripped):
        issues.append("internal_leak_marker")
    if must_reference_files and not re.search(r"[\w\u4e00-\u9fff][./\\][\w\u4e00-\u9fff]|`[^`]+\\.[a-z0-9]+`", stripped, re.I):
        issues.append("no_file_evidence_in_reply")
    planning_only_markers = ("I will first", "Let me first", "先看", "先检查", "接下来我会", "我会先")
    evidence_markers = ("已", "created", "modified", "verified", "验证", "运行", "pytest", "compile", "report")
    if any(m in stripped for m in planning_only_markers) and not any(m in stripped for m in evidence_markers):
        issues.append("planning_only_reply")
    return issues


APP_TASKS = [
    "Read the isolated app clone's `app/` directory with a full recursive traversal. Count Python files under `app/` only, list the 12 largest `app/` Python files by byte size, and explain how you avoided relying on a truncated directory tree. Report only verified facts.",
    "Perform a maintenance review of helper boundaries in this app clone. Focus on readhelper/edithelper/codehelper separation, image/OCR/file-reading behavior, and helper result verification. Use real files as evidence and propose concrete low-risk improvements.",
    "Make one low-risk improvement in this clone that improves verification or tests around helper/result consistency. Run the narrowest relevant tests or compile checks and report changed files plus command output summary.",
]

IELTS_TASKS = [
    "Inventory all materials in this directory. Separate text, image, audio, PDF, Word, zip, and other files. Produce `analysis_outputs/ielts_material_index.md` with counts, notable filenames, and what can be read directly versus what needs OCR/audio transcription.",
    "Using the available readable materials, write `analysis_outputs/ielts_learning_report.md`: summarize topics, tasks, writing/speaking/listening/reading coverage, missing evidence, and contradictions. Read file contents rather than guessing from names.",
]

GREENFIELD_TASKS = [
    "This directory is empty. Build a complete small local agent project from scratch with mixed languages: Python backend/core, JavaScript browser UI, a small C or C++ native utility, tests, docs, fixtures, and a `scripts/check_project.py` self-check. Keep it dependency-light and runnable locally.",
    "Continue the project. Add a second capability that requires multiple files to cooperate, improve tests, run the self-check, and document architecture and verification. Do not restart from scratch.",
]


async def scenario_loop(
    *,
    label: str,
    user_id: str,
    root: Path,
    tasks: list[str],
    validate,
    client: EnvClient,
    recorder: Recorder,
    end_at: float,
    gap_sec: float,
) -> None:
    turn = 0
    while time.monotonic() < end_at and turn < len(tasks):
        message = tasks[turn]
        before = validate(root)
        await recorder.record({"kind": "scenario_request", "label": label, "turn": turn, "root": str(root), "before": before, "message": message})
        result = await client.ask(user_id=user_id, current_dir=root, message=message, label=label, turn=turn)
        after = validate(root)
        issues = response_issues(result.get("text") or "", must_reference_files=label != "greenfield")
        await recorder.record({
            "kind": "scenario_result",
            "label": label,
            "turn": turn,
            "root": str(root),
            "after": after,
            "issues": issues,
            **result,
        })
        turn += 1
        await asyncio.sleep(gap_sec)


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


async def write_report(run_dir: Path, projects: dict[str, Path]) -> None:
    events_path = run_dir / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()] if events_path.exists() else []
    results = [e for e in events if e.get("kind") == "scenario_result"]
    summary = {
        "run_dir": str(run_dir),
        "events": len(events),
        "results": len(results),
        "issue_results": sum(1 for e in results if e.get("issues") or not e.get("ok")),
        "projects": {k: str(v) for k, v in projects.items()},
        "final_validation": {
            "app": validate_app_clone(projects["app"]),
            "ielts": validate_ielts(projects["ielts"]),
            "greenfield": validate_greenfield(projects["greenfield"]),
        },
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Capability Regression Report",
        "",
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
            f"- `{e.get('label')}` turn={e.get('turn')} ok={e.get('ok')} "
            f"latency={e.get('latency_sec')}s issues={e.get('issues')} text={text[:450]}"
        )
    lines.extend(["", "## Final Validation", "", "```json", json.dumps(summary["final_validation"], ensure_ascii=False, indent=2), "```"])
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


async def run(args: argparse.Namespace) -> Path:
    run_dir = RUNS_DIR / now_id()
    run_dir.mkdir(parents=True, exist_ok=True)
    projects = setup_projects(run_dir)
    recorder = Recorder(run_dir)
    client = EnvClient(args.base_url)
    if not await client.health():
        raise RuntimeError(f"backend not healthy: {args.base_url}")
    await recorder.record({
        "kind": "run_start",
        "base_url": args.base_url,
        "duration_min": args.duration_min,
        "projects": {k: str(v) for k, v in projects.items()},
        "initial_inventory": {k: static_inventory(v) for k, v in projects.items()},
    })
    stop = asyncio.Event()
    monitor = asyncio.create_task(monitor_loop(client, recorder, stop, args.monitor_interval_sec))
    end_at = time.monotonic() + args.duration_min * 60
    tasks = [
        asyncio.create_task(scenario_loop(label="app", user_id="cap_app", root=projects["app"], tasks=APP_TASKS, validate=validate_app_clone, client=client, recorder=recorder, end_at=end_at, gap_sec=args.gap_sec)),
        asyncio.create_task(scenario_loop(label="ielts", user_id="cap_ielts", root=projects["ielts"], tasks=IELTS_TASKS, validate=validate_ielts, client=client, recorder=recorder, end_at=end_at, gap_sec=args.gap_sec)),
        asyncio.create_task(scenario_loop(label="greenfield", user_id="cap_greenfield", root=projects["greenfield"], tasks=GREENFIELD_TASKS, validate=validate_greenfield, client=client, recorder=recorder, end_at=end_at, gap_sec=args.gap_sec)),
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
        await write_report(run_dir, projects)
    return run_dir


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://127.0.0.1:8031")
    p.add_argument("--duration-min", type=float, default=300.0)
    p.add_argument("--gap-sec", type=float, default=20.0)
    p.add_argument("--monitor-interval-sec", type=float, default=30.0)
    p.add_argument("--drain-sec", type=float, default=1800.0)
    return p.parse_args()


if __name__ == "__main__":
    print(asyncio.run(run(parse_args())))
