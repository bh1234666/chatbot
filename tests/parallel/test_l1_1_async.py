"""L1-1: spawn_async / poll / collect / wait_any 时序测试."""
import asyncio
import json
import pytest
import time
from app.llm.tools.delegate import (
    _store_pending_result, _consume_pending_result,
    _ensure_completion_event, _peek_pending_result,
)


@pytest.mark.asyncio
async def test_collect_wakes_when_late_spawn_helper_stores_result():
    from app.llm.tools.delegate import _handle_delegate_collect

    trace_id = "trace_collect_late_store"
    task_id = "late_helper"
    _ensure_completion_event(trace_id, task_id)

    async def _finish_later():
        await asyncio.sleep(0.1)
        fake_task = asyncio.current_task()
        await _store_pending_result(
            trace_id,
            task_id,
            {"task_id": task_id, "ok": True, "terminal_reason": "completed"},
            fake_task,
        )

    helper_task = asyncio.create_task(_finish_later())
    from app.llm.tools.delegate import proc_registry
    proc_id = await proc_registry().register_helper(
        owner=f"main:{trace_id}",
        task=helper_task,
        helper_task_id=task_id,
        helper_workspace="",
        abort_event=asyncio.Event(),
        description="test helper",
    )

    try:
        start = time.monotonic()
        result_str = await _handle_delegate_collect(
            {"task_ids": [task_id], "wait_window_sec": 5},
            main_owner=f"main:{trace_id}",
            trace_id=trace_id,
        )
        elapsed = time.monotonic() - start
    finally:
        await proc_registry().unregister(proc_id)

    data = json.loads(result_str)
    assert elapsed < 1.0
    assert data["ok"]
    assert data["helpers_completed"] == 1
    assert data["wait_window_expired"] is False
    assert data["results"][0]["task_id"] == task_id


@pytest.mark.asyncio
async def test_dynamic_wait_loop_guard_wakeup_is_not_timeout():
    from app.llm.tools.delegate import _dynamic_wait_loop

    async def _helper():
        await asyncio.sleep(0.05)
        return {"task_id": "guarded", "ok": True}

    async def _guard():
        await asyncio.sleep(0)
        return True, "ok", [], []

    helper_task = asyncio.create_task(_helper())
    guard_task = asyncio.create_task(_guard())

    result = await _dynamic_wait_loop(
        [helper_task],
        asyncio.Queue(),
        wait_window_sec=1,
        min_results_to_return=None,
        guard_task=guard_task,
    )

    assert isinstance(result, list)
    assert result == [{"task_id": "guarded", "ok": True}]


@pytest.mark.asyncio
async def test_dynamic_wait_loop_zero_result_final_grace_catches_near_complete(monkeypatch):
    from app.llm.tools import delegate_wait
    from app.llm.tools.delegate import _dynamic_wait_loop

    monkeypatch.setattr(delegate_wait, "_zero_result_wait_extension_seconds", lambda _wait: 0.005)
    monkeypatch.setattr(delegate_wait, "_zero_result_final_grace_seconds", lambda _wait: 0.1)

    async def _helper():
        await asyncio.sleep(0.03)
        return {"task_id": "near_done", "ok": True}

    result = await _dynamic_wait_loop(
        [asyncio.create_task(_helper())],
        asyncio.Queue(),
        wait_window_sec=0.005,
        min_results_to_return=None,
    )

    assert isinstance(result, list)
    assert result == [{"task_id": "near_done", "ok": True}]


@pytest.mark.asyncio
async def test_pending_result_store_and_consume():
    """基础:存了能取,取了再取拿不到。"""
    trace_id = "test_trace"
    task_id = "test_task"
    fake_task = asyncio.create_task(asyncio.sleep(0))
    fake_result = {"task_id": task_id, "ok": True, "report": "done"}

    await _store_pending_result(trace_id, task_id, fake_result, fake_task)

    r1 = await _consume_pending_result(trace_id, task_id)
    assert r1 is not None
    assert r1["task_id"] == task_id

    r2 = await _consume_pending_result(trace_id, task_id)
    assert r2 is None


