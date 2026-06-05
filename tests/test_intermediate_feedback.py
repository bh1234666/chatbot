import json
import time
import asyncio

import pytest

from app.core.intermediate_feedback import (
    IntermediateFeedbackGate,
    INTERMEDIATE_FEEDBACK_SYSTEM,
    classify_workflow_feedback_event,
    generate_intermediate_feedback,
    intermediate_feedback_event_sink,
    publish_feedback_workflow_event,
    summarize_workflow_feedback_event,
    _intermediate_feedback_user_payload,
    validate_intermediate_feedback_message,
)


def test_intermediate_feedback_preference_zero_stays_silent():
    gate = IntermediateFeedbackGate(preference=0.0, channel="chat")
    gate.start_at = time.monotonic() - 1000
    gate.last_emit_at = gate.start_at
    assert gate.allow_consideration("helper_done") is False


def test_intermediate_feedback_agent_preference_one_always_considers_key_node():
    gate = IntermediateFeedbackGate(preference=1.0, channel="agent")
    gate.start_at = time.monotonic()
    gate.last_emit_at = gate.start_at
    gate.last_consider_at = gate.start_at
    assert gate.allow_consideration("helper_done") is True
    assert gate.allow_consideration("milestone") is True


def test_intermediate_feedback_agent_preference_one_throttles_repeated_stuck():
    now = time.monotonic()
    gate = IntermediateFeedbackGate(preference=1.0, channel="agent")
    gate.start_at = now - 120
    gate.last_emit_at = now - 120
    gate.last_consider_at = now - 120
    assert gate.allow_consideration("stuck") is True
    assert gate.allow_consideration("stuck") is False
    gate.last_event_consider_at["stuck"] = time.monotonic() - 31
    assert gate.allow_consideration("stuck") is True


def test_intermediate_feedback_user_payload_is_stable_compact_json():
    payload = _intermediate_feedback_user_payload(
        persona="bot",
        user_request="整理材料",
        recent_work="event_fact=helper_started\nhelper_task=read_a",
        event="helper_start",
        event_hint="started",
        preference=1.0,
        direct=True,
        stage="round2",
        iteration=3,
    )

    assert payload.startswith('{"current_event_facts":')
    assert "\n" in json.loads(payload)["current_event_facts"]
    assert "\n" not in payload
    assert '"preference":1.0' in payload


@pytest.mark.asyncio
async def test_generate_intermediate_feedback_uses_stable_system_prompt(monkeypatch):
    from app.core import intermediate_feedback

    seen = {}

    async def fake_chat_json(messages, *, model_spec=None, **kwargs):
        seen["messages"] = messages
        return {"should_reply": True, "message": "已开始读取材料。"}

    monkeypatch.setattr(intermediate_feedback.llm, "chat_json", fake_chat_json)

    msg = await generate_intermediate_feedback(
        persona="bot",
        user_request="整理材料",
        recent_work="event_fact=helper_started\nhelper_task=read_a",
        event="helper_start",
        preference=1.0,
        stage="round2",
        iteration=1,
    )

    assert msg == "已开始读取材料。"
    assert seen["messages"][0] == {"role": "system", "content": INTERMEDIATE_FEEDBACK_SYSTEM}
    assert seen["messages"][1]["role"] == "user"
    assert seen["messages"][1]["content"].startswith('{"current_event_facts":')


def test_intermediate_feedback_chat_is_time_gated():
    now = time.monotonic()
    gate = IntermediateFeedbackGate(preference=0.5, channel="chat")
    gate.start_at = now - 100
    gate.last_emit_at = now - 10
    gate.last_consider_at = now - 10
    assert gate.allow_consideration("helper_done") is False

    gate.last_emit_at = now - 500
    gate.last_consider_at = now - 500
    assert gate.allow_consideration("long_silence") is True


def test_intermediate_feedback_agent_has_shorter_gate():
    now = time.monotonic()
    gate = IntermediateFeedbackGate(preference=0.5, channel="agent")
    gate.start_at = now - 80
    gate.last_emit_at = now - 30
    gate.last_consider_at = now - 30
    assert gate.allow_consideration("helper_done") is True


