from __future__ import annotations


def test_helper_tool_workflow_event_carries_parent_trace(monkeypatch):
    from app.core import debug
    from app.core.core_processes import (
        set_current_helper_proc_id,
        reset_current_helper_proc_id,
        set_current_owner,
        reset_current_owner,
    )
    from app.llm.client_tools_loop import _publish_main_tool_event

    events: list[dict] = []
    monkeypatch.setattr("app.core.environment_events.publish_workflow_event", events.append)

    debug.set_trace_id("trace_a.helper_task")
    owner_token = set_current_owner("helper:trace_a:helper_task")
    proc_token = set_current_helper_proc_id("proc123")
    try:
        _publish_main_tool_event(
            "main_tool_done",
            tool="bash",
            iteration=3,
            status="done",
            args={"command": "node verify_form.cjs http://127.0.0.1:1234/"},
            result='{"ok": true}',
        )
    finally:
        reset_current_helper_proc_id(proc_token)
        reset_current_owner(owner_token)
        debug.set_trace_id("")

    assert events
    assert events[-1]["proc_type"] == "helper"
    assert events[-1]["trace_id"] == "trace_a.helper_task"
    assert events[-1]["parent_trace_id"] == "trace_a"