@pytest.mark.asyncio
async def test_duplicate_completed_response_recovers_clean_helper_facts(tmp_path):
    from app.llm.tools.delegate_actions import _already_completed_delegate_response

    trace_id = "trace_duplicate_completed_recovery"
    task_id = "patch_report_client"
    fake_task = asyncio.create_task(asyncio.sleep(0))
    await _store_pending_result(
        trace_id,
        task_id,
        {
            "task_id": task_id,
            "ok": True,
            "terminal_reason": "completed",
            "files": ["report_client.py", "api_notes.md"],
            "outputs_check": {
                "outputs_complete": True,
                "producer_self_verified": True,
                "outputs_missing": [],
                "quality_warnings": [],
            },
        },
        fake_task,
    )
    await _consume_pending_result(trace_id, task_id)

    result = await _already_completed_delegate_response(
        trace_id=trace_id,
        main_owner=f"main:{trace_id}",
        main_workspace=str(tmp_path),
        task_ids=[task_id],
        note="already done",
    )

    assert result["already_completed"] is True
    assert result["task_ok"] is True
    assert result["helpers_completed"] == 1
    assert result["success_count"] == 1
    assert result["_stage_status"] == "clean_helper_batch"
    assert result["results"][0]["_post_helper_action"] == "output_json_directly"
    assert "producer-self-verified" in result["_stage_evidence_facts"]
    assert "_action_required" not in result
    assert "final synthesis" in result["_completion_guidance"]


@pytest.mark.asyncio
async def test_completion_event_signals_on_store():
    """wait_any 用的 completion_event 应在 store 时被 set。"""
    trace_id = "test_trace2"
    task_id = "test_task_b"
    ev = _ensure_completion_event(trace_id, task_id)
    assert not ev.is_set()

    fake_task = asyncio.create_task(asyncio.sleep(0))
    await _store_pending_result(trace_id, task_id, {"ok": True}, fake_task)

    assert ev.is_set()


@pytest.mark.asyncio
async def test_peek_does_not_consume():
    """peek 不应消费 result。"""
    trace_id = "trace_peek"
    task_id = "task_peek"
    fake_task = asyncio.create_task(asyncio.sleep(0))
    await _store_pending_result(trace_id, task_id, {"task_id": task_id, "ok": True}, fake_task)

    r1 = await _peek_pending_result(trace_id, task_id)
    assert r1 is not None

    r2 = await _peek_pending_result(trace_id, task_id)
    assert r2 is not None  # 还在

    r3 = await _consume_pending_result(trace_id, task_id)
    assert r3 is not None

    r4 = await _consume_pending_result(trace_id, task_id)
    assert r4 is None  # 没了


@pytest.mark.asyncio
async def test_poll_zero_blocking():
    """poll 应快速返回(no helpers)。"""
    from app.llm.tools.delegate import _handle_delegate_poll

    start = time.monotonic()
    result_str = await _handle_delegate_poll(
        {"task_ids": ["nonexistent"], "wait_window_sec": 5},
        main_owner="main:trace_x", trace_id="trace_x",
    )
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    data = json.loads(result_str)
    assert data["ok"]
    assert data["wait_window_ignored"] is True
    assert "collect" in data["wait_window_note"]
    polled = data["polled"][0]
    assert polled["status"] == "unknown"
    assert "delegate(action='status')" in polled["hint"]
    assert "Only spawn a new task_id" in polled["hint"]


