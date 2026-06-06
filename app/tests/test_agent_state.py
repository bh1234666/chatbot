import json

from app.core import agent_state
from app.core import debug


def test_agent_state_tool_registered_once():
    from app.llm.tools.registry import MAIN_THREAD_TOOL_METAS, ROUND2_TOOLS

    meta_names = [meta.name for meta in MAIN_THREAD_TOOL_METAS]
    tool_names = [tool["function"]["name"] for tool in ROUND2_TOOLS]
    assert meta_names.count("agent_state") == 1
    assert tool_names.count("agent_state") == 1
    assert meta_names.count("task_plan") == 1
    assert tool_names.count("task_plan") == 1


async def test_task_plan_tool_updates_thread_context_and_agent_state():
    from app.core import debug
    from app.core.core_processes import ThreadContext, set_current_thread_context, reset_current_thread_context, get_current_thread_context
    from app.llm.tools.task_plan_tool import handle_task_plan

    trace_id = "trace_task_plan_update"
    debug.set_trace_id(trace_id)
    agent_state.reset_trace(trace_id)
    token = set_current_thread_context(ThreadContext(user_message="继续", role_label="main"))
    try:
        raw = await handle_task_plan({
            "action": "update",
            "goal": "完成两个月前的数据报告任务",
            "key_points": ["memory c123 identifies the old task"],
            "deliverables": ["final_report.docx"],
            "current_stage": "reading_memory",
            "reason": "expanded memory clarified what continue refers to",
        })
        data = json.loads(raw)
        assert data["ok"] is True
        ctx = get_current_thread_context()
        assert ctx.plan_intent == "完成两个月前的数据报告任务"
        assert ctx.plan_key_points == ["memory c123 identifies the old task"]
        assert ctx.plan_deliverables == ["final_report.docx"]
        contract = data["contract"]
        assert contract["task_id"] == "main"
        assert contract["goal"] == "完成两个月前的数据报告任务"
        assert contract["current_stage"] == "reading_memory"
    finally:
        reset_current_thread_context(token)
        agent_state.reset_trace(trace_id)


def test_resource_request_becomes_ready_when_artifact_ready():
    trace_id = "trace_resource_ready"
    agent_state.reset_trace(trace_id)

    request = agent_state.register_resource_request(
        trace_id=trace_id,
        blocked_task_id="paper_edit",
        blocked_kind="edit",
        request={
            "resource_kind": "draw",
            "needed_outputs": ["chart_latency.png"],
            "resume_instruction": "Use the latency chart and finish the report.",
        },
    )
    assert request["state"] == agent_state.RESOURCE_WAITING

    agent_state.register_artifact(
        trace_id=trace_id,
        path="outputs/chart_latency.png",
        artifact_type="chart",
        created_by="draw_latency",
        status=agent_state.ARTIFACT_READY,
        verified_by="draw_helper",
    )

    ready = agent_state.ready_to_resume(trace_id)
    assert len(ready) == 1
    assert ready[0]["blocked_task_id"] == "paper_edit"
    assert ready[0]["satisfied_by"] == ["outputs/chart_latency.png"]


def test_failed_helper_is_failed_evidence_not_ready_artifact():
    trace_id = "trace_failed_helper"
    agent_state.reset_trace(trace_id)

    evidence = agent_state.register_helper_result(
        trace_id,
        "bad_stats",
        {
            "ok": False,
            "kind": "code",
            "terminal_reason": "failed",
            "report": "Command failed before producing verified statistics.",
            "files": ["stats.csv"],
            "outputs_check": {"outputs_complete": False},
        },
    )

    assert evidence["status"] == agent_state.EVIDENCE_FAILED
    assert agent_state.list_artifacts(trace_id, status=agent_state.ARTIFACT_READY) == []
    recent = agent_state.structured_status(trace_id)["evidence_recent"]
    assert recent[-1]["data"]["terminal_reason"] == "failed"


def test_resource_required_helper_creates_blocked_request():
    trace_id = "trace_blocked_helper"
    agent_state.reset_trace(trace_id)

    evidence = agent_state.register_helper_result(
        trace_id,
        "report_edit",
        {
            "ok": False,
            "kind": "edit",
            "terminal_reason": "resource_required",
            "report": "Report body is ready but the chart is missing.",
            "resource_required": {
                "resource_kind": "draw",
                "needed_outputs": ["trend_chart.png"],
                "blocked_reason": "Need a verified chart before final document layout.",
                "resume_instruction": "Insert trend_chart.png and finish layout verification.",
            },
        },
    )

    assert evidence["status"] == agent_state.EVIDENCE_PARTIAL
    blocked = agent_state.structured_status(trace_id)["blocked_helpers"]
    assert len(blocked) == 1
    assert blocked[0]["blocked_task_id"] == "report_edit"
    assert blocked[0]["requested_kind"] == "draw"
    assert blocked[0]["needed_outputs"] == ["trend_chart.png"]


