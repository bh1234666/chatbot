def test_helper_tool_events_are_published_to_environment_workflow(monkeypatch):
    from app.core.core_processes import reset_current_helper_proc_id, set_current_helper_proc_id
    from app.llm.client_tools_loop import _publish_main_tool_event

    events = []
    monkeypatch.setattr("app.core.environment_events.publish_workflow_event", events.append)

    token = set_current_helper_proc_id("helper_proc_1")
    try:
        _publish_main_tool_event(
            "main_tool_done",
            tool="edit_file",
            iteration=3,
            status="done",
            args={"path": "_env/app.js"},
            result='{"ok": true, "path": "_env/app.js"}',
            elapsed_sec=0.12,
            call_id="call_1",
        )
    finally:
        reset_current_helper_proc_id(token)

    assert len(events) == 1
    assert events[0]["proc_type"] == "helper"
    assert events[0]["proc_id"] == "helper_proc_1"
    assert events[0]["kind"] == "main_tool_done"
    assert events[0]["tool"] == "edit_file"
    assert events[0]["ok"] is True