def test_intermediate_feedback_workflow_sink_filters_by_trace():
    q1 = asyncio.Queue()
    q2 = asyncio.Queue()
    with intermediate_feedback_event_sink(q1, archive_id="a", group_id="g", user_id="u", trace_id="t1"), \
            intermediate_feedback_event_sink(q2, archive_id="a", group_id="g", user_id="u", trace_id="t2"):
        publish_feedback_workflow_event({
            "kind": "helper_start",
            "archive_id": "a",
            "group_id": "g",
            "user_id": "u",
            "trace_id": "t2",
        })

    assert q1.empty()
    event, payload = q2.get_nowait()
    assert event == "_feedback_workflow"
    assert payload["kind"] == "helper_start"


def test_helper_start_summary_marks_running_not_done():
    summary = summarize_workflow_feedback_event({
        "kind": "helper_start",
        "task_id": "rb_tree",
        "helper_kind": "code",
        "description": "Implement the red-black tree module.",
    })

    assert "event_fact=helper_started" in summary
    assert "state=running" in summary
    assert "helper_task=rb_tree" in summary
    assert "no result is available" in summary
    assert "state=done" not in summary


def test_helper_done_summary_scopes_completion_to_one_helper():
    summary = summarize_workflow_feedback_event({
        "kind": "helper_registry_done",
        "task_id": "bplus_tree",
        "helper_kind": "code",
        "status": "exited",
        "elapsed_seconds": 42.5,
        "description": "Fix and verify B+ tree package metadata.",
    })

    assert "event_fact=helper_process_exited" in summary
    assert "state=exited" in summary
    assert "helper_task=bplus_tree" in summary
    assert "left the active registry" in summary
    assert "success, blocker, resource request, or failure requires the delegate result" in summary
    assert "state=done" not in summary


def test_helper_registry_done_classifies_as_exit_not_done():
    event = classify_workflow_feedback_event({
        "kind": "helper_registry_done",
        "status": "exited",
        "task_id": "bplus_tree",
    })

    assert event == "helper_exit"


def test_helper_blocked_classifies_as_blocked_event():
    event = classify_workflow_feedback_event({
        "kind": "helper_blocked",
        "blocked_count": 2,
    })

    assert event == "helper_blocked"


def test_helper_blocked_summary_does_not_expose_unstarted_task_text():
    summary = summarize_workflow_feedback_event({
        "kind": "helper_blocked",
        "blocked_count": 2,
        "blocked_tasks": [
            {
                "task_id": "impl-rbtree",
                "helper_kind": "code",
                "mode": "hard",
                "description": "Implement Red-Black Tree with insert/delete/range benchmark details.",
            },
            {
                "task_id": "impl-skiplist",
                "helper_kind": "code",
                "mode": "hard",
                "description": "Implement Skip List with long algorithm requirements.",
            },
        ],
        "reason": "framework_first_required",
        "description": "Create a shared framework contract first.",
    })

    assert "event_fact=helper_blocked_before_start" in summary
    assert "state=blocked_not_running" in summary
    assert "blocked_task_ids=impl-rbtree, impl-skiplist" in summary
    assert "blocked_helper_kinds=code" in summary
    assert "blocked_helper_modes=hard" in summary
    assert "next_direction=build_or_refine_framework_first" in summary
    assert "did not start and are not running" in summary
    assert "Implement Red-Black Tree" not in summary
    assert "long algorithm requirements" not in summary


def test_intermediate_validator_rejects_blocked_helper_as_running():
    ok, reason = validate_intermediate_feedback_message(
        message="数据结构实现任务进行中，5个并行任务稳步推进。",
        recent_work="\n".join([
            "event_fact=helper_blocked_before_start",
            "state=blocked_not_running",
            "blocked_task_ids=impl-rbtree, impl-skiplist",
            "truth_scope=the blocked helpers did not start and are not running",
        ]),
        event="helper_blocked",
    )

    assert ok is False
    assert reason == "blocked_helper_described_as_running_or_complete"


def test_intermediate_validator_allows_blocked_helper_adjustment():
    ok, reason = validate_intermediate_feedback_message(
        message="分工启动前被拦截，我正在调整共享框架方案。",
        recent_work="\n".join([
            "event_fact=helper_blocked_before_start",
            "state=blocked_not_running",
            "next_direction=build_or_refine_framework_first",
        ]),
        event="helper_blocked",
    )

    assert ok is True
    assert reason == ""


