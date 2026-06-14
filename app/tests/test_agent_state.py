import json
import time

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
        assert ctx.plan_markers["source"] == "task_plan.update"
        assert ctx.plan_markers["current_stage"] == "reading_memory"
        assert ctx.plan_markers["has_deliverables"] is True
        assert "Round1 is a coarse entry route" in ctx.plan_markers["round1_scope_fact"]
        assert data["thread_plan"]["plan_markers"]["source"] == "task_plan.update"
        contract = data["contract"]
        assert contract["task_id"] == "main"
        assert contract["goal"] == "完成两个月前的数据报告任务"
        assert contract["current_stage"] == "reading_memory"
    finally:
        reset_current_thread_context(token)
        agent_state.reset_trace(trace_id)


async def test_task_plan_retains_prior_acceptance_when_update_is_weaker():
    from app.core.core_processes import (
        ThreadContext,
        reset_current_thread_context,
        set_current_thread_context,
    )
    from app.llm.tools.task_plan_tool import handle_task_plan

    trace_id = "trace_task_plan_retains_acceptance"
    debug.set_trace_id(trace_id)
    agent_state.reset_trace(trace_id)
    token = set_current_thread_context(ThreadContext(user_message="assemble docx", role_label="main"))
    try:
        first = json.loads(await handle_task_plan({
            "action": "update",
            "goal": "assemble paper.docx",
            "deliverables": ["paper.docx"],
            "acceptance": [
                "DOCX opens",
                "At least 4 comparative tables are present",
                "At least 3 figures are present",
            ],
        }))
        assert first["ok"] is True

        second = json.loads(await handle_task_plan({
            "action": "update",
            "goal": "assemble paper.docx",
            "deliverables": ["paper.docx"],
            "acceptance": ["DOCX opens", "2 comparison tables present"],
            "current_stage": "verified and delivered",
        }))

        contract = second["contract"]
        assert "At least 4 comparative tables are present" in contract["acceptance"]
        assert "At least 3 figures are present" in contract["acceptance"]
        assert "2 comparison tables present" in contract["acceptance"]
        assert contract["retained_prior_counts"]["acceptance"] == 2
        retained = contract["retained_prior_samples"]["acceptance"]
        assert "At least 4 comparative tables are present" in retained
        full_contract = agent_state.structured_status(trace_id)["contracts"][-1]
        assert any("Retained prior acceptance" in risk for risk in full_contract["risks"])
    finally:
        reset_current_thread_context(token)
        agent_state.reset_trace(trace_id)


async def test_task_plan_retained_notes_do_not_recurse_as_risks():
    from app.core.core_processes import (
        ThreadContext,
        reset_current_thread_context,
        set_current_thread_context,
    )
    from app.llm.tools.task_plan_tool import handle_task_plan

    trace_id = "trace_task_plan_retained_notes_no_risk_recursion"
    debug.set_trace_id(trace_id)
    agent_state.reset_trace(trace_id)
    token = set_current_thread_context(ThreadContext(user_message="continue", role_label="main"))
    try:
        first = json.loads(await handle_task_plan({
            "action": "update",
            "goal": "verify schema",
            "acceptance": ["Run verifier"],
            "risks": ["Schema may contain traps"],
        }))
        assert first["ok"] is True

        second = json.loads(await handle_task_plan({
            "action": "update",
            "goal": "verify schema",
            "acceptance": ["Verifier passed"],
            "risks": ["No unresolved schema trap"],
        }))
        assert second["contract"]["retained_prior_counts"]["acceptance"] == 1
        full_second = agent_state.structured_status(trace_id)["contracts"][-1]
        assert any("Retained prior acceptance" in risk for risk in full_second["risks"])

        third = json.loads(await handle_task_plan({
            "action": "update",
            "goal": "verify schema",
            "risks": ["No unresolved schema trap"],
        }))

        full_third = agent_state.structured_status(trace_id)["contracts"][-1]
        risks_text = "\n".join(full_third["risks"])
        assert "Retained prior risks" not in risks_text
        retained = full_third.get("retained_prior_contract_items", {})
        assert not any(
            "Retained prior acceptance" in item
            for item in retained.get("risks", [])
        )
    finally:
        reset_current_thread_context(token)
        agent_state.reset_trace(trace_id)


