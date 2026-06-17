import asyncio
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.mark.asyncio
async def test_processes_kill_with_terminate_clears_workspace_and_pause(monkeypatch, tmp_path):
    from app.core import pause_state
    from app.core.core_processes import registry
    from app.llm.tools.tool_processes import handle_processes

    monkeypatch.setattr(pause_state, "_resolve_workspace_root", lambda: str(tmp_path))

    helper_ws = tmp_path / "helper_ws"
    helper_ws.mkdir()
    (helper_ws / "artifact.txt").write_text("x", encoding="utf-8")

    async def _idle():
        await asyncio.Event().wait()

    task = asyncio.create_task(_idle())
    reg = registry()
    proc_id = await reg.register_helper(
        owner="main:trace-terminate",
        task=task,
        helper_task_id="task-hard",
        helper_workspace=str(helper_ws),
        abort_event=asyncio.Event(),
        description="terminate test helper",
        helper_kind="code",
        archive_id="arch_term",
        group_id="group_term",
        user_id="user_term",
    )
    await pause_state.save_pause(
        archive_id="arch_term",
        group_id="group_term",
        user_id="user_term",
        trace_id="trace-terminate",
        user_message="start",
        active_helpers=[{"task_id": "task-hard", "proc_id": proc_id, "workspace_path": str(helper_ws)}],
        completed_helpers=[],
    )

    try:
        result = await handle_processes({
            "action": "kill",
            "owner": "main:trace-terminate",
            "proc_id": proc_id,
            "reason": "content_deemed_useless",
            "terminate": True,
        })
        await asyncio.sleep(0)

        assert result["ok"] is True
        assert result["terminate"] is True
        assert result["terminate_mode"] == "hard"
        assert result["helper_workspace_removed"] is True
        assert not helper_ws.exists()

        snapshot = await pause_state.load_pause(
            archive_id="arch_term",
            group_id="group_term",
            user_id="user_term",
        )
        assert snapshot is None
        assert task.done()
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
