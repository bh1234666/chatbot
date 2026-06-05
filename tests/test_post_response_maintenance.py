import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class DebugRecorder:
    def __init__(self):
        self.events = []

    def log(self, category, message, payload=None):
        self.events.append((category, message, payload))


class FakeRequest:
    archive_id = "a"
    group_id = "g"
    user_id = "u"
    user_name = "User"


async def test_post_response_maintenance_writes_recovery_and_schedules(monkeypatch):
    from app.core import post_response_maintenance as prm
    from app.schemas.api import ResponsePlan

    scheduled = []
    recovered = []

    async def fail_hot(**kwargs):
        raise RuntimeError("hot failed")

    async def ok_group_message(**kwargs):
        return "id"

    async def fake_recovery(**kwargs):
        recovered.append(kwargs)

    async def fake_index_generated_files(**kwargs):
        return 1

    async def fake_finalize_and_compress(**kwargs):
        return None

    async def fake_profile(**kwargs):
        return None

    def fake_schedule(coro, name):
        scheduled.append(name)
        coro.close()

    monkeypatch.setattr(prm.hot, "append_user_turn", fail_hot)
    monkeypatch.setattr(prm.gm, "append_message", ok_group_message)
    monkeypatch.setattr(prm, "write_recovery_jsonl", fake_recovery)
    monkeypatch.setattr(prm.kb_mem, "index_generated_files", fake_index_generated_files)
    monkeypatch.setattr(prm, "bg_user_profile_update", fake_profile)
    monkeypatch.setattr(prm, "schedule", fake_schedule)

    await prm.post_response_maintenance(
        req=FakeRequest(),
        user_message="hello",
        assistant_message="hi",
        tendencies={},
        plan=ResponsePlan(intent="答复", key_points=["答复"], tone="自然", length_hint="短"),
        trace_id="t1",
        generated_files=[("x.txt", "url")],
        workspace_dir="f:/tmp/ws",
        progress_messages=["working"],
        finalize_and_compress=fake_finalize_and_compress,
        debug=DebugRecorder(),
    )

    assert recovered
    assert recovered[0]["hot_write_ok"] is False
    assert recovered[0]["gm_write_ok"] is True
    assert scheduled == ["orch.index_files", "orch.finalize_compress", "orch.user_profile"]


async def test_post_response_maintenance_keeps_bot_log_out_of_group_visible_memory(monkeypatch):
    from app.core import post_response_maintenance as prm
    from app.schemas.api import ResponsePlan

    hot_calls = []
    group_calls = []
    index_calls = []

    async def fake_hot(**kwargs):
        hot_calls.append(kwargs)
        return "hot-id"

    async def fake_group(**kwargs):
        group_calls.append(kwargs)
        return "group-id"

    def fake_index(**kwargs):
        index_calls.append(kwargs)
        async def _noop():
            return 1
        return _noop()

    async def fake_finalize_and_compress(**kwargs):
        return None

    async def fake_profile(**kwargs):
        return None

    def fake_schedule(coro, name):
        coro.close()

    monkeypatch.setattr(prm.hot, "append_user_turn", fake_hot)
    monkeypatch.setattr(prm.gm, "append_message", fake_group)
    monkeypatch.setattr(prm.kb_mem, "index_generated_files", fake_index)
    monkeypatch.setattr(prm, "bg_user_profile_update", fake_profile)
    monkeypatch.setattr(prm, "schedule", fake_schedule)

    assistant_message = "visible answer\n\n<bot_log>internal facts</bot_log>"
    await prm.post_response_maintenance(
        req=FakeRequest(),
        user_message="hello",
        assistant_message=assistant_message,
        tendencies={},
        plan=ResponsePlan(intent="reply", key_points=["reply"], tone="natural", length_hint="short"),
        trace_id="t1",
        generated_files=[("x.txt", "url")],
        workspace_dir="f:/tmp/ws",
        finalize_and_compress=fake_finalize_and_compress,
        debug=DebugRecorder(),
    )

    assert hot_calls[0]["assistant_content"] == assistant_message
    bot_group_call = [call for call in group_calls if call["user_id"] is None][0]
    assert bot_group_call["content"] == "visible answer"
    assert index_calls[0]["bot_response"] == "visible answer"