@pytest.mark.asyncio
async def test_poll_reports_completed_task_from_ledger_after_collect():
    from app.llm.tools.delegate import _handle_delegate_poll

    trace_id = "trace_poll_ledger"
    task_id = "done_then_collected"
    fake_task = asyncio.create_task(asyncio.sleep(0))
    await _store_pending_result(
        trace_id,
        task_id,
        {
            "task_id": task_id,
            "ok": True,
            "terminal_reason": "completed",
            "elapsed_sec": 1.25,
            "files": ["chart.png"],
            "outputs_check": {"outputs_complete": True, "outputs_missing": []},
        },
        fake_task,
    )
    assert await _consume_pending_result(trace_id, task_id) is not None

    result_str = await _handle_delegate_poll(
        {"task_ids": [task_id]},
        main_owner="main:trace_poll_ledger", trace_id=trace_id,
    )

    data = json.loads(result_str)
    assert data["ok"]
    polled = data["polled"][0]
    assert polled["status"] == "done_collected_or_historical"
    assert polled["outputs_complete"] is True
    assert polled["collect_now"] is False
    assert "producer evidence" in polled["hint"]


@pytest.mark.asyncio
async def test_collect_returns_immediately_when_done():
    """collect 在 result 已存时应快速返回。"""
    trace_id = "trace_c"
    task_id = "task_c"
    fake_task = asyncio.create_task(asyncio.sleep(0))
    await _store_pending_result(
        trace_id, task_id, {"task_id": task_id, "ok": True}, fake_task,
    )

    from app.llm.tools.delegate import _handle_delegate_collect

    start = time.monotonic()
    result_str = await _handle_delegate_collect(
        {"task_ids": [task_id]},
        main_owner="main:trace_c", trace_id=trace_id,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    data = json.loads(result_str)
    assert data["ok"]
    assert len(data["results"]) == 1


@pytest.mark.asyncio
async def test_collect_recovers_completed_task_from_ledger_when_pending_missing():
    from app.llm.tools.delegate import (
        _add_to_completion_ledger,
        _handle_delegate_collect,
    )

    trace_id = "trace_collect_ledger_recover"
    task_id = "gen_docx"
    await _add_to_completion_ledger(
        trace_id,
        task_id,
        {
            "task_id": task_id,
            "ok": True,
            "terminal_reason": "completed",
            "elapsed_sec": 124.5,
            "files": ["gen_docx_通信原理作业_第五章.docx"],
            "outputs_check": {"outputs_complete": True, "outputs_missing": []},
        },
    )

    start = time.monotonic()
    result_str = await _handle_delegate_collect(
        {"task_ids": [task_id], "wait_window_sec": 1},
        main_owner=f"main:{trace_id}",
        trace_id=trace_id,
    )
    elapsed = time.monotonic() - start

    data = json.loads(result_str)
    assert elapsed < 1.0
    assert data["ok"]
    assert data["still_running"] == []
    assert data["wait_window_expired"] is False
    assert len(data["results"]) == 1
    recovered = data["results"][0]
    assert recovered["task_id"] == task_id
    assert recovered["terminal_reason"] == "completed"
    assert recovered["recovered_from"] == "completion_ledger"
    assert recovered["files"] == ["gen_docx_通信原理作业_第五章.docx"]


@pytest.mark.asyncio
async def test_collect_recovers_completed_task_from_disk_when_pending_missing(tmp_path):
    from app.llm.tools.delegate import _handle_delegate_collect

    trace_id = "trace_collect_disk_recover"
    task_id = "disk_done"
    report_path = tmp_path / f".helper_{task_id}_full_report.txt"
    report_path.write_text("full helper report", encoding="utf-8")

    start = time.monotonic()
    result_str = await _handle_delegate_collect(
        {"task_ids": [task_id], "wait_window_sec": 1},
        main_owner=f"main:{trace_id}",
        trace_id=trace_id,
        main_workspace=str(tmp_path),
    )
    elapsed = time.monotonic() - start

    data = json.loads(result_str)
    assert elapsed < 1.0
    assert data["still_running"] == []
    recovered = data["results"][0]
    assert recovered["task_id"] == task_id
    assert recovered["report"] == "full helper report"
    assert recovered["recovered_from"] == "disk_full_report"
    assert recovered["ok"] is False
    assert recovered["terminal_reason"] == "unverified_disk_report"


@pytest.mark.asyncio
async def test_collect_recovers_verified_output_files_from_disk_report(tmp_path):
    from app.llm.tools.delegate import _handle_delegate_collect

    trace_id = "trace_collect_disk_verified_outputs"
    task_id = "analysis_btree"
    env_dir = tmp_path / "_env"
    env_dir.mkdir()
    output = env_dir / "algorithm_analysis_btree.md"
    output.write_text("B-tree analysis complete", encoding="utf-8")
    report_path = tmp_path / f".helper_{task_id}_full_report.txt"
    report_path.write_text(
        """
## Final Report

- **## Output files**
```json
{"files": ["_env/algorithm_analysis_btree.md"]}
```

- **## Verification recommendation**
recommend: yes, reason: file exists and checks pass
""",
        encoding="utf-8",
    )

    result_str = await _handle_delegate_collect(
        {"task_ids": [task_id], "wait_window_sec": 1},
        main_owner=f"main:{trace_id}",
        trace_id=trace_id,
        main_workspace=str(tmp_path),
    )

    data = json.loads(result_str)
    assert data["success_count"] == 1
    assert data["incomplete_count"] == 0
    recovered = data["results"][0]
    assert recovered["ok"] is True
    assert recovered["terminal_reason"] == "disk_report_outputs_verified"
    assert recovered["outputs_check"]["outputs_complete"] is True
    assert recovered["outputs_check"]["outputs_missing"] == []
    assert recovered["files"] == ["_env/algorithm_analysis_btree.md"]


@pytest.mark.asyncio
async def test_collect_keeps_disk_report_incomplete_when_declared_output_missing(tmp_path):
    from app.llm.tools.delegate import _handle_delegate_collect

    trace_id = "trace_collect_disk_missing_outputs"
    task_id = "analysis_missing"
    report_path = tmp_path / f".helper_{task_id}_full_report.txt"
    report_path.write_text(
        """
## Output files
```json
{"files": ["_env/missing.md"]}
```
""",
        encoding="utf-8",
    )

    result_str = await _handle_delegate_collect(
        {"task_ids": [task_id], "wait_window_sec": 1},
        main_owner=f"main:{trace_id}",
        trace_id=trace_id,
        main_workspace=str(tmp_path),
    )

    data = json.loads(result_str)
    assert data["success_count"] == 0
    assert data["failed_count"] == 1
    recovered = data["results"][0]
    assert recovered["ok"] is False
    assert recovered["outputs_check"]["outputs_complete"] is False
    assert recovered["outputs_check"]["outputs_missing"] == ["_env/missing.md"]


@pytest.mark.asyncio
async def test_collect_retries_malformed_disk_output_file_declaration(tmp_path):
    from app.llm.tools.delegate import _handle_delegate_collect

    trace_id = "trace_collect_disk_malformed_outputs"
    task_id = "analysis_malformed"
    env_dir = tmp_path / "_env"
    env_dir.mkdir()
    (env_dir / "analysis.md").write_text("done", encoding="utf-8")
    report_path = tmp_path / f".helper_{task_id}_full_report.txt"
    report_path.write_text(
        'Output files: {"files": ["_env/analysis.md"]}',
        encoding="utf-8",
    )

    result_str = await _handle_delegate_collect(
        {"task_ids": [task_id], "wait_window_sec": 1},
        main_owner=f"main:{trace_id}",
        trace_id=trace_id,
        main_workspace=str(tmp_path),
    )

    data = json.loads(result_str)
    assert data["success_count"] == 0
    recovered = data["results"][0]
    assert recovered["ok"] is False
    assert recovered["terminal_reason"] == "output_format_invalid"
    assert recovered["outputs_check"]["outputs_complete"] is False
    assert recovered["reported_files"] == []
    assert recovered["next_action"]["type"] == "resume_same_task_fix_output_format"
    assert "Output files" in recovered["retry_instruction"]


def test_complete_output_convergence_clears_stale_lifecycle_blockers():
    from app.llm.tools.delegate_runner import _converge_terminal_state_for_complete_outputs

    result = {
        "task_id": "paper_framework",
        "ok": True,
        "terminal_reason": "stuck",
        "interrupted": True,
        "stuck": True,
        "stuck_reason": "Consecutive tool failure after final files were copied",
        "next_action": {"type": "resume_upgraded"},
        "retry_instruction": "resume this helper",
        "outputs_check": {
            "outputs_complete": True,
            "outputs_missing": [],
            "quality_warnings": [],
        },
    }

    changed = _converge_terminal_state_for_complete_outputs(
        result,
        expected_outputs=["_env/paper_framework.md", "_env/acceptance_checklist.md"],
        kind="code",
    )

    assert changed is True
    assert result["ok"] is True
    assert result["terminal_reason"] == "completed"
    assert result["_terminal_converged_from"] == "stuck"
    assert "interrupted" not in result
    assert "stuck" not in result
    assert "next_action" not in result
    assert "retry_instruction" not in result


def test_complete_output_convergence_keeps_blocking_quality_warning_failed():
    from app.llm.tools.delegate_runner import _converge_terminal_state_for_complete_outputs

    result = {
        "task_id": "paper_docx",
        "ok": False,
        "terminal_reason": "quality_blocked",
        "stuck": True,
        "outputs_check": {
            "outputs_complete": True,
            "outputs_missing": [],
            "quality_warnings": [{"issue": "docx_too_few_paragraphs"}],
            "quality_blocked": True,
        },
    }

    changed = _converge_terminal_state_for_complete_outputs(
        result,
        expected_outputs=["_env/paper.docx"],
        kind="edit",
    )

    assert changed is False
    assert result["ok"] is False
    assert result["terminal_reason"] == "quality_blocked"
    assert result["stuck"] is True


@pytest.mark.asyncio
async def test_collect_returns_quickly_when_task_is_not_waitable():
    from app.llm.tools.delegate import _handle_delegate_collect

    trace_id = "trace_collect_unwaitable"
    task_id = "missing_helper"

    start = time.monotonic()
    result_str = await _handle_delegate_collect(
        {"task_ids": [task_id], "wait_window_sec": 300},
        main_owner=f"main:{trace_id}",
        trace_id=trace_id,
    )
    elapsed = time.monotonic() - start

    data = json.loads(result_str)
    assert elapsed < 1.0
    assert data["ok"]
    assert data["helpers_requested"] == 1
    assert data["helpers_completed"] == 0
    assert data["helpers_still_running"] == 0
    assert data["helpers_unavailable"] == 1
    assert data["results"] == []
    assert data["still_running"] == []
    assert data["wait_window_expired"] is False
    assert data["unavailable"][0]["task_id"] == task_id


@pytest.mark.asyncio
async def test_wait_any_gets_first_completed_task():

    """spawn 3 task,一个 0.1s 完成,两个不完成,wait_any 应拿到第一个。"""
    trace_id = "trace_wa"

    for tid in ["fast", "slow1", "slow2"]:
        _ensure_completion_event(trace_id, tid)

    async def _finish_fast():
        await asyncio.sleep(0.1)
        fake_t = asyncio.create_task(asyncio.sleep(0))
        await _store_pending_result(
            trace_id, "fast", {"task_id": "fast", "ok": True}, fake_t,
        )

    asyncio.create_task(_finish_fast())

    from app.llm.tools.delegate import _handle_delegate_wait_any

    start = time.monotonic()
    result_str = await _handle_delegate_wait_any(
        {"task_ids": ["fast", "slow1", "slow2"], "wait_window_sec": 5},
        main_owner="main:trace_wa", trace_id=trace_id,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 2.0
    data = json.loads(result_str)
    assert data["winner_task_id"] == "fast"
