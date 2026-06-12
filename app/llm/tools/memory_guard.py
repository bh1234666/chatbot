"""Memory guard for workspace and environment subprocesses."""
from __future__ import annotations

import asyncio
import ctypes
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import settings
from app.core import debug


GiB = 1024 * 1024 * 1024


def _bytes_to_gib(value: int | float) -> float:
    return round(float(value) / GiB, 3)


def workspace_memory_limits() -> tuple[int, int]:
    """Return (workspace_total_limit_bytes, min_system_available_bytes)."""
    limit = int(getattr(settings, "workspace_run_memory_limit_bytes", 16 * GiB) or 0)
    min_available = int(getattr(settings, "workspace_run_min_available_memory_bytes", GiB) or 0)
    return max(limit, 0), max(min_available, 0)


def system_memory_snapshot() -> dict[str, Any]:
    """Return best-effort system memory facts."""
    try:
        import psutil

        vm = psutil.virtual_memory()
        return {
            "ok": True,
            "total_bytes": int(vm.total),
            "available_bytes": int(vm.available),
            "used_bytes": int(vm.used),
            "percent": float(vm.percent),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _process_tree_rss_bytes(pid: int) -> int:
    try:
        import psutil

        root = psutil.Process(pid)
        procs = [root] + root.children(recursive=True)
        total = 0
        for proc in procs:
            try:
                total += int(proc.memory_info().rss)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return total
    except Exception:
        return 0


def _process_alive(pid: int) -> bool:
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:
        return True


def _kill_tree(pid: int, kill_tree: Callable[[int], Any]) -> None:
    try:
        kill_tree(pid)
        return
    except TypeError:
        pass
    try:
        kill_tree(pid=pid)
    except Exception:
        try:
            if sys.platform == "win32":
                import subprocess

                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                )
            else:
                import signal

                os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            pass


def _assign_windows_job_memory_limit(pid: int, limit_bytes: int) -> object | None:
    """Assign process to a Windows Job Object with a job-wide memory limit.

    The returned handle must stay alive while the process runs.
    """
    if sys.platform != "win32" or limit_bytes <= 0:
        return None
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        hjob = kernel32.CreateJobObjectW(None, None)
        if not hjob:
            return None

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_JOB_MEMORY
        info.JobMemoryLimit = int(limit_bytes)
        ok = kernel32.SetInformationJobObject(
            ctypes.c_void_p(hjob),
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(ctypes.c_void_p(hjob))
            return None

        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        ph = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
        if not ph:
            kernel32.CloseHandle(ctypes.c_void_p(hjob))
            return None
        try:
            if not kernel32.AssignProcessToJobObject(ctypes.c_void_p(hjob), ctypes.c_void_p(ph)):
                kernel32.CloseHandle(ctypes.c_void_p(hjob))
                return None
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(ph))
        return hjob
    except Exception as exc:
        debug.log("workspace.memory.job_limit_failed", f"{type(exc).__name__}: {exc}")
        return None


@dataclass
class _GuardSnapshot:
    guard: "WorkspaceMemoryGuard"
    rss_bytes: int


