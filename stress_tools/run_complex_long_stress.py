from __future__ import annotations

import argparse
import asyncio
import codecs
import csv
import io
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
RUNS_DIR = ROOT / "runs" / "complex_long"
_HTTP_5XX_RE = re.compile(r"(?:HTTP\s+|status=|status_code=)(5\d\d)\b|HTTPStatusError[^\n]*(5\d\d)\b")


def now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def count_explicit_http_5xx(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in _HTTP_5XX_RE.finditer(text or ""):
        code = match.group(1) or match.group(2)
        if not code:
            continue
        counts[code] = counts.get(code, 0) + 1
    return counts


def start_service(run_dir: Path, host: str, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env.setdefault("DEBUG_MODE", "true")
    env.setdefault("DEBUG_LOG_DIR", str(run_dir / "debug_logs"))
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    args = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        host,
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
                body = (r.text or "")[:300].replace("\n", " ")
                last = f"status={r.status_code} body={body!r}"
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
            await asyncio.sleep(1.0)
    raise RuntimeError(f"service did not become healthy: {last}")


async def run_child(name: str, args: list[str], run_dir: Path) -> dict[str, Any]:
    started = time.monotonic()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await proc.communicate()
    except asyncio.CancelledError:
        await terminate_child(proc)
        raise
    (run_dir / f"{name}.out").write_bytes(stdout_b)
    (run_dir / f"{name}.err").write_bytes(stderr_b)
    return {
        "name": name,
        "returncode": proc.returncode,
        "elapsed_sec": round(time.monotonic() - started, 3),
        "stdout_tail": stdout_b.decode("utf-8", errors="replace")[-4000:],
        "stderr_tail": stderr_b.decode("utf-8", errors="replace")[-4000:],
    }


async def terminate_child(proc: asyncio.subprocess.Process, timeout_sec: float = 20.0) -> None:
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=timeout_sec)
        else:
            proc.terminate()
    except Exception:
        try:
            proc.kill()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


class Recorder:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.events_path = run_dir / "direct_events.jsonl"
        self._lock = asyncio.Lock()

    async def record(self, event: dict[str, Any]) -> None:
        event = {"ts": now_iso(), **event}
        async with self._lock:
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")


class DirectClient:
    def __init__(self, base_url: str, recorder: Recorder):
        self.base_url = base_url.rstrip("/")
        self.recorder = recorder
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(2400.0, connect=20.0), trust_env=False)

    async def close(self) -> None:
        await self.client.aclose()

    async def create_archive_group(self) -> tuple[str, str]:
        r = await self.client.post(
            f"{self.base_url}/v1/archives",
            json={"name": f"complex long stress {now_iso()}", "persona_id": ""},
        )
        r.raise_for_status()
        archive_id = r.json()["archive_id"]
        group_id = "complex_group_" + uuid.uuid4().hex[:10]
        jr = await self.client.post(
            f"{self.base_url}/v1/bot/groups/{group_id}/join",
            json={"archive_id": archive_id, "group_name": "complex stress", "persona_label": "bot"},
        )
        jr.raise_for_status()
        return archive_id, group_id

    async def upload(self, archive_id: str, group_id: str, name: str, content: bytes, content_type: str) -> dict[str, Any]:
        r = await self.client.post(
            f"{self.base_url}/v1/chat/files/{archive_id}/{group_id}/upload",
            params={"filename": name, "user_id": "direct_tester", "user_name": "Direct Tester"},
            content=content,
            headers={"content-type": content_type},
        )
        if r.status_code >= 400:
            return {"ok": False, "status_code": r.status_code, "error": r.text[:1000]}
        return r.json()

    async def list_files(self, archive_id: str, group_id: str) -> dict[str, Any]:
        r = await self.client.get(f"{self.base_url}/v1/chat/files/{archive_id}/{group_id}")
        return r.json()

    async def list_artifacts(self, archive_id: str, group_id: str) -> dict[str, Any]:
        r = await self.client.get(f"{self.base_url}/v1/chat/artifacts/{archive_id}/{group_id}")
        return r.json()

    async def abort(self, archive_id: str, group_id: str, user_id: str) -> dict[str, Any]:
        r = await self.client.post(
            f"{self.base_url}/v1/chat/abort",
            json={"archive_id": archive_id, "group_id": group_id, "user_id": user_id},
        )
        try:
            return r.json()
        except Exception:
            return {"status_code": r.status_code, "text": r.text[:1000]}

    async def abort_environment(self, user_id: str, current_dir: Path, project_id: str = "") -> dict[str, Any]:
        r = await self.client.post(
            f"{self.base_url}/v1/environment/abort",
            json={
                "user_id": user_id,
                "current_dir": str(current_dir),
                "project_id": project_id,
            },
        )
        try:
            return r.json()
        except Exception:
            return {"status_code": r.status_code, "text": r.text[:1000]}

    async def chat(self, payload: dict[str, Any], *, environment: bool = False) -> dict[str, Any]:
        started = time.monotonic()
        endpoint = "/v1/environment/stream" if environment else "/v1/chat/stream"
        tokens: list[str] = []
        events: dict[str, list[Any]] = {"progress": [], "workflow": [], "command": [], "error": [], "done": [], "meta": []}
        try:
            async with self.client.stream("POST", f"{self.base_url}{endpoint}", json=payload) as r:
                r.encoding = "utf-8"
                if r.status_code >= 400:
                    body = await r.aread()
                    return {
                        "ok": False,
                        "status_code": r.status_code,
                        "latency_sec": round(time.monotonic() - started, 3),
                        "error": body.decode("utf-8", errors="replace")[:2000],
                    }
                current_event = "message"
                data_lines: list[str] = []
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                pending = ""

                async def _utf8_lines():
                    nonlocal pending
                    async for chunk in r.aiter_bytes():
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

                async for line in _utf8_lines():
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
        except Exception as e:
            return {
                "ok": False,
                "status_code": 0,
                "latency_sec": round(time.monotonic() - started, 3),
                "error": f"{type(e).__name__}: {e}",
            }
        text = "".join(tokens).strip()
        event_counts = {k: len(v) for k, v in events.items()}
        done_payload = events["done"][-1] if events["done"] else {}
        files = done_payload.get("files") if isinstance(done_payload, dict) else []
        has_visible_result = bool(text or files or events["workflow"] or events["command"])
        if not has_visible_result:
            events["error"].append({
                "kind": "empty_response",
                "message": "SSE completed without token text, files, workflow, or command events",
            })
        return {
            "ok": not events["error"],
            "status_code": 200,
            "latency_sec": round(time.monotonic() - started, 3),
            "text": text,
            "event_counts": event_counts,
            "errors": events["error"],
            "done": done_payload,
            "meta": events["meta"][-1] if events["meta"] else {},
        }


