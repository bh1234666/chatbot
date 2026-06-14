import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class DebugRecorder:
    def __init__(self):
        self.events = []

    def log(self, category, message, payload=None):
        self.events.append((category, message, payload))


class FakeRegistry:
    async def list_owned_by(self, owner):
        return []


async def test_collect_and_save_pause_snapshot_merges_previous_helpers(monkeypatch, tmp_path):
    from app.core import core_processes, pause_state
    from app.core.pause_snapshot import collect_and_save_pause_snapshot
    from app.schemas.api import ChatRequest, ResponsePlan

    saved = {}

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(core_processes, "registry", lambda: FakeRegistry())

    async def fake_load_pause(**kwargs):
        return {
            "active_helpers": [{"task_id": "old_active"}],
            "completed_helpers": [{"task_id": "old_done"}],
        }

    async def fake_save_pause(**kwargs):
        saved.update(kwargs)
        return True

    monkeypatch.setattr(pause_state, "load_pause", fake_load_pause)
    monkeypatch.setattr(pause_state, "save_pause", fake_save_pause)

    helper_ws = tmp_path / "_delegate_user_new_task"
    helper_ws.mkdir()
    (helper_ws / ".helper_summary.txt").write_text("new summary", encoding="utf-8")

    req = ChatRequest(
        archive_id="a1",
        group_id="g1",
        user_id="u1",
        user_name="User",
        message="stop",
    )
    plan = ResponsePlan(intent="处理", key_points=["保存进度"], tone="自然", length_hint="短")
    debug = DebugRecorder()

    await collect_and_save_pause_snapshot(
        req=req,
        trace_id="t1",
        abort_event=asyncio.Event(),
        plan=plan,
        round3_partial_text="partial",
        workspace_dir=str(tmp_path),
        debug=debug,
    )

    active_ids = {helper["task_id"] for helper in saved["active_helpers"]}
    completed_ids = {helper["task_id"] for helper in saved["completed_helpers"]}
    assert {"new_task", "old_active"}.issubset(active_ids)
    assert "old_done" in completed_ids
    assert saved["round3_partial_text"] == "partial"
    assert any(event[0] == "pause_state.merge" for event in debug.events)


async def test_collect_and_save_pause_snapshot_uses_request_user_scope(monkeypatch, tmp_path):
    from app.core import core_processes, pause_state
    from app.core.pause_snapshot import collect_and_save_pause_snapshot
    from app.schemas.api import ChatRequest

    calls = {"load": None, "save": None}

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(core_processes, "registry", lambda: FakeRegistry())

    async def fake_load_pause(**kwargs):
        calls["load"] = kwargs
        return {"active_helpers": [{"task_id": "old_user_b"}], "completed_helpers": []}

    async def fake_save_pause(**kwargs):
        calls["save"] = kwargs
        return True

    monkeypatch.setattr(pause_state, "load_pause", fake_load_pause)
    monkeypatch.setattr(pause_state, "save_pause", fake_save_pause)

    req = ChatRequest(
        archive_id="arch",
        group_id="group",
        user_id="user_b",
        user_name="Bob",
        message="pause my run",
    )

    await collect_and_save_pause_snapshot(
        req=req,
        trace_id="trace_b",
        abort_event=asyncio.Event(),
        plan=None,
        round3_partial_text="",
        workspace_dir=str(tmp_path),
    )

    assert calls["load"] == {"archive_id": "arch", "group_id": "group", "user_id": "user_b"}
    assert calls["save"]["archive_id"] == "arch"
    assert calls["save"]["group_id"] == "group"
    assert calls["save"]["user_id"] == "user_b"
    assert calls["save"]["active_helpers"] == [{"task_id": "old_user_b"}]
