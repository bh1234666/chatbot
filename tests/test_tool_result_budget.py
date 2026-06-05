import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.tools.result_budget import apply_result_budget


def test_tool_result_budget_leaves_small_results_unchanged():
    payload = json.dumps({"ok": True, "content": "short"}, ensure_ascii=False)

    assert apply_result_budget("read_file", payload, max_chars=1000) == payload


def test_tool_result_budget_truncates_plain_text_as_json_envelope():
    result = apply_result_budget("workspace", "x" * 200, max_chars=80)
    data = json.loads(result)

    assert data["truncated"] is True
    assert data["original_chars"] == 200
    assert "tool result truncated" in data["content"]


def test_tool_result_budget_truncates_common_json_text_fields():
    result = apply_result_budget(
        "delegate",
        json.dumps({"ok": True, "report": "x" * 500}, ensure_ascii=False),
        max_chars=180,
    )
    data = json.loads(result)

    assert data["ok"] is True
    assert data["truncated"] is True
    assert data["original_chars"] == 500
    assert len(result) <= 220


def test_tool_result_budget_wraps_long_json_arrays():
    result = apply_result_budget(
        "search_files",
        json.dumps([{"path": f"file_{idx}.txt", "text": "x" * 20} for idx in range(50)], ensure_ascii=False),
        max_chars=160,
    )
    data = json.loads(result)

    assert data["ok"] is True
    assert data["truncated"] is True
    assert data["original_chars"] > 160
    assert "tool result truncated" in data["content"]


def test_tool_result_budget_preserves_ok_false_when_wrapping_large_object():
    result = apply_result_budget(
        "workspace",
        json.dumps({"ok": False, "items": ["x" * 40 for _ in range(20)]}, ensure_ascii=False),
        max_chars=140,
    )
    data = json.loads(result)

    assert data["ok"] is False
    assert data["truncated"] is True


def test_max_result_chars_uses_tool_override_and_default():
    from app.llm.tools.result_budget import DEFAULT_MAX_RESULT_CHARS, max_result_chars

    assert max_result_chars("delegate") == 50 * 1024
    assert max_result_chars("wait_helper") == 50 * 1024
    assert max_result_chars("unknown_tool") == DEFAULT_MAX_RESULT_CHARS