def make_direct_project(root: Path) -> Path:
    project = root / "complex_direct_project"
    if project.exists():
        return project
    (project / "src" / "algolab").mkdir(parents=True, exist_ok=True)
    (project / "tests").mkdir(parents=True, exist_ok=True)
    (project / "docs").mkdir(parents=True, exist_ok=True)
    (project / "data").mkdir(parents=True, exist_ok=True)
    files = {
        "README.md": "# AlgoLab\n\nA scaffold for graph algorithms, benchmark reports, and data validation.\n",
        "pyproject.toml": (
            "[project]\n"
            "name='algolab-stress'\n"
            "version='0.1.0'\n"
            "requires-python='>=3.10'\n\n"
            "[tool.pytest.ini_options]\n"
            "pythonpath=['src']\n"
            "testpaths=['tests']\n"
        ),
        "src/algolab/__init__.py": "__version__ = '0.1.0'\n",
        "src/algolab/graph.py": "def shortest_path(graph, start, goal):\n    return []\n",
        "src/algolab/benchmark.py": "def compare():\n    return {}\n",
        "src/algolab/report.py": "def render_report(data):\n    return '# Report\\n'\n",
        "tests/test_graph.py": "def test_placeholder():\n    assert True\n",
        "docs/spec.md": "# Spec\n\nImplement weighted graph algorithms and benchmark methodology.\n",
        "data/edges.csv": "src,dst,weight\nA,B,4\nA,C,2\nC,B,1\nB,D,5\nC,D,8\n",
    }
    for rel, text in files.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return project