async def test_task_plan_retained_notes_replace_same_field_summary():
    from app.core.core_processes import (
        ThreadContext,
        reset_current_thread_context,
        set_current_thread_context,
    )
    from app.llm.tools.task_plan_tool import handle_task_plan

    trace_id = "trace_task_plan_retained_notes_replace"
    debug.set_trace_id(trace_id)
    agent_state.reset_trace(trace_id)
    token = set_current_thread_context(ThreadContext(user_message="continue", role_label="main"))
    try:
        await handle_task_plan({
            "action": "update",
            "goal": "verify exact report",
            "acceptance": ["Exact report order preserved", "Run verifier"],
        })
        await handle_task_plan({
            "action": "update",
            "goal": "verify exact report",
            "acceptance": ["Run verifier"],
        })
        third = json.loads(await handle_task_plan({
            "action": "update",
            "goal": "verify exact report",
            "acceptance": ["Verifier passed"],
        }))

        full_third = agent_state.structured_status(trace_id)["contracts"][-1]
        retained_notes = [
            risk for risk in full_third["risks"]
            if risk.startswith("Retained prior acceptance not repeated")
        ]
        assert len(retained_notes) == 1
        assert "Exact report order preserved" in retained_notes[0]
        assert "Run verifier" in retained_notes[0]
    finally:
        reset_current_thread_context(token)
        agent_state.reset_trace(trace_id)


async def test_task_plan_reports_order_conflict_against_exact_reference():
    from app.core.core_processes import (
        ThreadContext,
        reset_current_thread_context,
        set_current_thread_context,
    )
    from app.llm.tools.task_plan_tool import handle_task_plan

    trace_id = "trace_task_plan_exact_reference_conflict"
    debug.set_trace_id(trace_id)
    agent_state.reset_trace(trace_id)
    token = set_current_thread_context(ThreadContext(user_message="fix pipeline", role_label="main"))
    try:
        agent_state.upsert_task_contract(
            trace_id=trace_id,
            task_id="main",
            goal="fix pipeline",
            acceptance=[
                "Exact reference file fact: expected/report.txt was read as expected/golden/snapshot/reference text. "
                "If an active verifier compares output to this file, preserve line order, delimiters, visible text, "
                "and trailing blank lines according to the verifier's text-vs-byte comparison semantics.",
            ],
            evidence_required=["Exact reference evidence from expected/report.txt"],
            current_stage="reference_evidence_seen",
        )

        data = json.loads(await handle_task_plan({
            "action": "update",
            "goal": "fix pipeline",
            "key_points": ["Expected values: East, North, West; order may vary"],
            "acceptance": ["print East, North, West in any order"],
        }))

        assert data["ok"] is True
        assert "contract_conflict_facts" in data
        assert "order-insensitive language" in data["contract_conflict_facts"][0]
        assert "精确参考文件事实" in data["next_action_instruction"]
    finally:
        reset_current_thread_context(token)
        agent_state.reset_trace(trace_id)


async def test_task_plan_coding_evidence_required_is_helper_satisfiable_fact():
    from app.core.core_processes import (
        ThreadContext,
        reset_current_thread_context,
        set_current_thread_context,
    )
    from app.llm.tools.task_plan_tool import handle_task_plan
    from app.llm.tools.tool_schemas import TASK_PLAN_SCHEMA

    desc = TASK_PLAN_SCHEMA["function"]["parameters"]["properties"]["evidence_required"]["description"]
    assert "records final evidence needs, not who must collect them" in desc
    assert "code helpers can satisfy source reading" in desc

    trace_id = "trace_task_plan_coding_evidence_handoff"
    debug.set_trace_id(trace_id)
    agent_state.reset_trace(trace_id)
    token = set_current_thread_context(ThreadContext(user_message="fix tests", role_label="main"))
    try:
        data = json.loads(await handle_task_plan({
            "action": "update",
            "goal": "Fix config_loader.py",
            "acceptance": ["python -m pytest tests/test_config_loader.py passes"],
            "evidence_required": ["source code", "failing pytest output"],
        }))
        assert data["ok"] is True
        hint = data["next_action_instruction"]
        assert "final evidence needs, not a main-thread collection requirement" in hint
        assert "code helper can satisfy source reading" in hint
        assert "input_files and acceptance_checks" in hint
        assert "不表示必须由主进程收集" in hint
    finally:
        reset_current_thread_context(token)
        agent_state.reset_trace(trace_id)


