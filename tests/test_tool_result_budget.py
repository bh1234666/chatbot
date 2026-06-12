import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.tools.result_budget import apply_result_budget


def test_spill_text_field_marks_real_tool_result_truncated(tmp_path):
    from app.llm.tools.output_spill import spill_text_field

    result = {"ok": True}
    spill_text_field(
        result,
        root_dir=str(tmp_path),
        tool_name="bash",
        field="stdout",
        text="HEAD\n" + ("x" * 2000),
        visible_chars=100,
    )

    assert result["tool_result_truncated"] is True
    assert result["output_truncated"] is True
    assert result["stdout_truncated"] is True
    assert result["stdout"].startswith("HEAD")
    assert len(result["stdout"]) == 100
    assert (tmp_path / result["stdout_full_saved_path"]).read_text(encoding="utf-8").startswith("HEAD")


def test_tool_result_budget_leaves_small_results_unchanged():
    payload = json.dumps({"ok": True, "content": "short"}, ensure_ascii=False)

    assert apply_result_budget("read_file", payload, max_chars=1000) == payload


def test_tool_result_budget_truncates_plain_text_as_json_envelope():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        result = apply_result_budget("workspace", "x" * 200, max_chars=80, spill_root=td)
        root = Path(td)
        data = json.loads(result)

        assert data["truncated"] is True
        assert data["output_truncated"] is True
        assert data["original_chars"] == 200
        assert "tool result truncated" in data["content"]
        assert data["tool_result_truncated"] is True
        assert "recovery source for named missing details" in data["visible_excerpt_policy"]
        saved = root / data["full_result_saved_path"]
        content_saved = root / data["content_full_saved_path"]
        assert saved.is_file()
        assert content_saved.is_file()
        assert saved.read_text(encoding="utf-8") == "x" * 200
        assert content_saved.read_text(encoding="utf-8") == "x" * 200
        assert data["content_full_saved_path"] != data["full_result_saved_path"]


def test_tool_result_budget_spills_long_error_field():
    import tempfile
    from pathlib import Path

    error = "Traceback\n" + ("frame\n" * 3000)
    raw = json.dumps({"ok": False, "error": error}, ensure_ascii=False)
    with tempfile.TemporaryDirectory() as td:
        result = apply_result_budget(
            "bash",
            raw,
            max_chars=12000,
            spill_root=td,
            field_max_chars=1000,
            field_head_chars=200,
        )
        root = Path(td)
        data = json.loads(result)

        assert data["ok"] is False
        assert data["tool_result_truncated"] is True
        assert data["output_truncated"] is True
        assert data["error_truncated"] is True
        assert data["error"].startswith("Traceback")
        assert len(data["error"]) == 200
        saved = root / data["error_full_saved_path"]
        assert saved.is_file()
        assert saved.read_text(encoding="utf-8") == error


def test_tool_result_budget_spills_long_message_and_diagnostics_fields():
    import tempfile
    from pathlib import Path

    message = "failure head\n" + ("m" * 9000)
    diagnostics = "diagnostic head\n" + ("d" * 9000)
    raw = json.dumps(
        {"ok": False, "message": message, "diagnostics": diagnostics},
        ensure_ascii=False,
    )
    with tempfile.TemporaryDirectory() as td:
        result = apply_result_budget(
            "env_run",
            raw,
            max_chars=30000,
            spill_root=td,
            field_max_chars=1000,
            field_head_chars=120,
        )
        root = Path(td)
        data = json.loads(result)

        assert data["tool_result_truncated"] is True
        assert data["output_truncated"] is True
        assert data["message_truncated"] is True
        assert data["diagnostics_truncated"] is True
        assert len(data["message"]) == 120
        assert len(data["diagnostics"]) == 120
        assert (root / data["message_full_saved_path"]).read_text(encoding="utf-8") == message
        assert (root / data["diagnostics_full_saved_path"]).read_text(encoding="utf-8") == diagnostics