def test_soft_compact_folds_old_delegate_milestones_without_todo_boundary():
    from app.llm.message_utils import _soft_compact_redundant_tool_results

    old_report = json.dumps(
        {
            "ok": True,
            "helpers_completed": 1,
            "helpers_still_running": 0,
            "any_stuck": False,
            "report": "x" * 12000,
        },
        ensure_ascii=False,
    )
    latest_report = json.dumps(
        {"ok": True, "helpers_completed": 2, "helpers_still_running": 0, "report": "latest"},
        ensure_ascii=False,
    )
    msgs = [
        {"role": "user", "content": "make a long office artifact"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_delegate_1",
                    "type": "function",
                    "function": {"name": "delegate", "arguments": '{"action":"spawn"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_delegate_1", "content": old_report},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_delegate_2",
                    "type": "function",
                    "function": {"name": "delegate", "arguments": '{"action":"collect"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_delegate_2", "content": latest_report},
    ]

    _soft_compact_redundant_tool_results(msgs)
    folded = json.loads(msgs[2]["content"])

    assert folded["_folded"] is True
    assert folded["_redundant"] == "delegate_excerpts_already_extracted"
    assert msgs[2]["tool_call_id"] == "call_delegate_1"
    assert msgs[4]["content"] == latest_report


def test_soft_compact_preserves_delegate_task_status_fields():
    from app.llm.message_utils import _soft_compact_redundant_tool_results

    old_report = json.dumps(
        {
            "ok": True,
            "task_ok": False,
            "_task_status": "incomplete",
            "incomplete_count": 1,
            "resource_required_count": 1,
            "_evidence_policy": "failed helpers are not factual evidence",
            "helpers_completed": 1,
            "helpers_still_running": 0,
            "any_stuck": False,
            "results": [{"task_id": "bad", "report": "x" * 12000}],
        },
        ensure_ascii=False,
    )
    latest_report = json.dumps(
        {"ok": True, "helpers_completed": 2, "helpers_still_running": 0, "report": "latest"},
        ensure_ascii=False,
    )
    msgs = [
        {"role": "user", "content": "make a long office artifact"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_delegate_1",
                    "type": "function",
                    "function": {"name": "delegate", "arguments": '{"action":"spawn"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_delegate_1", "content": old_report},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_delegate_2",
                    "type": "function",
                    "function": {"name": "delegate", "arguments": '{"action":"collect"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_delegate_2", "content": latest_report},
    ]

    _soft_compact_redundant_tool_results(msgs)
    folded = json.loads(msgs[2]["content"])

    assert folded["task_ok"] is False
    assert folded["_task_status"] == "incomplete"
    assert folded["incomplete_count"] == 1
    assert folded["resource_required_count"] == 1
    assert "failed helpers" in folded["_evidence_policy"]


def test_soft_compact_delegate_fold_serializes_prompt_json_stably():
    from app.llm.message_utils import _soft_compact_redundant_tool_results

    def make_msgs(old_report: str):
        latest_report = json.dumps(
            {"ok": True, "helpers_completed": 2, "helpers_still_running": 0, "report": "latest"},
            ensure_ascii=False,
        )
        return [
            {"role": "user", "content": "make a long office artifact"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_delegate_1",
                        "type": "function",
                        "function": {"name": "delegate", "arguments": '{"action":"spawn"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_delegate_1", "content": old_report},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_delegate_2",
                        "type": "function",
                        "function": {"name": "delegate", "arguments": '{"action":"collect"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_delegate_2", "content": latest_report},
        ]

    payload_a = json.dumps(
        {
            "ok": True,
            "helpers_completed": 1,
            "helpers_still_running": 0,
            "any_stuck": False,
            "task_ok": False,
            "incomplete_count": 1,
            "resource_required_count": 0,
            "_task_status": "incomplete",
            "report": "x" * 12000,
        },
        ensure_ascii=False,
    )
    payload_b = json.dumps(
        {
            "report": "x" * 12000,
            "_task_status": "incomplete",
            "resource_required_count": 0,
            "incomplete_count": 1,
            "task_ok": False,
            "any_stuck": False,
            "helpers_still_running": 0,
            "helpers_completed": 1,
            "ok": True,
        },
        ensure_ascii=False,
    )
    msgs_a = make_msgs(payload_a)
    msgs_b = make_msgs(payload_b)

    _soft_compact_redundant_tool_results(msgs_a)
    _soft_compact_redundant_tool_results(msgs_b)

    assert msgs_a[2]["content"] == msgs_b[2]["content"]
    assert msgs_a[2]["content"].startswith('{"_evidence_policy":')


def test_fold_old_tool_messages_preserves_delegate_aggregate_status():
    from app.llm.client import _fold_old_tool_messages

    payload = json.dumps(
        {
            "ok": True,
            "task_ok": False,
            "_task_status": "incomplete",
            "incomplete_count": 1,
            "resource_required_count": 1,
            "_evidence_policy": "failed helpers are not factual evidence",
            "helpers_completed": 1,
            "helpers_still_running": 0,
            "results": [{"task_id": "bad", "report": "x" * 12000}],
        },
        ensure_ascii=False,
    )
    msgs = [
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call_delegate_1",
                "type": "function",
                "function": {"name": "delegate", "arguments": '{"action":"spawn"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call_delegate_1", "content": payload},
    ]

    _fold_old_tool_messages(msgs, keep_recent_iters=0, force_fold_size=100)
    folded = json.loads(msgs[1]["content"])

    assert folded["task_ok"] is False
    assert folded["_task_status"] == "incomplete"
    assert folded["incomplete_count"] == 1
    assert folded["resource_required_count"] == 1
    assert "failed helpers" in folded["_evidence_policy"]
