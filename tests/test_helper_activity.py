import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class FakeRegistry:
    def __init__(self, processes=None, handles=None):
        self.processes = processes or []
        self.handles = handles or {}

    async def list_owned_by(self, owner):
        return self.processes

    async def find_by_proc_id(self, proc_id):
        return self.handles.get(proc_id)


class FakeHandle:
    def __init__(self):
        self.abort_event = asyncio.Event()


class DebugRecorder:
    def __init__(self):
        self.events = []

    def log(self, category, message, payload=None):
        self.events.append((category, message, payload))


async def test_scan_active_helpers_uses_registry_as_active_source(monkeypatch, tmp_path):
    from app.core import core_processes
    from app.core.helper_activity import scan_active_helpers

    active_ws = tmp_path / "_delegate_user_task_live"
    completed_ws = tmp_path / "_delegate_user_task_done"
    active_ws.mkdir()
    completed_ws.mkdir()
    (active_ws / ".helper_summary.txt").write_text("live summary", encoding="utf-8")
    (completed_ws / ".helper_summary.txt").write_text("done summary", encoding="utf-8")
    registry = FakeRegistry(processes=[{
        "proc_type": "helper",
        "helper_task_id": "task_live",
        "proc_id": "p1",
        "recent_tools": ["a", "b"],
        "helper_workspace": str(active_ws),
    }])
    monkeypatch.setattr(core_processes, "registry", lambda: registry)

    active, completed = await scan_active_helpers(trace_id="t1", workspace_dir=str(tmp_path))

    assert set(active) == {"task_live"}
    assert active["task_live"]["report_excerpt"] == "live summary"
    assert set(completed) == {"task_done"}
    assert completed["task_done"]["report_excerpt"] == "done summary"


async def test_scan_active_helpers_can_filter_old_completed_helpers(monkeypatch, tmp_path):
    from app.core import core_processes
    from app.core.helper_activity import scan_active_helpers
    import os
    import time

    old_ws = tmp_path / "_delegate_user_task_old"
    new_ws = tmp_path / "_delegate_user_task_new"
    old_ws.mkdir()
    new_ws.mkdir()
    old_time = time.time() - 3600
    os.utime(old_ws, (old_time, old_time))
    registry = FakeRegistry(processes=[])
    monkeypatch.setattr(core_processes, "registry", lambda: registry)

    active, completed = await scan_active_helpers(
        trace_id="t1",
        workspace_dir=str(tmp_path),
        completed_since=time.time() - 60,
    )

    assert active == {}
    assert set(completed) == {"task_new"}


async def test_request_active_helpers_finalize_sets_abort_event(monkeypatch):
    from app.core import core_processes
    from app.core.helper_activity import request_active_helpers_finalize

    handle = FakeHandle()
    registry = FakeRegistry(handles={"p1": handle})
    monkeypatch.setattr(core_processes, "registry", lambda: registry)
    debug = DebugRecorder()

    sent = await request_active_helpers_finalize(
        trace_id="t1",
        active_helpers={"task": {"proc_id": "p1"}},
        debug=debug,
    )

    assert sent == 1
    assert handle.abort_event.is_set()
    assert debug.events[-1][0] == "orchestrate.complete.finalize_helper"