def test_delegate_tool_done_summary_preserves_completed_and_running_sets():
    result = {
        "ok": True,
        "action": "collect",
        "helpers_requested": 3,
        "helpers_completed": 1,
        "helpers_still_running": 2,
        "results": [
            {
                "task_id": "b_tree",
                "ok": True,
                "terminal_reason": "completed",
                "outputs_check": {"outputs_complete": True},
            }
        ],
        "still_running": [
            {"task_id": "framework_scaffold"},
            {"task_id": "skip_list"},
        ],
    }
    summary = summarize_workflow_feedback_event({
        "kind": "main_tool_done",
        "tool": "delegate",
        "result_preview": json.dumps(result, ensure_ascii=False),
    })

    assert "returned_result_count=1" in summary
    assert "success_count=0" in summary
    assert "running_count=2" in summary
    assert "completed_task_ids=b_tree" in summary
    assert "running_task_ids=framework_scaffold, skip_list" in summary
    assert "event_focus_task_ids=b_tree, framework_scaffold, skip_list" in summary
    assert "label_policy=task ids, filenames, paths, and technical terms are stable labels" in summary
    assert "returned_result_count only means helper results were collected" in summary


def test_delegate_preflight_block_summary_marks_helpers_not_running():
    result = {
        "ok": False,
        "error": "framework_first_required",
        "reason": "Need one shared comparison framework before peer helpers.",
        "helpers_initially_spawned": 0,
        "preflight_guard": True,
        "instruction": "Build the common contract first.",
    }
    summary = summarize_workflow_feedback_event({
        "kind": "main_tool_done",
        "tool": "delegate",
        "result_preview": json.dumps(result, ensure_ascii=False),
    })

    assert "event_fact=helper_blocked_before_start" in summary
    assert "state=blocked_not_running" in summary
    assert "error=framework_first_required" in summary
    assert "Build the common contract first" in summary
    assert "requested=0" in summary
    assert "blocked before startup and are not running" in summary
    assert "wording_hint=if event_fact=helper_blocked_before_start is present" in summary


def test_delegate_tool_done_summary_does_not_treat_returned_failure_as_complete():
    result = {
        "ok": True,
        "action": "collect",
        "task_ok": False,
        "helpers_completed": 1,
        "helpers_still_running": 0,
        "success_count": 0,
        "incomplete_count": 1,
        "results": [
            {
                "task_id": "impl-skiplist",
                "ok": False,
                "terminal_reason": "interrupted",
                "outputs_check": {
                    "outputs_complete": False,
                    "outputs_missing": ["benchmark_skiplist.csv"],
                },
            }
        ],
    }
    summary = summarize_workflow_feedback_event({
        "kind": "main_tool_done",
        "tool": "delegate",
        "result_preview": json.dumps(result, ensure_ascii=False),
    })

    assert "returned_result_count=1" in summary
    assert "success_count=0" in summary
    assert "failed_task_ids=impl-skiplist" in summary
    assert "outputs_missing=" in summary
    assert "completed_task_ids=impl-skiplist" not in summary


def test_todo_write_summary_is_plan_not_execution_evidence():
    result = {
        "ok": True,
        "action": "todo_write",
        "counts": {"total": 3, "completed": 1, "in_progress": 1, "pending": 1},
        "todos": [
            {"id": "1", "content": "Build shared framework", "status": "completed"},
            {"id": "2", "content": "Parallel implementation of algorithm helpers", "status": "in_progress"},
            {"id": "3", "content": "Write final paper", "status": "pending"},
        ],
    }
    summary = summarize_workflow_feedback_event({
        "kind": "main_tool_done",
        "tool": "todo_write",
        "result_preview": json.dumps(result, ensure_ascii=False),
    })

    assert "todo_result:" in summary
    assert "plan_in_progress=Parallel implementation of algorithm helpers" in summary
    assert "plan_completed=Build shared framework" in summary
    assert "plan checklist only" in summary
    assert "not proof that helpers, commands, or parallel execution have started" in summary
    assert "Write final paper" not in summary


def test_intermediate_validator_rejects_todo_as_parallel_execution():
    ok, reason = validate_intermediate_feedback_message(
        message="正在并行实现5种数据结构。",
        recent_work="\n".join([
            "todo_result:",
            "plan_in_progress=Parallel implementation of algorithm helpers",
            "truth_scope=todo_write records the plan checklist only",
        ]),
        event="milestone",
    )

    assert ok is False
    assert reason == "todo_plan_described_as_execution"


def test_intermediate_validator_rejects_todo_as_completion():
    ok, reason = validate_intermediate_feedback_message(
        message="已完成算法对比分析和论文框架，正在并行撰写各部分。",
        recent_work="\n".join([
            "todo_result:",
            "plan_completed=Build shared framework",
            "plan_in_progress=Parallel implementation of algorithm helpers",
            "truth_scope=todo_write records the plan checklist only",
        ]),
        event="milestone",
    )

    assert ok is False
    assert reason == "todo_plan_described_as_completion"


