"""Environment-mode workflow and command monitor.

This is intentionally lightweight and in-process. It gives local frontends a
separate stream for workflow visibility without changing the existing chat/QQ
paths or requiring a new database table.
"""
from __future__ import annotations

import asyncio
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EnvironmentCommand:
    command_id: str
    trace_id: str
    archive_id: str
    group_id: str
    user_id: str
    root_dir: str
    cwd: str
    command: str
    started_at: float
    proc: Any = None
    pid: int | None = None
    status: str = "running"
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)
    returncode: int | None = None
    timed_out: bool = False
    elapsed_sec: float = 0.0

    def public(self) -> dict:
        return {
            "command_id": self.command_id,
            "trace_id": self.trace_id,
            "archive_id": self.archive_id,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "root_dir": self.root_dir,
            "cwd": self.cwd,
            "command": self.command[:500],
            "pid": self.pid,
            "status": self.status,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "elapsed_sec": round(time.monotonic() - self.started_at, 3)
            if self.status == "running" else self.elapsed_sec,
        }


@dataclass
class EnvironmentMonitor:
    max_history: int = 1000
    _active_commands: dict[str, EnvironmentCommand] = field(default_factory=dict)
    _history: list[dict] = field(default_factory=list)
    _subscribers: set[asyncio.Queue] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def publish(self, event: str, payload: dict) -> None:
        item = {
            "event": event,
            "payload": payload,
            "ts": time.time(),
        }
        async with self._lock:
            self._history.append(item)
            if len(self._history) > self.max_history:
                del self._history[: len(self._history) - self.max_history]
            queues = list(self._subscribers)
        for queue in queues:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    async def snapshot(
        self,
        *,
        archive_id: str = "",
        group_id: str = "",
        user_id: str = "",
        trace_id: str = "",
    ) -> dict:
        async with self._lock:
            commands = [
                cmd.public()
                for cmd in self._active_commands.values()
                if _match(cmd.archive_id, archive_id)
                and _match(cmd.group_id, group_id)
                and _match(cmd.user_id, user_id)
                and _match(cmd.trace_id, trace_id)
            ]
        helpers = await _active_helpers_snapshot(
            archive_id=archive_id,
            group_id=group_id,
            user_id=user_id,
            trace_id=trace_id,
        )
        return {
            "active_commands": commands,
            "active_command_count": len(commands),
            "active_helpers": helpers,
            "active_helper_count": len(helpers),
        }

    async def history(
        self,
        *,
        archive_id: str = "",
        group_id: str = "",
        user_id: str = "",
        trace_id: str = "",
        limit: int = 200,
    ) -> list[dict]:
        limit = max(1, min(int(limit or 200), self.max_history))
        async with self._lock:
            items = list(self._history)
        matched = []
        for item in reversed(items):
            payload = item.get("payload") or {}
            if archive_id and payload.get("archive_id") and payload.get("archive_id") != archive_id:
                continue
            if group_id and payload.get("group_id") and payload.get("group_id") != group_id:
                continue
            if user_id and payload.get("user_id") and payload.get("user_id") != user_id:
                continue
            if trace_id and payload.get("trace_id") != trace_id:
                continue
            matched.append(item)
            if len(matched) >= limit:
                break
        return list(reversed(matched))

    async def run_snapshot(
        self,
        trace_id: str,
        *,
        archive_id: str = "",
        group_id: str = "",
        user_id: str = "",
        limit: int = 500,
    ) -> dict:
        trace_id = (trace_id or "").strip()
        if not trace_id:
            return {"trace_id": "", "items": []}
        items = await self.history(
            archive_id=archive_id,
            group_id=group_id,
            user_id=user_id,
            trace_id=trace_id,
            limit=limit,
        )
        matched = []
        for item in items:
            payload = item.get("payload") or {}
            if payload.get("trace_id") != trace_id:
                continue
            matched.append(item)
        active = await self.snapshot(
            archive_id=archive_id,
            group_id=group_id,
            user_id=user_id,
            trace_id=trace_id,
        )
        return {
            "trace_id": trace_id,
            "items": matched,
            "active": active,
        }

    async def register_command(
        self,
        *,
        trace_id: str,
        archive_id: str,
        group_id: str,
        user_id: str,
        root_dir: str,
        cwd: str,
        command: str,
    ) -> str:
        command_id = "envcmd_" + uuid.uuid4().hex[:10]
        cmd = EnvironmentCommand(
            command_id=command_id,
            trace_id=trace_id,
            archive_id=archive_id,
            group_id=group_id,
            user_id=user_id,
            root_dir=root_dir,
            cwd=cwd,
            command=command,
            started_at=time.monotonic(),
        )
        async with self._lock:
            self._active_commands[command_id] = cmd
        await self.publish("command", {"kind": "start", **cmd.public()})
        return command_id

    async def attach_process(self, command_id: str, proc: Any, pid: int | None) -> None:
        async with self._lock:
            cmd = self._active_commands.get(command_id)
            if cmd is None:
                return
            cmd.proc = proc
            cmd.pid = pid
            payload = cmd.public()
        await self.publish("command", {"kind": "pid", **payload})

    async def finish_command(
        self,
        command_id: str,
        *,
        status: str,
        returncode: int | None,
        timed_out: bool,
    ) -> dict:
        async with self._lock:
            cmd = self._active_commands.pop(command_id, None)
            if cmd is None:
                return {"ok": False, "error": "command not found"}
            cmd.status = status
            cmd.returncode = returncode
            cmd.timed_out = timed_out
            cmd.elapsed_sec = round(time.monotonic() - cmd.started_at, 3)
            payload = cmd.public()
        await self.publish("command", {"kind": "done", **payload})
        return {"ok": True, **payload}

    async def abort_command(self, command_id: str) -> dict:
        async with self._lock:
            cmd = self._active_commands.get(command_id)
            if cmd is None:
                return {"ok": False, "error": "command not active"}
            proc = cmd.proc
            cmd.status = "aborting"
            cmd.abort_event.set()
            payload = cmd.public()
        if proc is None:
            return {"ok": False, "error": "process not attached", **payload}
        try:
            if sys.platform == "win32":
                from app.llm.tools.process_utils import _kill_process_tree
                _kill_process_tree(proc.pid, proc_obj=proc)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}", **payload}
        await self.publish("command", {"kind": "abort_requested", **payload})
        return {"ok": True, **payload}

    async def wait_abort(self, command_id: str) -> bool:
        async with self._lock:
            cmd = self._active_commands.get(command_id)
            if cmd is None:
                return False
            abort_event = cmd.abort_event
        await abort_event.wait()
        return True


def _match(value: str, expected: str) -> bool:
    return not expected or value == expected


async def _active_helpers_snapshot(
    *,
    archive_id: str = "",
    group_id: str = "",
    user_id: str = "",
    trace_id: str = "",
) -> list[dict]:
    try:
        from app.core.core_processes import registry
    except Exception:
        return []
    try:
        helpers = []
        for item in await registry().list_all_public():
            if item.get("proc_type") != "helper":
                continue
            if trace_id:
                owner = str(item.get("owner") or "")
                if not (owner == f"main:{trace_id}" or owner.startswith(f"helper:{trace_id}:")):
                    continue
            if archive_id and item.get("archive_id") and item.get("archive_id") != archive_id:
                continue
            if group_id and item.get("group_id") and item.get("group_id") != group_id:
                continue
            if user_id and item.get("user_id") and item.get("user_id") != user_id:
                continue
            helpers.append(item)
        return helpers
    except Exception:
        return []


monitor = EnvironmentMonitor()
