import asyncio


def test_pause_state_is_user_scoped_within_same_group(monkeypatch, tmp_path):
    from app.core import pause_state

    monkeypatch.setattr(pause_state, "_resolve_workspace_root", lambda: str(tmp_path))

    asyncio.run(pause_state.save_pause(
        archive_id="arch",
        group_id="group",
        user_id="user_a",
        trace_id="trace_a",
        user_message="stop A",
        active_helpers=[{"task_id": "alpha"}],
        completed_helpers=[{"task_id": "alpha_done"}],
    ))
    asyncio.run(pause_state.save_pause(
        archive_id="arch",
        group_id="group",
        user_id="user_b",
        trace_id="trace_b",
        user_message="stop B",
        active_helpers=[{"task_id": "beta"}],
        completed_helpers=[],
    ))

    snapshot_b = asyncio.run(pause_state.load_pause(
        archive_id="arch",
        group_id="group",
        user_id="user_b",
    ))
    assert snapshot_b is not None
    assert snapshot_b["user_message"] == "stop B"
    assert [helper["task_id"] for helper in snapshot_b["active_helpers"]] == ["beta"]

    asyncio.run(pause_state.clear_pause(
        archive_id="arch",
        group_id="group",
        user_id="user_b",
    ))

    assert asyncio.run(pause_state.load_pause(
        archive_id="arch",
        group_id="group",
        user_id="user_b",
    )) is None
    snapshot_a = asyncio.run(pause_state.load_pause(
        archive_id="arch",
        group_id="group",
        user_id="user_a",
    ))
    assert snapshot_a is not None
    assert snapshot_a["user_message"] == "stop A"
    assert [helper["task_id"] for helper in snapshot_a["active_helpers"]] == ["alpha"]


def test_pause_state_remove_helper_only_touches_current_user(monkeypatch, tmp_path):
    from app.core import pause_state

    monkeypatch.setattr(pause_state, "_resolve_workspace_root", lambda: str(tmp_path))

    for user_id in ("user_a", "user_b"):
        asyncio.run(pause_state.save_pause(
            archive_id="arch",
            group_id="group",
            user_id=user_id,
            trace_id=f"trace_{user_id}",
            active_helpers=[{"task_id": "shared_name"}],
            completed_helpers=[{"task_id": "done"}],
        ))

    asyncio.run(pause_state.remove_helper_from_pause(
        archive_id="arch",
        group_id="group",
        user_id="user_b",
        task_id="shared_name",
    ))

    snapshot_a = asyncio.run(pause_state.load_pause(
        archive_id="arch",
        group_id="group",
        user_id="user_a",
    ))
    snapshot_b = asyncio.run(pause_state.load_pause(
        archive_id="arch",
        group_id="group",
        user_id="user_b",
    ))

    assert snapshot_a is not None
    assert [helper["task_id"] for helper in snapshot_a["active_helpers"]] == ["shared_name"]
    assert snapshot_b is not None
    assert snapshot_b["active_helpers"] == []
    assert [helper["task_id"] for helper in snapshot_b["completed_helpers"]] == ["done"]