def csv_bytes(rows: int = 120) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["region", "month", "revenue", "cost", "incidents"])
    for i in range(rows):
        writer.writerow([
            f"R{i % 8}",
            f"2026-{(i % 12) + 1:02d}",
            1000 + (i * 37) % 9000,
            450 + (i * 29) % 3000,
            (i * 7) % 11,
        ])
    return buf.getvalue().encode("utf-8")


def html_bytes() -> bytes:
    return (
        "<!doctype html><meta charset='utf-8'><title>Sort Lab</title>"
        "<script>function bubbleSort(a){return a.slice().sort((x,y)=>x-y)}"
        "function benchmark(){return ['bubble','merge','quick']}</script>"
        "<body><h1>Sort Lab</h1><p>Compare algorithms.</p></body>"
    ).encode("utf-8")


def validate_direct_project(project: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    env = os.environ.copy()
    src = str((project / "src").resolve())
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    for name, cmd, timeout in (
        ("compileall", [sys.executable, "-m", "compileall", "src", "tests"], 60),
        ("pytest", [sys.executable, "-m", "pytest", "tests", "-q"], 120),
    ):
        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(project),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            checks.append({
                "name": name,
                "cmd": cmd,
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "latency_sec": round(time.monotonic() - started, 3),
                "stdout_tail": proc.stdout[-2000:],
                "stderr_tail": proc.stderr[-2000:],
            })
        except subprocess.TimeoutExpired as e:
            checks.append({
                "name": name,
                "cmd": cmd,
                "ok": False,
                "returncode": None,
                "latency_sec": round(time.monotonic() - started, 3),
                "error": f"timeout after {timeout}s",
                "stdout_tail": (e.stdout or "")[-2000:] if isinstance(e.stdout, str) else "",
                "stderr_tail": (e.stderr or "")[-2000:] if isinstance(e.stderr, str) else "",
            })
    return {"ok": all(c.get("ok") for c in checks), "checks": checks}


DIRECT_TASKS = [
    {
        "name": "small_algorithm_probe",
        "environment": True,
        "timeout_sec": 1200,
        "message": (
            "Do a bounded small engineering task: implement weighted-graph Dijkstra shortest paths, add 3-5 pytest "
            "cases covering a normal path, unreachable nodes, and negative-weight rejection, update the README usage "
            "snippet, then run compile/test/smoke verification. This is a src-layout project; pytest must pass through "
            "the pyproject pythonpath setting or an explicit PYTHONPATH=src verification. Do not duplicate test files or "
            "turn tests into conftest.py to bypass imports. Do not run a large benchmark or generate a long report. If a "
            "verification command fails, fix it once before replying."
        ),
    },
    {
        "name": "complex_algorithm_report",
        "environment": True,
        "timeout_sec": 1500,
        "message": (
            "In this project, implement a more complex algorithm task: complete Dijkstra, A*, and Floyd-Warshall for "
            "weighted graphs; add reproducible example data, a benchmark script, pytest coverage, README usage notes, and "
            "docs/algorithm_report.md. Compare complexity, appropriate use cases, and boundary conditions. Run compile, "
            "tests, and a smoke command; if verification fails, repair and rerun before replying."
        ),
    },
    {
        "name": "attached_data_long_report",
        "environment": False,
        "timeout_sec": 1200,
        "message": (
            "Read the uploaded CSV and HTML materials and do a complex analysis task: compare experimental designs for "
            "three sorting algorithms, use the CSV to produce a long report with data summary, chart recommendations, "
            "risks, and verification steps, preferably producing markdown or docx. If computation is needed, use a code "
            "helper; an edit/report helper should only write the final prose from computed results."
        ),
    },
    {
        "name": "multi_artifact_office",
        "environment": False,
        "timeout_sec": 1200,
        "message": (
            "Based on the uploaded data, create a management deliverable set: a detailed analysis report, a short executive "
            "summary, and a reusable CSV/JSON result file. Do not write placeholder chart sections; if chart resources are "
            "missing, request or generate the needed resource before integrating it."
        ),
    },
    {
        "name": "retry_stuck_probe",
        "environment": True,
        "timeout_sec": 1200,
        "message": (
            "Inspect graph.py and benchmark.py in the project, find all obvious incompleteness, and fix it. If a helper "
            "stalls or fails, use the available continuation/escalation path or restart the failed work item instead of "
            "replying from the failure alone."
        ),
    },
    {
        "name": "internal_info_attack",
        "environment": False,
        "timeout_sec": 600,
        "message": (
            "Explain OCR, TTS, helpers, caches, and model pools as general technologies. This is a conceptual explanation "
            "request, not a request to invoke tools or expose internal implementation details."
        ),
    },
    {
        "name": "abort_recovery",
        "environment": True,
        "timeout_sec": 900,
        "message": (
            "Start a longer task: refactor this project into a maintainable algorithm lab platform, split modules, add tests, "
            "add docs, and run verification. If interrupted by a new message or abort, preserve progress and make the work "
            "recoverable."
        ),
        "abort_after_sec": 25,
    },
]

async def run_direct_tasks(
    base_url: str,
    run_dir: Path,
    duration_min: float,
    direct_gap_sec: float,
    direct_task_timeout_sec: float,
    max_turns: int | None = None,
    task_names: set[str] | None = None,
) -> Path:
    recorder = Recorder(run_dir)
    client = DirectClient(base_url, recorder)
    project_root = run_dir / "direct_projects"
    project_root.mkdir(parents=True, exist_ok=True)
    project = make_direct_project(project_root)
    archive_id, group_id = await client.create_archive_group()
    uploads = [
        await client.upload(archive_id, group_id, "finance_sample.csv", csv_bytes(), "text/csv"),
        await client.upload(archive_id, group_id, "sort_lab.html", html_bytes(), "text/html"),
        await client.upload(
            archive_id,
            group_id,
            "requirements_note.md",
            "# Requirements\n\nUse helpers appropriately. Verify results. Avoid placeholder chapters.\n".encode("utf-8"),
            "text/markdown",
        ),
    ]
    files_after_upload = await client.list_files(archive_id, group_id)
    await recorder.record({
        "kind": "direct_setup",
        "archive_id": archive_id,
        "group_id": group_id,
        "project": str(project),
        "uploads": uploads,
        "files_after_upload": files_after_upload,
    })
    attached_ids = [
        str(item.get("id") or item.get("file_id") or "")
        for item in files_after_upload.get("items", [])
        if item.get("id") or item.get("file_id")
    ]
    end_at = time.monotonic() + duration_min * 60
    tasks = [task for task in DIRECT_TASKS if not task_names or task["name"] in task_names]
    if not tasks:
        await recorder.record({
            "kind": "direct_no_matching_tasks",
            "requested_task_names": sorted(task_names or []),
            "available_task_names": [task["name"] for task in DIRECT_TASKS],
        })
        await client.close()
        return run_dir
    turn = 0
    try:
        while time.monotonic() < end_at:
            if max_turns is not None and turn >= max_turns:
                await recorder.record({"kind": "direct_max_turns_reached", "turn": turn, "max_turns": max_turns})
                break
            task = tasks[turn % len(tasks)]
            user_id = "direct_env_user" if task["environment"] else "direct_chat_user"
            payload = {
                "archive_id": archive_id,
                "group_id": group_id,
                "user_id": user_id,
                "user_name": "Direct Tester",
                "message": task["message"],
                "client_msg_id": f"direct_{turn}_{uuid.uuid4().hex[:8]}",
                "attached_file_ids": attached_ids if not task["environment"] else [],
            }
            if task["environment"]:
                payload["current_dir"] = str(project)
            await recorder.record({"kind": "direct_request", "turn": turn, "task": task["name"], "payload": payload})
            chat_task = asyncio.create_task(client.chat(payload, environment=bool(task["environment"])))
            abort_after = task.get("abort_after_sec")
            if abort_after:
                await asyncio.sleep(float(abort_after))
                if task["environment"]:
                    abort_result = await client.abort_environment(user_id=user_id, current_dir=project)
                else:
                    abort_result = await client.abort(
                        archive_id=archive_id,
                        group_id=group_id,
                        user_id=user_id,
                    )
                await recorder.record({"kind": "direct_abort", "turn": turn, "task": task["name"], "result": abort_result})
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(chat_task),
                    timeout=max(30.0, float(direct_task_timeout_sec), float(task.get("timeout_sec", 0) or 0)),
                )
            except asyncio.TimeoutError:
                timeout_used = max(float(direct_task_timeout_sec), float(task.get("timeout_sec", 0) or 0))
                if task["environment"]:
                    abort_result = await client.abort_environment(user_id=user_id, current_dir=project)
                else:
                    abort_result = await client.abort(
                        archive_id=archive_id,
                        group_id=group_id,
                        user_id=user_id,
                    )
                await recorder.record({
                    "kind": "direct_timeout_abort",
                    "turn": turn,
                    "task": task["name"],
                    "timeout_sec": timeout_used,
                    "result": abort_result,
                })
                try:
                    result = await asyncio.wait_for(asyncio.shield(chat_task), timeout=120.0)
                except asyncio.TimeoutError:
                    chat_task.cancel()
                    await asyncio.gather(chat_task, return_exceptions=True)
                    result = {
                        "ok": False,
                        "status_code": 0,
                        "latency_sec": round(timeout_used, 3),
                        "error": f"direct task timed out after {timeout_used}s and abort did not drain",
                    }
                except asyncio.CancelledError:
                    result = {
                        "ok": False,
                        "status_code": 0,
                        "latency_sec": round(timeout_used, 3),
                        "error": "direct task was cancelled while draining after abort",
                    }
            artifacts = await client.list_artifacts(archive_id, group_id)
            project_validation = validate_direct_project(project) if task["environment"] else None
            if project_validation is not None and isinstance(result, dict) and result.get("ok") and not project_validation.get("ok"):
                result = dict(result)
                result["ok"] = False
                result["error"] = "project_validation_failed"
                result["project_validation"] = project_validation
            result_files = []
            if isinstance(result, dict):
                done_payload = result.get("done") or {}
                if isinstance(done_payload, dict):
                    result_files = list(done_payload.get("files") or [])
            await recorder.record({
                "kind": "direct_result",
                "turn": turn,
                "task": task["name"],
                "result": result,
                "artifacts": artifacts,
                "result_files": result_files,
                "project_validation": project_validation,
            })
            if not result.get("ok"):
                break
            turn += 1
            await asyncio.sleep(max(1.0, direct_gap_sec + random.uniform(-2.0, 2.0)))
    finally:
        await client.close()
    return run_dir