def test_in_progress_resource_request_is_visible_before_helper_final_result():
    trace_id = "trace_live_blocked_helper"
    agent_state.reset_trace(trace_id)

    request = agent_state.register_helper_resource_request(
        trace_id=trace_id,
        task_id="live_report_edit",
        helper_kind="edit",
        request={
            "resource_kind": "draw",
            "needed_outputs": ["live_chart.png"],
            "blocked_reason": "Chart is required before the document can be finished.",
            "resume_instruction": "Insert live_chart.png and complete the document.",
        },
    )

    assert request["state"] == agent_state.RESOURCE_WAITING
    status = agent_state.structured_status(trace_id)
    assert status["blocked_helpers"][0]["blocked_task_id"] == "live_report_edit"
    assert status["evidence_recent"][-1]["source"] == "helper_resource_request"


def test_resource_request_can_be_refused_by_main_process():
    trace_id = "trace_resource_refused"
    agent_state.reset_trace(trace_id)
    request = agent_state.register_helper_resource_request(
        trace_id=trace_id,
        task_id="report_edit",
        helper_kind="edit",
        request={"resource_kind": "draw", "needed_outputs": ["missing.png"]},
    )

    updated = agent_state.update_resource_request(
        trace_id=trace_id,
        request_id=request["request_id"],
        state=agent_state.RESOURCE_REFUSED,
        reason="Source data is unavailable.",
    )

    assert updated is not None
    assert updated["state"] == agent_state.RESOURCE_REFUSED
    assert "Source data" in updated["reason"]
    assert agent_state.structured_status(trace_id)["blocked_helpers"] == []


def test_completed_helper_files_become_ready_artifacts():
    trace_id = "trace_ready_artifact"
    agent_state.reset_trace(trace_id)

    evidence = agent_state.register_helper_result(
        trace_id,
        "snake_impl",
        {
            "ok": True,
            "kind": "code",
            "terminal_reason": "completed",
            "report": "Implemented and smoke-tested the game.",
            "files": [{"rel_path": "snake.html"}],
            "outputs_check": {"outputs_complete": True},
        },
    )

    assert evidence["status"] == agent_state.EVIDENCE_VERIFIED
    artifacts = agent_state.structured_status(trace_id)["artifacts_ready"]
    assert len(artifacts) == 1
    assert artifacts[0]["path"] == "snake.html"
    assert artifacts[0]["type"] == "code"
    assert artifacts[0]["verified_by"] == "helper_outputs_check"


async def test_agent_state_dispatch_contract_and_artifact_flow():
    from app.llm.tools.registry import dispatch

    trace_id = "trace_dispatch_agent_state"
    debug.set_trace_id(trace_id)
    agent_state.reset_trace(trace_id)

    contract_raw = await dispatch(
        "agent_state",
        {
            "action": "upsert_contract",
            "task_id": "main",
            "goal": "Build a verified snake game.",
            "acceptance": ["snake.html exists", "basic controls are described"],
        },
        archive_id="arch",
        group_id="group",
        user_id="user",
        workspace_dir="",
    )
    assert '"ok": true' in contract_raw

    artifact_raw = await dispatch(
        "agent_state",
        {
            "action": "register_artifact",
            "path": "snake.html",
            "artifact_type": "code",
            "status": "ready",
            "created_by": "snake_impl",
            "verified_by": "smoke_test",
        },
        archive_id="arch",
        group_id="group",
        user_id="user",
        workspace_dir="",
    )
    assert '"artifacts_ready"' in artifact_raw
    assert "snake.html" in artifact_raw


async def test_delegate_status_includes_structured_agent_state():
    from app.llm.tools.delegate_actions import _handle_delegate_status

    trace_id = "trace_delegate_status_structured"
    agent_state.reset_trace(trace_id)
    agent_state.register_helper_resource_request(
        trace_id=trace_id,
        task_id="doc_edit",
        helper_kind="edit",
        request={"resource_kind": "draw", "needed_outputs": ["fig1.png"]},
    )
    agent_state.register_artifact(
        trace_id=trace_id,
        path="fig1.png",
        artifact_type="chart",
        created_by="draw_fig1",
        status=agent_state.ARTIFACT_READY,
    )

    raw = await _handle_delegate_status({}, main_owner=f"main:{trace_id}", trace_id=trace_id)
    assert '"blocked_helpers"' in raw
    assert '"ready_to_resume_helpers"' in raw
    assert '"fig1.png"' in raw


def test_agent_state_publishes_resource_and_artifact_events(monkeypatch):
    trace_id = "trace_agent_state_events"
    agent_state.reset_trace(trace_id)
    events = []

    def fake_publish(payload):
        events.append(payload)

    monkeypatch.setattr("app.core.environment_events.publish_workflow_event", fake_publish)
    agent_state.register_helper_resource_request(
        trace_id=trace_id,
        task_id="doc_edit",
        helper_kind="edit",
        request={"resource_kind": "draw", "needed_outputs": ["fig2.png"]},
    )
    agent_state.register_artifact(
        trace_id=trace_id,
        path="fig2.png",
        artifact_type="chart",
        created_by="draw_fig2",
        status=agent_state.ARTIFACT_READY,
    )

    kinds = [event["kind"] for event in events]
    assert kinds == ["helper_blocked", "helper_ready_to_resume", "artifact_ready"]
    assert events[1]["task_id"] == "doc_edit"
    assert events[1]["satisfied_by"] == ["fig2.png"]