async def test_task_plan_returns_compact_agent_state_summary():
    from app.core.core_processes import (
        ThreadContext,
        reset_current_thread_context,
        set_current_thread_context,
    )
    from app.llm.tools.task_plan_tool import handle_task_plan

    trace_id = "trace_task_plan_compact_summary"
    debug.set_trace_id(trace_id)
    agent_state.reset_trace(trace_id)
    token = set_current_thread_context(ThreadContext(user_message="fix service", role_label="main"))
    try:
        for idx in range(8):
            agent_state.add_evidence(
                trace_id=trace_id,
                task_id="main",
                source=f"tool_{idx}",
                kind="verification",
                status=agent_state.EVIDENCE_VERIFIED,
                summary="verified fact " + ("x" * 200),
            )

        raw = await handle_task_plan({
            "action": "update",
            "goal": "fix service",
            "acceptance": ["pytest passes"],
            "current_stage": "testing",
        })
        data = json.loads(raw)

        assert data["ok"] is True
        assert "agent_state" not in data
        assert "agent_state_summary" in data
        assert data["agent_state_summary"]["counts"]["evidence_recent"] == 8
        assert len(data["agent_state_summary"]["recent_evidence"]) == 5
        assert "agent_state(action='status')" in data["agent_state_full_status_fact"]
        assert len(raw) < 6000
    finally:
        reset_current_thread_context(token)
        agent_state.reset_trace(trace_id)


async def test_recall_thread_returns_latest_plan_markers(tmp_path):
    from app.core.core_processes import (
        ThreadContext,
        reset_current_thread_context,
        set_current_thread_context,
        update_thread_plan,
    )
    from app.llm.tools.workspace_transfer_tools import handle_recall_thread

    token = set_current_thread_context(ThreadContext(user_message="continue prior task", role_label="main"))
    try:
        await update_thread_plan(
            intent="finish current report",
            key_points=["schema evidence checked"],
            deliverables=["report.docx"],
            current_stage="assembling",
            acceptance=["report.docx opens"],
            markers={"source": "unit_test"},
        )
        data = json.loads(await handle_recall_thread(str(tmp_path), {}))
        markers = data["plan"]["markers"]
        assert markers["source"] == "unit_test"
        assert markers["current_stage"] == "assembling"
        assert markers["has_acceptance"] is True
        assert markers["deliverables_count"] == 1
        assert "Round1 is a coarse entry route" in markers["round1_scope_fact"]
    finally:
        reset_current_thread_context(token)


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


def test_delegate_helper_acceptance_inherits_explicit_main_constraints():
    from app.llm.tools.delegate_runner import (
        _helper_inherited_active_task_facts_section,
        _merge_helper_acceptance_with_active_constraints,
    )

    trace_id = "trace_helper_explicit_constraints"
    debug.set_trace_id(trace_id)
    agent_state.reset_trace(trace_id)
    try:
        agent_state.upsert_task_contract(
            trace_id=trace_id,
            task_id="main",
            goal="fix frontend",
            acceptance=[
                "Current user turn explicit constraint: Use the browser tool to reproduce the bug before fixing.",
                "List only user-facing artifacts verified against the resolved active task.",
            ],
            evidence_required=[],
            current_stage="round2_started",
        )
        merged = _merge_helper_acceptance_with_active_constraints(["existing check"])

        assert "existing check" in merged
        assert any(
            item.startswith("Main active-task fact: Current user turn explicit constraint:")
            and "browser tool to reproduce" in item
            for item in merged
        )
        assert not any("List only user-facing artifacts" in item for item in merged)

        debug.set_trace_id("trace_helper_explicit_constraints.child")
        merged_from_parent = _merge_helper_acceptance_with_active_constraints(
            ["existing check"],
            trace_id=trace_id,
        )
        assert any(
            item.startswith("Main active-task fact: Current user turn explicit constraint:")
            and "browser tool to reproduce" in item
            for item in merged_from_parent
        )
        section = _helper_inherited_active_task_facts_section(merged_from_parent)
        assert "Inherited Active-Task Facts" in section
        assert "browser tool to reproduce" in section
        assert "not automatic success or failure labels" in section
        assert "report the concrete blocker" in section
        assert "Browser evidence route fact" in section
        assert "Plain source reads and curl/plain HTTP checks" in section
    finally:
        debug.set_trace_id(trace_id)
        agent_state.reset_trace(trace_id)