class WorkspaceMemoryCoordinator:
    """Coordinate memory pressure across concurrent subprocess guards.

    The 16GiB setting is treated as the total workspace/env subprocess budget.
    When the total budget or system reserve is under pressure, the coordinator
    kills one process at a time and leaves at least one under-limit process alive
    whenever possible.
    """

    def __init__(self) -> None:
        self._guards: dict[str, WorkspaceMemoryGuard] = {}
        self._lock = asyncio.Lock()
        self._last_kill_at = 0.0
        self._kill_cooldown_sec = 2.0

    def register(self, guard: "WorkspaceMemoryGuard") -> None:
        self._guards[guard.proc_id] = guard

    def unregister(self, guard: "WorkspaceMemoryGuard") -> None:
        self._guards.pop(guard.proc_id, None)

    def active_memory_facts(self) -> dict[str, Any]:
        limit, min_available = workspace_memory_limits()
        entries = self._live_snapshots()
        total = sum(item.rss_bytes for item in entries)
        return {
            "active_process_count": len(entries),
            "workspace_total_rss_gib": _bytes_to_gib(total),
            "workspace_total_limit_gib": _bytes_to_gib(limit) if limit else None,
            "min_available_gib": _bytes_to_gib(min_available) if min_available else None,
            "processes": [self._public_process_facts(item) for item in entries[:8]],
        }

    async def request_relief(
        self,
        requester: "WorkspaceMemoryGuard",
        *,
        reason: str,
        system_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        async with self._lock:
            now = time.monotonic()
            limit, min_available = workspace_memory_limits()
            entries = self._live_snapshots()
            total = sum(item.rss_bytes for item in entries)
            requester_entry = next((item for item in entries if item.guard is requester), None)
            requester_rss = requester_entry.rss_bytes if requester_entry else _process_tree_rss_bytes(requester.pid)

            if limit > 0 and requester_rss > limit:
                return self._kill_guard(
                    requester,
                    reason="process_tree_memory_limit_exceeded",
                    rss_bytes=requester_rss,
                    total_rss_bytes=total,
                    system_snapshot=system_snapshot,
                    limit_bytes=limit,
                    min_available_bytes=min_available,
                )

            pressure = total > limit > 0 or reason == "system_memory_low"
            if not pressure:
                return None

            if now - self._last_kill_at < self._kill_cooldown_sec:
                return None

            victim = self._choose_victim(entries, requester=requester, limit_bytes=limit)
            if victim is None:
                return None

            facts = self._kill_guard(
                victim.guard,
                reason=reason if reason != "system_memory_low" else "system_memory_low",
                rss_bytes=victim.rss_bytes,
                total_rss_bytes=total,
                system_snapshot=system_snapshot,
                limit_bytes=limit,
                min_available_bytes=min_available,
            )
            self._last_kill_at = now
            return facts

    def _live_snapshots(self) -> list[_GuardSnapshot]:
        dead: list[str] = []
        entries: list[_GuardSnapshot] = []
        for proc_id, guard in list(self._guards.items()):
            if guard.stopped or guard.triggered or not _process_alive(guard.pid):
                dead.append(proc_id)
                continue
            entries.append(_GuardSnapshot(guard=guard, rss_bytes=_process_tree_rss_bytes(guard.pid)))
        for proc_id in dead:
            self._guards.pop(proc_id, None)
        entries.sort(key=lambda item: item.guard.started_at)
        return entries

    def _choose_victim(
        self,
        entries: list[_GuardSnapshot],
        *,
        requester: "WorkspaceMemoryGuard",
        limit_bytes: int,
    ) -> _GuardSnapshot | None:
        if not entries:
            return None
        if len(entries) == 1:
            only = entries[0]
            if limit_bytes > 0 and only.rss_bytes > limit_bytes:
                return only
            return None

        # Keep the smallest/oldest runnable process alive when possible; stop one
        # larger/newer process at a time so later work can retry after memory frees.
        candidates = list(entries)
        candidates.sort(
            key=lambda item: (
                item.rss_bytes,
                item.guard.started_at,
                0 if item.guard is not requester else -1,
            ),
            reverse=True,
        )
        return candidates[0]

    def _kill_guard(
        self,
        guard: "WorkspaceMemoryGuard",
        *,
        reason: str,
        rss_bytes: int,
        total_rss_bytes: int,
        system_snapshot: dict[str, Any] | None,
        limit_bytes: int,
        min_available_bytes: int,
    ) -> dict[str, Any]:
        available = int((system_snapshot or {}).get("available_bytes") or 0)
        facts = {
            "reason": reason,
            "pid": guard.pid,
            "proc_id": guard.proc_id,
            "scope": guard.scope,
            "rss_bytes": rss_bytes,
            "rss_gib": _bytes_to_gib(rss_bytes),
            "workspace_total_rss_gib": _bytes_to_gib(total_rss_bytes),
            "workspace_total_limit_gib": _bytes_to_gib(limit_bytes) if limit_bytes else None,
            "process_tree_limit_gib": _bytes_to_gib(limit_bytes) if limit_bytes else None,
            "available_gib": _bytes_to_gib(available) if available else None,
            "min_available_gib": _bytes_to_gib(min_available_bytes) if min_available_bytes else None,
            "command_preview": (guard.command or "")[:160],
            "coordination": (
                "one_subprocess_stopped_to_keep_other_under_limit_work_running"
                if reason != "process_tree_memory_limit_exceeded"
                else "subprocess_stopped_because_its_own_process_tree_exceeded_limit"
            ),
        }
        guard._triggered = facts
        debug.warn(
            "memory guard killed subprocess: "
            f"reason={reason} scope={guard.scope} pid={guard.pid} proc_id={guard.proc_id} "
            f"rss={facts['rss_gib']}GiB total={facts['workspace_total_rss_gib']}GiB "
            f"limit={facts['workspace_total_limit_gib']}GiB available={facts['available_gib']}GiB"
        )
        _kill_tree(guard.pid, guard.kill_tree)
        return facts

    def _public_process_facts(self, item: _GuardSnapshot) -> dict[str, Any]:
        guard = item.guard
        return {
            "proc_id": guard.proc_id,
            "pid": guard.pid,
            "scope": guard.scope,
            "rss_gib": _bytes_to_gib(item.rss_bytes),
            "elapsed_sec": round(time.monotonic() - guard.started_at, 1),
            "command_preview": (guard.command or "")[:120],
        }

    def reset_for_tests(self) -> None:
        self._guards.clear()
        self._last_kill_at = 0.0


_coordinator = WorkspaceMemoryCoordinator()


def active_workspace_memory_facts() -> dict[str, Any]:
    return _coordinator.active_memory_facts()


def preflight_memory_check(command: str = "") -> dict[str, Any] | None:
    """Return a model-facing error when memory is already unavailable."""
    limit, min_available = workspace_memory_limits()
    snap = system_memory_snapshot()
    active = active_workspace_memory_facts()
    active_total_gib = float(active.get("workspace_total_rss_gib") or 0)
    if limit > 0 and active_total_gib * GiB >= limit:
        debug.log(
            "workspace.memory.preflight_block",
            f"active_total={active_total_gib}GiB limit={_bytes_to_gib(limit)}GiB",
        )
        return {
            "ok": False,
            "error": (
                "The command was not started because active workspace/env subprocesses already occupy the "
                "configured memory budget. This is a resource-scheduling fact. Wait for an active subprocess "
                "to finish, stop a less useful one, or retry a smaller command.\n"
                "已有 workspace/env 子进程占用内存预算；本命令未启动。请等待、停止低价值进程或拆小后重试。"
            ),
            "error_kind": "workspace_memory_budget_busy",
            "memory_limit_exceeded": True,
            "memory": {
                **active,
                "command_preview": (command or "")[:160],
            },
            "observed_recovery_options": [
                "wait for active under-limit subprocesses to finish",
                "stop a less useful active subprocess",
                "retry a smaller command or narrower shard",
            ],
        }
    if not snap.get("ok"):
        return None
    available = int(snap.get("available_bytes") or 0)
    if min_available > 0 and available < min_available:
        debug.log(
            "workspace.memory.preflight_block",
            f"available={_bytes_to_gib(available)}GiB below reserve={_bytes_to_gib(min_available)}GiB",
        )
        return {
            "ok": False,
            "error": (
                "The command was not started because host memory is already below the workspace safety reserve. "
                "This is a resource-scheduling fact. Reduce dataset size, split the benchmark, reuse partial "
                "results, or wait until memory is available.\n"
                "启动前系统可用内存已低于安全保留；本命令未启动。请降载、拆分、复用已有结果或等待。"
            ),
            "error_kind": "system_memory_low",
            "memory_limit_exceeded": True,
            "memory": {
                **active,
                "available_gib": _bytes_to_gib(available),
                "min_available_gib": _bytes_to_gib(min_available),
                "command_preview": (command or "")[:160],
            },
            "observed_recovery_options": [
                "wait until host memory is available",
                "reduce dataset size or command scope",
                "reuse existing partial results before retrying unfinished work",
            ],
        }
    return None


@dataclass
class WorkspaceMemoryGuard:
    pid: int
    proc_id: str
    command: str
    kill_tree: Callable[[int], Any]
    limit_bytes: int
    min_available_bytes: int
    scope: str = "workspace.run"
    poll_interval_sec: float = 0.5
    started_at: float = field(default_factory=time.monotonic)
    stopped: bool = False
    _task: asyncio.Task | None = None
    _triggered: dict[str, Any] | None = None
    _job_handle: object | None = None

    def start(self) -> "WorkspaceMemoryGuard":
        _coordinator.register(self)
        if self.limit_bytes > 0:
            self._job_handle = _assign_windows_job_memory_limit(self.pid, self.limit_bytes)
            if self._job_handle:
                debug.log(
                    "workspace.memory.job_limit",
                    f"pid={self.pid} proc_id={self.proc_id} limit={_bytes_to_gib(self.limit_bytes)}GiB",
                )
        self._task = asyncio.create_task(self._watch(), name=f"memory-guard-{self.proc_id}")
        return self

    @property
    def triggered(self) -> dict[str, Any] | None:
        return self._triggered

    async def stop(self) -> None:
        self.stopped = True
        _coordinator.unregister(self)
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._close_job()

    def _close_job(self) -> None:
        if self._job_handle and sys.platform == "win32":
            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle(ctypes.c_void_p(int(self._job_handle)))
            except Exception:
                pass
        self._job_handle = None

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval_sec)
            if self.stopped or self._triggered:
                return
            rss = _process_tree_rss_bytes(self.pid)
            snap = system_memory_snapshot()
            available = int(snap.get("available_bytes") or 0) if snap.get("ok") else 0
            reason = ""
            if self.limit_bytes > 0 and rss > self.limit_bytes:
                reason = "process_tree_memory_limit_exceeded"
            elif self.limit_bytes > 0:
                active = active_workspace_memory_facts()
                active_total_gib = float(active.get("workspace_total_rss_gib") or 0)
                if active_total_gib * GiB > self.limit_bytes:
                    reason = "workspace_total_memory_limit_exceeded"
            if not reason and self.min_available_bytes > 0 and available and available < self.min_available_bytes:
                reason = "system_memory_low"
            if not reason:
                continue
            await _coordinator.request_relief(self, reason=reason, system_snapshot=snap)


