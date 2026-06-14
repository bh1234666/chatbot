import pytest


def test_delegate_runner_resource_request_registers_parent_trace():
    from pathlib import Path

    text = Path("app/llm/tools/delegate_runner.py").read_text(encoding="utf-8")

    assert "main_trace_id = parent_trace or \"\"" in text
    call_at = text.index("agent_state.register_helper_resource_request(")
    call_block = text[call_at:text.index(")", call_at) + 1]
    assert "trace_id=main_trace_id" in call_block
    assert "trace_id=trace_id" not in call_block


@pytest.mark.asyncio
async def test_request_resource_handler_returns_freeze_payload(tmp_path):
    from app.core.core_processes import (
        set_current_owner,
        reset_current_owner,
        HELPER_OWNER_PREFIX,
    )
    from app.llm.tools.registry import dispatch

    token = set_current_owner(f"{HELPER_OWNER_PREFIX}:trace:read_task")
    try:
        raw = await dispatch(
            "request_resource",
            {
                "kind": "ocr",
                "reason": "Need stronger visual extraction before summarizing.",
                "needed_outputs": ["image_text.txt"],
                "resume_instruction": "Use the OCR result and finish the evidence report.",
            },
            archive_id="arch",
            group_id="group",
            user_id="user",
            workspace_dir=str(tmp_path),
            caller="helper",
        )
    finally:
        reset_current_owner(token)

    assert '"requires_main_resource": true' in raw
    assert '"resource_kind": "read"' in raw
    assert '"image_text.txt"' in raw