def test_delegate_helper_acceptance_surfaces_exact_reference_order_conflict():
    from app.llm.tools.delegate_runner import (
        _helper_order_conflict_fact,
        _merge_helper_acceptance_with_active_constraints,
    )

    trace_id = "trace_helper_exact_reference_conflict"
    debug.set_trace_id(trace_id)
    agent_state.reset_trace(trace_id)
    try:
        agent_state.upsert_task_contract(
            trace_id=trace_id,
            task_id="main",
            goal="fix pipeline",
            acceptance=[
                "Exact reference file fact: expected/report.txt was read as expected/golden/snapshot/reference text. "
                "If an active verifier compares output to this file, preserve line order, delimiters, visible text, "
                "and trailing blank lines according to the verifier's text-vs-byte comparison semantics.",
            ],
            evidence_required=[],
            current_stage="planning",
        )
        merged = _merge_helper_acceptance_with_active_constraints(
            ["print East, North, West in any order"],
        )
        conflict = _helper_order_conflict_fact(
            prompt="The order of regions does not matter.",
            acceptance_checks=merged,
        )

        assert "Acceptance Comparison Facts" in conflict
        assert "order-insensitive language" in conflict
        assert "Exact reference file fact: expected/report.txt" in conflict
        assert "保留参考文件顺序" in conflict
    finally:
        agent_state.reset_trace(trace_id)


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
    blocked = agent_state.structured_status(trace_id)["blocked_work"]
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
    assert status["blocked_work"][0]["blocked_task_id"] == "live_report_edit"
    assert status["evidence_recent"][-1]["source"] == "background_work_resource_request"


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
    assert agent_state.structured_status(trace_id)["blocked_work"] == []


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
            "outputs_check": {"outputs_complete": True, "producer_self_verified": True},
        },
    )

    assert evidence["status"] == agent_state.EVIDENCE_VERIFIED
    artifacts = agent_state.structured_status(trace_id)["artifacts_ready"]
    assert len(artifacts) == 1
    assert artifacts[0]["path"] == "snake.html"
    assert artifacts[0]["type"] == "code"
    assert artifacts[0]["verified_by"] == "helper_producer_self_verified"


def test_structured_status_reports_facts_newer_than_contract():
    trace_id = "trace_state_freshness"
    agent_state.reset_trace(trace_id)

    agent_state.upsert_task_contract(
        trace_id=trace_id,
        task_id="main",
        goal="Fix code and verify tests",
        current_stage="diagnose",
    )
    time.sleep(0.002)
    agent_state.add_evidence(
        trace_id=trace_id,
        source="env_run",
        status=agent_state.EVIDENCE_VERIFIED,
        summary="Test command passed: 2 passed",
        kind="test",
    )

    status = agent_state.structured_status(trace_id)
    assert status["freshness"]["latest_fact_after_contract"] is True
    assert status["contracts"][0]["current_stage"] == "diagnose"
    assert status["verified_evidence_recent"][-1]["summary"] == "Test command passed: 2 passed"


def test_main_tool_facts_are_mirrored_into_agent_state():
    from app.llm.client_tools_loop import _record_main_tool_facts_in_agent_state

    trace_id = "trace_main_tool_fact_mirror"
    debug.set_trace_id(trace_id)
    agent_state.reset_trace(trace_id)

    _record_main_tool_facts_in_agent_state(
        "env_apply_replace",
        {},
        json.dumps({
            "ok": True,
            "action": "env_apply_replace",
            "path": "config_loader.py",
            "new_sha256": "abc",
        }),
        helper_kind=None,
    )
    _record_main_tool_facts_in_agent_state(
        "env_run",
        {"command": "python -m pytest tests/test_config_loader.py -v"},
        json.dumps({
            "ok": True,
            "command": "python -m pytest tests/test_config_loader.py -v",
            "test_summary": "[pytest] 2 passed in 0.02s",
        }),
        helper_kind=None,
    )

    status = agent_state.structured_status(trace_id)
    assert status["artifacts_ready"][-1]["path"] == "config_loader.py"
    assert status["artifacts_ready"][-1]["verified_by"] == "env_apply_replace"
    assert status["verified_evidence_recent"][-1]["kind"] == "test"
    assert "2 passed" in status["verified_evidence_recent"][-1]["summary"]