def test_intermediate_validator_rejects_helper_exit_as_completion():
    ok, reason = validate_intermediate_feedback_message(
        message="B+树模块已完成。",
        recent_work="\n".join([
            "event_fact=helper_process_exited",
            "state=exited",
            "truth_scope=this only says the helper process left the active registry",
        ]),
        event="helper_exit",
    )

    assert ok is False
    assert reason == "helper_lifecycle_exit_described_as_completion"


def test_intermediate_validator_rejects_failed_delegate_as_complete():
    ok, reason = validate_intermediate_feedback_message(
        message="跳表基准数据已经完成，正在写入最终论文。",
        recent_work="\n".join([
            "delegate_result:",
            "failed_task_ids=impl-skiplist",
            "outputs_missing=benchmark_skiplist.csv, description_skiplist.txt",
            "truth_scope=completed_task_ids are the only helpers this event says are complete",
        ]),
        event="milestone",
    )

    assert ok is False
    assert reason == "failed_or_missing_delegate_described_as_complete"


def test_intermediate_validator_rejects_unrelated_helper_label():
    ok, reason = validate_intermediate_feedback_message(
        message="paper_contract 已完成，正在读取对比分析材料。",
        recent_work="\n".join([
            "delegate_result:",
            "completed_task_ids=novel_ds",
            "running_task_ids=assemble_paper",
            "event_focus_task_ids=novel_ds, assemble_paper",
            "truth_scope=completed_task_ids are successful helpers; running_task_ids are still running",
        ]),
        event="helper_done",
    )

    assert ok is False
    assert reason == "message_mentions_label_outside_current_event_facts"


def test_intermediate_validator_rejects_technical_novel_mistranslation():
    ok, reason = validate_intermediate_feedback_message(
        message="正在起草小说数据结构章节，稍后汇报进度。",
        recent_work="\n".join([
            "event_fact=helper_started",
            "state=running",
            "helper_task=novel_ds",
            "visible_work=Produce `_env/sections/novel_ds.md` — the novel data structure section for a database index paper.",
            "event_focus_task_ids=novel_ds",
            "label_policy=task ids, filenames, paths, and technical terms are stable labels; preserve them if named",
        ]),
        event="helper_start",
    )

    assert ok is False
    assert reason == "technical_term_mistranslated"


def test_intermediate_feedback_prompt_preserves_technical_labels():
    assert "Preserve technical task ids, filenames, paths, and domain terms exactly" in INTERMEDIATE_FEEDBACK_SYSTEM
    assert "do not reinterpret opaque labels" in INTERMEDIATE_FEEDBACK_SYSTEM
    assert "技术" in INTERMEDIATE_FEEDBACK_SYSTEM


def test_stage_start_validator_rejects_premature_completion():
    ok, reason = validate_intermediate_feedback_message(
        message="已完成论文框架、比较标准和验收清单，正在进行算法分析并行写作。",
        recent_work="\n".join([
            "event_fact=round2_stage_started",
            "state=planning_started",
            "milestone=round2_stage_hard_started",
            "truth_scope=this is only the start of a planning/tool stage",
        ]),
        event="stage_start",
    )

    assert ok is False
    assert reason == "stage_start_described_as_completion"


def test_stage_start_summary_limits_truth_scope():
    summary = summarize_workflow_feedback_event({
        "kind": "main_milestone",
        "milestone": "round2_stage_hard_started",
        "message": "ROUND 2 started",
    })

    assert "event_fact=planning_stage_started" in summary
    assert "no helper, file read, framework, analysis, document, or deliverable is complete yet" in summary
    assert "round2_stage_hard_started" not in summary.lower()
    assert "veryhard" not in summary.lower()


def test_intermediate_validator_rejects_internal_workflow_terms():
    ok, reason = validate_intermediate_feedback_message(
        message="计划升级为非常困难，正在重新规划round2。",
        recent_work="\n".join([
            "event_fact=planning_recheck_started",
            "state=rechecking_with_stronger_evidence",
            "truth_scope=this says the approach is being rechecked with stronger evidence",
        ]),
        event="milestone",
    )

    assert ok is False
    assert reason == "internal_workflow_term_exposed"