def newest_run(base: Path) -> Path | None:
    if not base.exists():
        return None
    dirs = [p for p in base.iterdir() if p.is_dir()]
    return max(dirs, key=lambda p: p.stat().st_mtime) if dirs else None


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def summarize_direct(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "direct_events.jsonl"
    if not path.exists():
        return {}
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    results = [e for e in events if e.get("kind") == "direct_result"]
    latencies = [
        float(((e.get("result") or {}).get("latency_sec")) or 0)
        for e in results
        if ((e.get("result") or {}).get("latency_sec"))
    ]
    return {
        "events": len(events),
        "direct_results": len(results),
        "direct_ok": sum(1 for e in results if (e.get("result") or {}).get("ok")),
        "direct_fail": sum(1 for e in results if not (e.get("result") or {}).get("ok")),
        "aborts": sum(1 for e in events if e.get("kind") == "direct_abort"),
        "latency_min": min(latencies) if latencies else None,
        "latency_max": max(latencies) if latencies else None,
        "latency_mean": sum(latencies) / len(latencies) if latencies else None,
        "tasks": [
            {
                "turn": e.get("turn"),
                "task": e.get("task"),
                "ok": (e.get("result") or {}).get("ok"),
                "latency_sec": (e.get("result") or {}).get("latency_sec"),
                "event_counts": (e.get("result") or {}).get("event_counts"),
                "text_head": str((e.get("result") or {}).get("text") or "")[:300],
                "errors": (e.get("result") or {}).get("errors"),
                "result_files": e.get("result_files") or [],
                "artifact_count": len(((e.get("artifacts") or {}).get("items") or [])),
                "project_validation_ok": ((e.get("project_validation") or {}).get("ok")),
            }
            for e in results[-20:]
        ],
    }


async def run(args: argparse.Namespace) -> Path:
    run_id = now_id()
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    base_url = f"http://127.0.0.1:{args.port}"
    proc = None
    try:
        if args.start_service:
            proc = start_service(run_dir, "127.0.0.1", args.port)
        await wait_health(base_url, args.health_timeout_sec)
        duration = args.duration_min
        child_specs = [
            (
                "group_sim",
                [
                    sys.executable,
                    "group_sim/run_group_sim.py",
                    "--base-url",
                    base_url,
                    "--duration-min",
                    str(duration),
                    "--members",
                    str(args.members),
                    "--bot-prob",
                    str(args.bot_prob),
                    "--min-gap-sec",
                    str(args.group_min_gap_sec),
                    "--max-gap-sec",
                    str(args.group_max_gap_sec),
                    "--force-bot-every-sec",
                    str(args.force_bot_every_sec),
                    "--max-bot-inflight",
                    str(args.group_max_bot_inflight),
                    "--max-bot-backlog",
                    str(args.group_max_bot_backlog),
                    "--member-model",
                    str(args.group_member_model),
                    "--monitor-interval-sec",
                    str(args.monitor_interval_sec),
                    "--drain-sec",
                    str(args.group_drain_sec),
                ],
            ),
            (
                "environment_maintenance",
                [
                    sys.executable,
                    "stress_tools/run_environment_maintenance.py",
                    "--base-url",
                    base_url,
                    "--duration-min",
                    str(duration),
                    "--projects",
                    str(args.env_projects),
                    "--max-inflight",
                    str(args.env_max_inflight),
                    "--review-gap-sec",
                    str(args.env_review_gap_sec),
                    "--monitor-interval-sec",
                    str(args.monitor_interval_sec),
                    "--drain-sec",
                    str(args.env_drain_sec),
                ],
            ),
        ]
        children = [
            asyncio.create_task(run_child(name, cmd, run_dir))
            for name, cmd in child_specs
        ]
        direct = asyncio.create_task(run_direct_tasks(
            base_url,
            run_dir,
            duration,
            args.direct_gap_sec,
            args.direct_task_timeout_sec,
        ))
        expected_sec = max(60.0, duration * 60 + args.child_grace_sec)
        results: list[dict[str, Any]] = []
        try:
            results = await asyncio.wait_for(asyncio.gather(*children), timeout=expected_sec)
            await asyncio.wait_for(direct, timeout=max(60.0, args.direct_drain_sec))
        except asyncio.TimeoutError:
            for task in children + [direct]:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*children, direct, return_exceptions=True)
            results.append({"name": "timeout", "returncode": None, "error": f"timeout after {expected_sec}s"})
        group_run = newest_run(PROJECT_ROOT / "group_sim" / "runs")
        env_run = newest_run(ROOT / "runs" / "environment")
        server_err = ""
        server_err_path = run_dir / "server.err"
        if server_err_path.exists():
            server_err = server_err_path.read_text(encoding="utf-8", errors="replace")
        summary = {
            "run_id": run_id,
            "base_url": base_url,
            "duration_min": duration,
            "children": results,
            "group_run": str(group_run) if group_run else "",
            "environment_run": str(env_run) if env_run else "",
            "group_summary": load_json(group_run / "summary.json") if group_run else {},
            "environment_summary": load_json(env_run / "summary.json") if env_run else {},
            "direct_summary": summarize_direct(run_dir),
            "server_log_signals": {
                "traceback_count": server_err.count("Traceback"),
                "explicit_http_5xx": count_explicit_http_5xx(server_err),
                "raw_502_substring_count": server_err.count("502"),
                "read_error_count": server_err.count("ReadError"),
            },
            "server_returncode": proc.poll() if proc else None,
        }
        (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            "# Complex Long Stress Report",
            "",
            f"- Run: {run_id}",
            f"- Duration min: {duration}",
            f"- Group run: {summary['group_run']}",
            f"- Environment run: {summary['environment_run']}",
            "",
            "## Summary",
            "",
            "```json",
            json.dumps({
                "group": summary["group_summary"],
                "environment": summary["environment_summary"],
                "direct": summary["direct_summary"],
            }, ensure_ascii=False, indent=2),
            "```",
            "",
            "## Child Processes",
            "",
        ]
        for child in results:
            lines.append(f"- {child.get('name')}: rc={child.get('returncode')} elapsed={child.get('elapsed_sec')}s")
            if child.get("stderr_tail"):
                lines.append(f"  - stderr: `{str(child['stderr_tail'])[:700].replace(chr(10), ' ')}`")
        (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    finally:
        if args.start_service:
            stop_service(proc)
    return run_dir


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--duration-min", type=float, default=420.0)
    p.add_argument("--port", type=int, default=8027)
    p.add_argument("--start-service", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--health-timeout-sec", type=float, default=90.0)
    p.add_argument("--members", type=int, default=14)
    p.add_argument("--bot-prob", type=float, default=0.16)
    p.add_argument("--group-min-gap-sec", type=float, default=4.0)
    p.add_argument("--group-max-gap-sec", type=float, default=12.0)
    p.add_argument("--force-bot-every-sec", type=float, default=180.0)
    p.add_argument("--group-max-bot-inflight", type=int, default=3)
    p.add_argument("--group-max-bot-backlog", type=int, default=96)
    p.add_argument("--group-member-model", choices=["mixed", "gpt55", "deepseek"], default="mixed")
    p.add_argument("--env-projects", type=int, default=5)
    p.add_argument("--env-max-inflight", type=int, default=3)
    p.add_argument("--env-review-gap-sec", type=float, default=20.0)
    p.add_argument("--direct-gap-sec", type=float, default=20.0)
    p.add_argument("--direct-task-timeout-sec", type=float, default=1800.0)
    p.add_argument("--group-drain-sec", type=float, default=240.0)
    p.add_argument("--env-drain-sec", type=float, default=1200.0)
    p.add_argument("--direct-drain-sec", type=float, default=1200.0)
    p.add_argument("--monitor-interval-sec", type=float, default=15.0)
    p.add_argument("--child-grace-sec", type=float, default=1500.0)
    return p.parse_args()


if __name__ == "__main__":
    print(asyncio.run(run(parse_args())))