def memory_limit_error(facts: dict[str, Any], *, stdout: str = "", stderr: str = "") -> dict[str, Any]:
    """Build a model-facing memory limit result."""
    return {
        "ok": False,
        "error": (
            "The command was stopped by the memory guard before OS memory exhaustion. "
            "This is a resource limit fact, not proof of a code bug. Completed partial work may still be valid. "
            "The memory facts below support decisions such as waiting for under-limit work, splitting the benchmark, "
            "reducing data size, streaming results to files, or retrying unfinished work after pressure drops.\n"
            "命令因内存保护被中断；这是资源事实，不等于代码错误。内存事实可支持等待、拆分、降载或重试未完成部分。"
        ),
        "error_kind": "memory_limit_exceeded",
        "memory_limit_exceeded": True,
        "resource_required": {
            "resource_kind": "memory",
            "matching_helper_kind": "code",
            "suggested_helper_kind": "code",
            "blocked_reason": "workspace/env subprocess memory pressure",
            "needed_outputs": [],
            "observed_recovery_options": [
                "wait for still-running under-limit work",
                "split the benchmark or command into smaller shards",
                "retry only unfinished portions after memory pressure drops",
            ],
            "resource_resolution_facts": (
                "This is memory pressure. Re-running all unfinished heavy commands in parallel unchanged preserves "
                "the same pressure pattern; under-limit work, smaller shards, or retrying unfinished portions after "
                "pressure drops are recoverable shapes."
            ),
            "main_thread_action": (
                "main process decides from active task and memory facts: wait for still-running under-limit work, "
                "split the benchmark, reduce load, or retry only unfinished portions"
            ),
        },
        "memory": facts,
        "partial_stdout": stdout if stdout else "",
        "partial_stderr": stderr if stderr else "",
        "observed_recovery_options": [
            "wait for still-running under-limit work",
            "split or reduce the heavy command",
            "retry only unfinished portions after memory pressure drops",
        ],
    }