def test_env_read_project_file_fact_is_visible_to_delegate_guard_anchor():
    from app.llm.client_tools_loop import _record_main_tool_facts_in_agent_state
    from app.llm.tools.delegate import _current_task_anchor_for_guard

    trace_id = "trace_env_read_guard_anchor"
    debug.set_trace_id(trace_id)
    agent_state.reset_trace(trace_id)
    try:
        _record_main_tool_facts_in_agent_state(
            "env_read",
            {"path": "inbox/msg_01.txt"},
            json.dumps({
                "ok": True,
                "path": "inbox/msg_01.txt",
                "source_zone": "project",
                "sha256": "0123456789abcdef",
                "start_line": 1,
                "end_line": 12,
                "total_lines": 12,
                "truncated": False,
                "content": "urgent outage",
            }),
            helper_kind=None,
        )

        status = agent_state.structured_status(trace_id)
        assert status["verified_evidence_recent"][-1]["kind"] == "project_file_read"
        anchor = _current_task_anchor_for_guard(
            "Write the triage report.",
            [{"task_id": "write_triage_report", "kind": "edit", "prompt": "Create triage_report.txt"}],
        )
        assert "Recent verified main-thread and completed-helper evidence" in anchor
        assert "inbox/msg_01.txt" in anchor
        assert "total_lines=12" in anchor
        assert "do not by themselves prove" in anchor
    finally:
        agent_state.reset_trace(trace_id)


def test_exact_reference_read_is_mirrored_into_main_contract_for_helpers():
    from app.llm.client_tools_loop import _record_main_tool_facts_in_agent_state
    from app.llm.tools.delegate_runner import _merge_helper_acceptance_with_active_constraints

    trace_id = "trace_exact_reference_contract"
    debug.set_trace_id(trace_id)
    agent_state.reset_trace(trace_id)
    try:
        agent_state.upsert_task_contract(
            trace_id=trace_id,
            task_id="main",
            goal="produce report.txt",
            acceptance=["report.txt exists"],
            current_stage="planning",
        )
        _record_main_tool_facts_in_agent_state(
            "env_read",
            {"path": "expected/report.txt"},
            json.dumps({
                "ok": True,
                "path": "expected/report.txt",
                "content": "East: 150\nNorth: 50\nWest: 80",
                "exact_text_reference": {
                    "kind": "exact_text_reference",
                    "path": "expected/report.txt",
                    "text_facts": {
                        "line_count": 3,
                        "ends_with_newline": True,
                        "newline_counts": {"crlf": 0, "lf": 3, "cr": 0},
                    },
                },
            }),
            helper_kind=None,
        )

        status = agent_state.structured_status(trace_id)
        main_contract = status["contracts"][0]
        acceptance_text = "\n".join(main_contract["acceptance"])
        assert "Exact reference file fact: expected/report.txt" in acceptance_text
        assert "preserve line order" in acceptance_text
        assert "trailing blank lines" in acceptance_text
        assert status["verified_evidence_recent"][-1]["kind"] == "exact_text_reference"

        merged = _merge_helper_acceptance_with_active_constraints(["write report.txt"])
        assert "write report.txt" in merged
        assert any(
            item.startswith("Main active-task fact: Exact reference file fact:")
            and "expected/report.txt" in item
            and "line order" in item
            for item in merged
        )
    finally:
        agent_state.reset_trace(trace_id)


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
    assert '"status_summary"' in artifact_raw
    assert '"artifacts_ready_paths"' in artifact_raw
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
    assert '"blocked_work"' in raw
    assert '"ready_to_resume_work"' in raw
    assert '"blocked_helpers"' not in raw
    assert '"ready_to_resume_helpers"' not in raw
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