def test_progress_callback_uses_message_snapshot(monkeypatch):
    from app.llm.message_utils import _safe_progress

    seen = {}

    async def cb(iteration, msgs, event):
        seen["len"] = len(msgs)
        seen["event"] = event

    msgs = [{"role": "user", "content": "start"}]
    task = _safe_progress(cb, 1, msgs, "helper_done")
    msgs.append({"role": "tool", "content": "late mutation"})

    asyncio.run(task)

    assert seen == {"len": 1, "event": "helper_done"}


@pytest.mark.asyncio
async def test_drive_round2_emits_intermediate_reply_from_workflow_event(monkeypatch):
    from app.core import debug, orchestrator
    from app.core.environment_events import publish_workflow_event
    from app.schemas.api import ResponsePlan, TendencyAnalysis

    async def fake_generate_intermediate_feedback(**kwargs):
        return "我已经开始拆分处理了。"

    async def fake_round2(*args, **kwargs):
        publish_workflow_event({
            "kind": "helper_start",
            "archive_id": "a",
            "group_id": "g",
            "user_id": "u",
            "trace_id": debug.current_trace_id() or "",
            "task_id": "read_docs",
            "helper_kind": "read",
        })
        await asyncio.sleep(0.01)
        return ResponsePlan(
            intent="answer",
            key_points=["ok"],
            tone="natural",
            length_hint="short",
            avoid=[],
        )

    monkeypatch.setattr(orchestrator, "generate_intermediate_feedback", fake_generate_intermediate_feedback)
    monkeypatch.setattr(orchestrator, "_round2", fake_round2)

    debug.set_trace_id("trace-feedback-test")
    gate = IntermediateFeedbackGate(preference=1.0, channel="agent")
    gate.start_at = time.monotonic() - 100
    gate.last_emit_at = gate.start_at
    gate.last_consider_at = gate.start_at

    events = []
    async for ev_type, ev_data in orchestrator._drive_round2(
        [],
        TendencyAnalysis(tendencies={}, rationale="test"),
        persona="persona",
        workspace_dir="",
        archive_id="a",
        group_id="g",
        user_id="u",
        abort_event=asyncio.Event(),
        progress_log=[],
        think=False,
        tier="low",
        helper_lite=True,
        intermediate_feedback_gate=gate,
        needs_tools=True,
        needs_recall=False,
        prior_plan=None,
        max_iter=1,
        user_message_text="请处理这个任务",
    ):
        events.append((ev_type, ev_data))

    replies = [payload for ev_type, payload in events if ev_type == "intermediate_reply"]
    assert replies
    assert replies[0]["message"] == "我已经开始拆分处理了。"
    assert replies[0]["persona_safe"] is True
    assert events[-1][0] == "_plan"


@pytest.mark.asyncio
async def test_drive_round2_suppresses_legacy_scheduled_progress(monkeypatch):
    from app.core import debug, orchestrator
    from app.schemas.api import ResponsePlan, TendencyAnalysis

    async def fake_round2(*args, **kwargs):
        progress_cb = kwargs.get("progress_cb")
        if progress_cb is not None:
            await progress_cb(1, [{"role": "user", "content": "做复杂任务"}], "scheduled")
        await asyncio.sleep(0.01)
        return ResponsePlan(
            intent="answer",
            key_points=["ok"],
            tone="natural",
            length_hint="short",
            avoid=[],
        )

    async def fail_generate_intermediate_feedback(**kwargs):
        raise AssertionError("legacy scheduled progress should not call the LLM")

    monkeypatch.setattr(orchestrator, "generate_intermediate_feedback", fail_generate_intermediate_feedback)
    monkeypatch.setattr(orchestrator, "_round2", fake_round2)

    debug.set_trace_id("trace-scheduled-suppressed")
    gate = IntermediateFeedbackGate(preference=1.0, channel="agent")
    gate.start_at = time.monotonic() - 100
    gate.last_emit_at = gate.start_at
    gate.last_consider_at = gate.start_at

    events = []
    async for ev_type, ev_data in orchestrator._drive_round2(
        [],
        TendencyAnalysis(tendencies={}, rationale="test"),
        persona="persona",
        workspace_dir="",
        archive_id="a",
        group_id="g",
        user_id="u",
        abort_event=asyncio.Event(),
        progress_log=[],
        think=False,
        tier="low",
        helper_lite=True,
        intermediate_feedback_gate=gate,
        needs_tools=True,
        needs_recall=False,
        prior_plan=None,
        max_iter=1,
        user_message_text="请处理这个任务",
    ):
        events.append((ev_type, ev_data))

    assert [ev_type for ev_type, _ in events] == ["_plan"]
