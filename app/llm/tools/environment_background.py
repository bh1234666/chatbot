"""Environment-only background process runner.

Background jobs are intentionally file-backed: tool calls return handles and
workspace-relative result paths, while stdout/stderr/result bodies stay on disk
for later read_file inspection.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core import debug
from app.core.bg_tasks import schedule
from app.core.environment_monitor import monitor as env_monitor
from app.core.locks import get_group_guard
from app.core.runtime_mode import current_environment
from app.llm.tools.command_risk import analyze_command
from app.llm.tools.process_utils import _kill_process_tree
from app.llm.tools.workspace import _translate_windows_command


async def _try_send_direct_reminder(task: "BackgroundTask", message: str) -> dict[str, Any]:
    """Best-effort direct user reminder when no main task is active."""
    if not str(task.group_id or "").isdigit():
        return {"sent": False, "reason": "group_id_not_numeric"}
    try:
        import httpx
        from app.config import settings

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{settings.napcat_url}/send_group_msg",
                json={
                    "group_id": int(task.group_id),
                    "message": f"[CQ:at,qq={task.user_id}] {message}",
                },
            )
        return {
            "sent": resp.status_code == 200,
            "status_code": resp.status_code,
            "body": resp.text[:200],
        }
    except Exception as exc:
        return {"sent": False, "error": f"{type(exc).__name__}: {exc}"}


ENV_BACKGROUND_SCHEMA = {
    "type": "function",
    "function": {
        "name": "env_background",
        "description": (
            "Start, inspect, or stop environment-mode background processes without blocking the current task. "
            "Use it for project commands, timers, servers, long checks, watchers, or other work that should continue "
            "while the main flow or a code helper proceeds. This tool is environment-only and unavailable in group chat mode. "
            "Start returns only task IDs and absolute file paths; stdout, stderr, and final result JSON are written under "
            "a task-specific background output directory and are not injected into the model. Read result_path/status_path "
            "or the matching *_abs_path fields with read_file when "
            "a completion or periodic wake notification arrives. Set notify_on_finish=true when the user should be "
            "reminded after completion; running jobs wake the main flow for status checks at least every 10 minutes."
            "\n\n"
            "环境模式后台进程工具；启动后不阻塞；结果不内联返回，提醒或周期唤醒后优先用返回的绝对路径 read_file 读取。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "status", "list", "kill"],
                    "description": "Operation to perform. start launches a new background process.",
                    "default": "start",
                },
                "task_id": {
                    "type": "string",
                    "description": "Semantic task id. For start it is optional; for status/kill it selects a background task.",
                },
                "command": {
                    "type": "string",
                    "description": "Shell command to run in the real project directory. Use either command or python_code.",
                },
                "python_code": {
                    "type": "string",
                    "description": "Alternative to command: Python script body executed from a temporary file with cwd in the project.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Project-relative working directory. Defaults to project root.",
                    "default": ".",
                },
                "notify_on_finish": {
                    "type": "boolean",
                    "description": "Whether to notify the user/main flow when the background process finishes.",
                    "default": False,
                },
                "reminder_text": {
                    "type": "string",
                    "description": "Short user-visible completion/check reminder, such as the task name that ended. Optional.",
                },
                "wake_interval_sec": {
                    "type": "integer",
                    "description": "Periodic forced wake/check interval while running. Defaults to 600 and is clamped to at most 600 seconds.",
                    "default": 600,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}


_TASK_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass
class BackgroundTask:
    task_id: str
    command_id: str
    command: str
    cwd: str
    workspace_dir: str
    result_dir: Path
    result_rel: str
    stdout_rel: str
    stderr_rel: str
    status_rel: str
    archive_id: str
    group_id: str
    user_id: str
    root_dir: str
    trace_id: str
    notify_on_finish: bool
    reminder_text: str
    wake_interval_sec: float
    created_at: float = field(default_factory=time.time)
    started_at: float = field(default_factory=time.monotonic)
    status: str = "starting"
    returncode: int | None = None
    pid: int | None = None
    proc: asyncio.subprocess.Process | None = None
    last_wake_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None

    def public(self) -> dict[str, Any]:
        elapsed = (
            (self.finished_at or time.monotonic()) - self.started_at
        )
        return {
            "task_id": self.task_id,
            "command_id": self.command_id,
            "status": self.status,
            "pid": self.pid,
            "returncode": self.returncode,
            "elapsed_sec": round(elapsed, 3),
            "cwd": self.cwd,
            "command": self.command[:500],
            "archive_id": self.archive_id,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "trace_id": self.trace_id,
            "root_dir": self.root_dir,
            "result_path": str((self.result_dir / "result.json").resolve()),
            "stdout_path": str((self.result_dir / "stdout.log").resolve()),
            "stderr_path": str((self.result_dir / "stderr.log").resolve()),
            "status_path": str((self.result_dir / "status.json").resolve()),
            "result_abs_path": str((self.result_dir / "result.json").resolve()),
            "stdout_abs_path": str((self.result_dir / "stdout.log").resolve()),
            "stderr_abs_path": str((self.result_dir / "stderr.log").resolve()),
            "status_abs_path": str((self.result_dir / "status.json").resolve()),
            "result_rel_path": self.result_rel,
            "stdout_rel_path": self.stdout_rel,
            "stderr_rel_path": self.stderr_rel,
            "status_rel_path": self.status_rel,
            "workspace_dir": self.workspace_dir,
            "notify_on_finish": self.notify_on_finish,
            "reminder_text": self.reminder_text,
            "wake_interval_sec": self.wake_interval_sec,
            "output_read_instruction": (
                "Use read_file on absolute result_path/status_path/stdout_path/stderr_path when the background task is relevant. "
                "The *_rel_path fields are compatibility hints only and depend on the project root cwd. "
                "The process output is intentionally not included in tool results."
            ),
        }


_TASKS: dict[str, BackgroundTask] = {}
_LOCK = asyncio.Lock()


def _safe_task_id(raw: str) -> str:
    value = _TASK_ID_RE.sub("_", (raw or "").strip())[:64].strip("._-")
    return value or f"bg_{uuid.uuid4().hex[:10]}"


def _result_paths(storage_root: str, task_id: str) -> tuple[Path, dict[str, str]]:
    root = Path(storage_root).resolve()
    result_dir = root / "_env_background" / task_id
    rel_base = f".temp/_env_background/{task_id}"
    return result_dir, {
        "status": f"{rel_base}/status.json",
        "stdout": f"{rel_base}/stdout.log",
        "stderr": f"{rel_base}/stderr.log",
        "result": f"{rel_base}/result.json",
    }


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_status(task: BackgroundTask) -> None:
    _write_json(task.result_dir / "status.json", task.public())


def _base_env(root_dir: str) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["NVIDIA_VISIBLE_DEVICES"] = "none"
    env["ENV_PROJECT_ROOT"] = str(Path(root_dir).resolve())
    return env


async def _stream_to_file(stream: asyncio.StreamReader | None, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with path.open("wb") as f:
        if stream is None:
            return 0
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            total += len(chunk)
            f.write(chunk)
            f.flush()
    return total


async def _notify(task: BackgroundTask, *, kind: str, detail: str) -> None:
    short = (task.reminder_text or detail or f"{task.task_id} task update").strip()
    if len(short) > 120:
        short = short[:120].rstrip()
    finished_suffix = "\u4efb\u52a1\u7ed3\u675f"
    if kind == "finished" and finished_suffix not in short and "finished" not in short.lower():
        short = f"{short} {finished_suffix}"
    interrupt_message = f"[CQ:at,qq={task.user_id}] {short}"
    direct_message = (
        f"[\u540e\u53f0\u4efb\u52a1\u63d0\u9192] {short}\n"
        f"\u72b6\u6001\u6587\u4ef6: {(task.result_dir / 'status.json').resolve()}\n"
        f"\u7ed3\u679c\u6587\u4ef6: {(task.result_dir / 'result.json').resolve()}\n"
        "\u8bf7\u6309\u9700\u8bfb\u53d6\u7ed3\u679c\u6587\u4ef6\u540e\u518d\u7ee7\u7eed\u5224\u65ad\u3002"
    )
    guard = get_group_guard()
    busy = await guard.is_busy(task.archive_id, task.group_id, task.user_id)
    await env_monitor.publish("workflow", {
        "kind": "background_reminder",
        "reminder_kind": kind,
        "task_id": task.task_id,
        "status": task.status,
        "message": interrupt_message,
        "direct_message": direct_message,
        "active_task": busy,
        "trace_id": task.trace_id,
        "archive_id": task.archive_id,
        "group_id": task.group_id,
        "user_id": task.user_id,
        "result_path": str((task.result_dir / "result.json").resolve()),
        "status_path": str((task.result_dir / "status.json").resolve()),
        "result_abs_path": str((task.result_dir / "result.json").resolve()),
        "status_abs_path": str((task.result_dir / "status.json").resolve()),
        "result_rel_path": task.result_rel,
        "status_rel_path": task.status_rel,
    })
    if not busy:
        direct = await _try_send_direct_reminder(task, direct_message)
        await env_monitor.publish("workflow", {
            "kind": "background_direct_reminder",
            "task_id": task.task_id,
            "status": task.status,
            "message": direct_message,
            "direct_send": direct,
            "trace_id": task.trace_id,
            "archive_id": task.archive_id,
            "group_id": task.group_id,
            "user_id": task.user_id,
            "result_path": str((task.result_dir / "result.json").resolve()),
            "status_path": str((task.result_dir / "status.json").resolve()),
            "result_abs_path": str((task.result_dir / "result.json").resolve()),
            "status_abs_path": str((task.result_dir / "status.json").resolve()),
            "result_rel_path": task.result_rel,
            "status_rel_path": task.status_rel,
        })
        return
    try:
        from app.api.interrupts import push_interrupt_payload

        push_interrupt_payload(
            archive_id=task.archive_id,
            group_id=task.group_id,
            user_id=task.user_id,
            message=interrupt_message,
            client_msg_id=f"env_bg_{kind}_{task.task_id}_{int(time.time())}",
            kind="background",
            source=f"env_background_{kind}",
            meta={
                "task_id": task.task_id,
                "status": task.status,
                "reminder_kind": kind,
                "result_path": str((task.result_dir / "result.json").resolve()),
                "status_path": str((task.result_dir / "status.json").resolve()),
                "result_abs_path": str((task.result_dir / "result.json").resolve()),
                "status_abs_path": str((task.result_dir / "status.json").resolve()),
                "result_rel_path": task.result_rel,
                "status_rel_path": task.status_rel,
                "direct_message": direct_message,
            },
            maxlen=10,
        )
        await guard.signal_abort(task.archive_id, task.group_id, task.user_id)
    except Exception:
        debug.warn(f"env_background notify failed for {task.task_id}")


async def _periodic_wake_loop(task: BackgroundTask) -> None:
    while task.status == "running":
        await asyncio.sleep(max(1.0, min(task.wake_interval_sec, 600.0)))
        if task.status != "running":
            return
        task.last_wake_at = time.monotonic()
        _write_status(task)
        await _notify(
            task,
            kind="check",
            detail=f"{task.task_id} \u540e\u53f0\u4efb\u52a1\u4ecd\u5728\u8fd0\u884c\uff0c\u8bf7\u68c0\u67e5\u72b6\u6001",
        )


async def _run_task(task: BackgroundTask, *, env: dict[str, str], temp_script_path: Path | None = None) -> None:
    stdout_path = task.result_dir / "stdout.log"
    stderr_path = task.result_dir / "stderr.log"
    result_path = task.result_dir / "result.json"
    task.status = "running"
    _write_status(task)
    await env_monitor.publish("workflow", {
        "kind": "background_start",
        "trace_id": task.trace_id,
        "archive_id": task.archive_id,
        "group_id": task.group_id,
        "user_id": task.user_id,
        **task.public(),
    })
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    preexec_fn = os.setsid if sys.platform != "win32" else None
    try:
        proc = await asyncio.create_subprocess_shell(
            task.command,
            cwd=task.cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
            preexec_fn=preexec_fn,
        )
        task.proc = proc
        task.pid = proc.pid
        await env_monitor.attach_process(task.command_id, proc, proc.pid)
        _write_status(task)
        wake_task = asyncio.create_task(_periodic_wake_loop(task))
        stdout_task = asyncio.create_task(_stream_to_file(proc.stdout, stdout_path))
        stderr_task = asyncio.create_task(_stream_to_file(proc.stderr, stderr_path))
        try:
            returncode = await proc.wait()
            stdout_bytes, stderr_bytes = await asyncio.gather(stdout_task, stderr_task)
        finally:
            if not wake_task.done():
                wake_task.cancel()
                try:
                    await wake_task
                except asyncio.CancelledError:
                    pass
        task.returncode = returncode
        task.status = "completed" if returncode == 0 else "failed"
        task.finished_at = time.monotonic()
        result = {
            **task.public(),
            "ok": returncode == 0,
            "stdout_bytes": stdout_bytes,
            "stderr_bytes": stderr_bytes,
            "completed_at": time.time(),
        }
    except asyncio.CancelledError:
        task.status = "cancelled"
        task.finished_at = time.monotonic()
        if task.proc and task.proc.pid:
            try:
                _kill_process_tree(task.proc.pid, proc_obj=task.proc)
            except Exception:
                pass
        result = {**task.public(), "ok": False, "cancelled": True}
        raise
    except Exception as exc:
        task.status = "error"
        task.finished_at = time.monotonic()
        result = {**task.public(), "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if temp_script_path is not None:
            try:
                temp_script_path.unlink(missing_ok=True)
            except OSError:
                pass
        task.returncode = task.returncode
        _write_json(result_path, result)
        _write_status(task)
        await env_monitor.finish_command(
            task.command_id,
            status=task.status,
            returncode=task.returncode,
            timed_out=False,
        )
        await env_monitor.publish("workflow", {
            "kind": "background_done",
            "trace_id": task.trace_id,
            "archive_id": task.archive_id,
            "group_id": task.group_id,
            "user_id": task.user_id,
            **task.public(),
        })
        if task.notify_on_finish:
            await _notify(task, kind="finished", detail=f"{task.task_id} 任务结束")


async def _start(workspace_dir: str, args: dict[str, Any]) -> dict[str, Any]:
    env_ctx = current_environment()
    if env_ctx is None:
        return {"ok": False, "error": "environment context is required"}
    storage_root = Path(env_ctx.root_dir).resolve() / ".temp"
    storage_root.mkdir(parents=True, exist_ok=True)
    workspace_dir = str(storage_root)
    command = str(args.get("command") or "").strip()
    python_code = str(args.get("python_code") or "")
    if command and python_code.strip():
        return {"ok": False, "error": "Provide either command or python_code, not both."}
    if not command and not python_code.strip():
        return {"ok": False, "error": "command or python_code is required"}
    cwd = Path(env_ctx.root_dir).resolve() / str(args.get("cwd") or ".")
    cwd = cwd.resolve()
    root = Path(env_ctx.root_dir).resolve()
    try:
        cwd.relative_to(root)
    except ValueError:
        return {"ok": False, "error": "cwd escapes environment root"}
    if not cwd.is_dir():
        return {"ok": False, "error": "cwd is not a directory"}
    temp_script_path: Path | None = None
    if python_code.strip():
        tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".py", prefix="env_bg_", delete=False)
        try:
            tmp.write(python_code)
            if not python_code.endswith("\n"):
                tmp.write("\n")
        finally:
            tmp.close()
        temp_script_path = Path(tmp.name)
        command = f'"{sys.executable}" -X utf8 "{temp_script_path}"'
    decision = analyze_command(command, str(cwd), is_main_thread=True)
    if not decision.allowed:
        if temp_script_path is not None:
            temp_script_path.unlink(missing_ok=True)
        return {"ok": False, "error": decision.reason, "category": decision.category}
    command = _translate_windows_command(command, str(cwd))
    task_id = _safe_task_id(str(args.get("task_id") or ""))
    async with _LOCK:
        if task_id in _TASKS and _TASKS[task_id].status in {"starting", "running"}:
            task_id = _safe_task_id(f"{task_id}_{uuid.uuid4().hex[:6]}")
    result_dir, rels = _result_paths(workspace_dir, task_id)
    result_dir.mkdir(parents=True, exist_ok=True)
    trace_id = debug.current_trace_id() or ""
    command_id = await env_monitor.register_command(
        trace_id=trace_id,
        archive_id=env_ctx.archive_id,
        group_id=env_ctx.group_id,
        user_id=env_ctx.user_id,
        root_dir=env_ctx.root_dir,
        cwd=str(cwd),
        command=command,
    )
    wake_interval = max(1.0, min(float(args.get("wake_interval_sec") or 600), 600.0))
    task = BackgroundTask(
        task_id=task_id,
        command_id=command_id,
        command=command,
        cwd=str(cwd),
        workspace_dir=str(Path(workspace_dir).resolve()),
        result_dir=result_dir,
        result_rel=rels["result"],
        stdout_rel=rels["stdout"],
        stderr_rel=rels["stderr"],
        status_rel=rels["status"],
        archive_id=env_ctx.archive_id,
        group_id=env_ctx.group_id,
        user_id=env_ctx.user_id,
        root_dir=env_ctx.root_dir,
        trace_id=trace_id,
        notify_on_finish=bool(args.get("notify_on_finish", False)),
        reminder_text=str(args.get("reminder_text") or "").strip(),
        wake_interval_sec=wake_interval,
    )
    async with _LOCK:
        _TASKS[task_id] = task
    _write_status(task)
    env = _base_env(env_ctx.root_dir)
    schedule(_run_task(task, env=env, temp_script_path=temp_script_path), name=f"env_bg.{task_id}")
    return {
        "ok": True,
        "started": True,
        **task.public(),
        "notify_on_finish": task.notify_on_finish,
        "wake_interval_sec": task.wake_interval_sec,
        "result_storage_fact": (
            "Background process output is stored in files and is not returned inline. "
            "When notified or checking later, use read_file on absolute status_path/result_path/stdout_path/stderr_path. "
            "The matching *_abs_path fields carry the same absolute paths for compatibility."
        ),
    }


async def handle_background_tool(workspace_dir: str, args: dict[str, Any]) -> dict[str, Any]:
    action = str((args or {}).get("action") or "start").strip().lower()
    if action == "start":
        return await _start(workspace_dir, args or {})
    task_id = _safe_task_id(str((args or {}).get("task_id") or ""))
    async with _LOCK:
        tasks = list(_TASKS.values())
        task = _TASKS.get(task_id)
    if action == "list":
        return {
            "ok": True,
            "tasks": [t.public() for t in tasks],
            "output_read_instruction": "Use read_file on each task's absolute status_path/result_path/stdout_path/stderr_path.",
        }
    if task is None:
        return {"ok": False, "error": "background task not found", "task_id": task_id}
    if action == "status":
        return {"ok": True, **task.public()}
    if action == "kill":
        if task.proc and task.proc.pid and task.status == "running":
            try:
                _kill_process_tree(task.proc.pid, proc_obj=task.proc)
                task.status = "killed"
                task.finished_at = time.monotonic()
                _write_status(task)
                return {"ok": True, **task.public()}
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}", **task.public()}
        return {"ok": True, "already_finished": True, **task.public()}
    return {"ok": False, "error": f"unknown env_background action: {action}"}


async def list_running_background_tasks() -> list[dict[str, Any]]:
    async with _LOCK:
        tasks = [task.public() for task in _TASKS.values() if task.status == "running"]
    return tasks


async def reset_background_tasks_for_tests() -> None:
    async with _LOCK:
        tasks = list(_TASKS.values())
        _TASKS.clear()
    for task in tasks:
        if task.proc and task.status == "running":
            try:
                _kill_process_tree(task.proc.pid, proc_obj=task.proc)
            except Exception:
                pass
    try:
        from app.core.bg_tasks import cancel_all
        await cancel_all(timeout=1.5)
    except Exception:
        pass
    await asyncio.sleep(0.05)