def test_tool_result_budget_truncates_common_json_text_fields():
    import tempfile
    from pathlib import Path

    raw = json.dumps({"ok": True, "report": "x" * 5000}, ensure_ascii=False)
    with tempfile.TemporaryDirectory() as td:
        result = apply_result_budget(
            "delegate",
            raw,
            max_chars=3000,
            spill_root=td,
        )
        root = Path(td)
        data = json.loads(result)

        assert data["ok"] is True
        assert data["truncated"] is True
        assert data["original_chars"] == 5000
        assert data["report_truncated"] is True
        assert data["report_full_saved_path"] != data["full_result_saved_path"]
        saved = root / data["full_result_saved_path"]
        field_saved = root / data["report_full_saved_path"]
        assert saved.is_file()
        assert field_saved.is_file()
        assert saved.read_text(encoding="utf-8") == raw
        assert field_saved.read_text(encoding="utf-8") == "x" * 5000


def test_tool_result_budget_spills_long_text_field_even_when_total_is_under_limit():
    import tempfile
    from pathlib import Path

    raw = json.dumps({"ok": False, "stderr": "ERR\n" + ("x" * 9000)}, ensure_ascii=False)
    with tempfile.TemporaryDirectory() as td:
        result = apply_result_budget(
            "env_run",
            raw,
            max_chars=20000,
            spill_root=td,
            field_max_chars=1000,
            field_head_chars=120,
        )
        root = Path(td)
        data = json.loads(result)

        assert data["ok"] is False
        assert data["tool_result_truncated"] is True
        assert data["output_truncated"] is True
        assert data["stderr_truncated"] is True
        assert data["stderr"].startswith("ERR")
        assert len(data["stderr"]) == 120
        saved = root / data["full_result_saved_path"]
        field_saved = root / data["stderr_full_saved_path"]
        assert saved.is_file()
        assert field_saved.is_file()
        assert saved.read_text(encoding="utf-8") == raw
        assert field_saved.read_text(encoding="utf-8") == "ERR\n" + ("x" * 9000)


def test_tool_result_budget_spills_nested_file_content_and_error_message():
    import tempfile
    from pathlib import Path

    file_content = "file head\n" + ("f" * 9000)
    error_message = "error head\n" + ("e" * 9000)
    raw = json.dumps(
        {
            "ok": False,
            "files": [{"path": "large.txt", "content": file_content}],
            "errors": [{"path": "large.txt", "message": error_message}],
        },
        ensure_ascii=False,
    )
    with tempfile.TemporaryDirectory() as td:
        result = apply_result_budget(
            "workspace",
            raw,
            max_chars=30000,
            spill_root=td,
            field_max_chars=1000,
            field_head_chars=160,
        )
        root = Path(td)
        data = json.loads(result)

        file_item = data["files"][0]
        error_item = data["errors"][0]
        assert data["tool_result_truncated"] is True
        assert data["output_truncated"] is True
        assert file_item["content_truncated"] is True
        assert error_item["message_truncated"] is True
        assert file_item["content"].startswith("file head")
        assert error_item["message"].startswith("error head")
        assert len(file_item["content"]) == 160
        assert len(error_item["message"]) == 160
        assert (root / file_item["content_full_saved_path"]).read_text(encoding="utf-8") == file_content
        assert (root / error_item["message_full_saved_path"]).read_text(encoding="utf-8") == error_message


def test_tool_result_budget_wraps_long_json_arrays():
    import tempfile
    from pathlib import Path

    raw = json.dumps([{"path": f"file_{idx}.txt", "text": "x" * 20} for idx in range(50)], ensure_ascii=False)
    with tempfile.TemporaryDirectory() as td:
        result = apply_result_budget(
            "search_files",
            raw,
            max_chars=160,
            spill_root=td,
        )
        root = Path(td)
        data = json.loads(result)

        assert data["ok"] is True
        assert data["truncated"] is True
        assert data["original_chars"] > 160
        assert "tool result truncated" in data["content"]
        saved = root / data["full_result_saved_path"]
        assert saved.is_file()
        assert saved.read_text(encoding="utf-8") == raw


def test_tool_result_budget_preserves_ok_false_when_wrapping_large_object():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        result = apply_result_budget(
            "workspace",
            json.dumps({"ok": False, "items": ["x" * 40 for _ in range(20)]}, ensure_ascii=False),
            max_chars=140,
            spill_root=td,
        )
        data = json.loads(result)

        assert data["ok"] is False
        assert data["truncated"] is True
        assert data["tool_result_truncated"] is True


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
