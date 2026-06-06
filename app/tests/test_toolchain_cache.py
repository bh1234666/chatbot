from __future__ import annotations

import json

from app.core import debug
from app.core import agent_state
from app.core import toolchain_cache


def test_toolchain_continue_clears_and_blocks_second_call(tmp_path, monkeypatch):
    monkeypatch.setattr(toolchain_cache.ws_tool, "_get_workspace_root", lambda: tmp_path)
    archive_id = "arch_test"
    group_id = "group"
    user_id = "user"
    trace_id = "trace_a"

    messages = [
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call_1",
                "function": {"name": "delegate", "arguments": json.dumps({"action": "spawn"})},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": json.dumps({"ok": True, "results": [{"task_id": "impl", "status": "done", "report": "wrote file"}]}),
        },
    ]
    appended = toolchain_cache.append_round(
        archive_id=archive_id,
        group_id=group_id,
        user_id=user_id,
        trace_id="old_trace",
        messages=messages,
        user_message="build feature",
    )
    assert appended["entries"] == 1

    first = toolchain_cache.continue_chain(
        archive_id=archive_id,
        group_id=group_id,
        user_id=user_id,
        trace_id=trace_id,
        reason="continue",
    )
    assert first["ok"] is True
    assert first["cache_cleared"] is True
    assert "delegate" in first["continued_toolchain_prefix"]
    assert "wrote file" in first["continued_toolchain_prefix"]

    second = toolchain_cache.continue_chain(
        archive_id=archive_id,
        group_id=group_id,
        user_id=user_id,
        trace_id=trace_id,
        reason="again",
    )
    assert second["ok"] is False
    assert second["error"] == "toolchain_already_continued_this_round"


def test_filter_tools_keeps_schema_stable_after_trace_used():
    trace_id = "trace_filter"
    debug.set_trace_id(trace_id)
    toolchain_cache.reset_trace(trace_id)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "continue_toolchain",
                "description": "resume " + ("long details " * 40),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delegate",
                "description": "delegate " + ("long details " * 40),
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]
    first = toolchain_cache.filter_tools_for_trace(tools, trace_id)
    assert len(first) == 2
    assert first is toolchain_cache.filter_tools_for_trace(tools, trace_id)
    assert len(first[0]["function"]["description"]) < len(tools[0]["function"]["description"])

    toolchain_cache._CONTINUED_TRACES.add(trace_id)
    filtered = toolchain_cache.filter_tools_for_trace(tools, trace_id)
    assert filtered is first
    assert [t["function"]["name"] for t in filtered] == ["continue_toolchain", "delegate"]
    toolchain_cache.reset_trace(trace_id)


def test_tool_schema_retry_guidance_does_not_change_tool_schema_view():
    from app.core.prompt_cache_observer import describe_prompt_cache_input

    trace_id = "trace_schema_expand"
    toolchain_cache.reset_trace(trace_id)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "workspace",
                "description": "workspace tool. " + ("Detailed usage sentence. " * 30),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "Choose one action. " + ("Long action details. " * 20),
                            "enum": ["read", "write"],
                        }
                    },
                    "required": ["action"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delegate",
                "description": "delegate helper work. " + ("Detailed usage sentence. " * 30),
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]

    slim = toolchain_cache.filter_tools_for_trace(tools, trace_id)
    before_shape = describe_prompt_cache_input(messages=[], tools=slim)
    assert slim[0]["function"]["description"] != tools[0]["function"]["description"]
    assert slim[0]["function"]["parameters"]["properties"]["action"]["enum"] == ["read", "write"]
    assert len(slim[0]["function"]["parameters"]["properties"]["action"]["description"]) < (
        len(tools[0]["function"]["parameters"]["properties"]["action"]["description"])
    )

    toolchain_cache.mark_tool_schema_retry("workspace", "missing required action", trace_id)
    still_slim = toolchain_cache.filter_tools_for_trace(tools, trace_id)
    after_shape = describe_prompt_cache_input(messages=[], tools=still_slim)
    assert still_slim is slim
    assert after_shape["tool_schema_hash"] == before_shape["tool_schema_hash"]
    assert after_shape["tool_schema_bytes"] == before_shape["tool_schema_bytes"]

    guidance = toolchain_cache.tool_schema_retry_guidance(tools, trace_id)
    assert "Tool Schema Retry Facts" in guidance
    assert tools[0]["function"]["description"] in guidance
    assert tools[1]["function"]["description"] not in guidance

    toolchain_cache.mark_tool_schema_success("workspace", trace_id)
    slim_again = toolchain_cache.filter_tools_for_trace(tools, trace_id)
    assert slim_again is slim
    assert "workspace" not in toolchain_cache.expanded_schema_tools(trace_id)
    assert toolchain_cache.tool_schema_retry_guidance(tools, trace_id) == ""


def test_toolchain_summary_includes_structured_agent_state():
    trace_id = "trace_toolchain_structured"
    agent_state.reset_trace(trace_id)
    agent_state.upsert_task_contract(
        trace_id=trace_id,
        task_id="main",
        goal="Maintain a copied project and verify the result.",
        acceptance=["tests pass"],
    )
    agent_state.add_evidence(
        trace_id=trace_id,
        source="pytest",
        status=agent_state.EVIDENCE_VERIFIED,
        summary="8 tests passed",
        task_id="verify",
    )
    agent_state.register_artifact(
        trace_id=trace_id,
        path="report.md",
        artifact_type="report",
        created_by="edit_report",
        status=agent_state.ARTIFACT_READY,
    )

    summary = toolchain_cache.summarize_messages(
        [],
        user_message="continue project work",
        trace_id=trace_id,
    )

    assert "[structured agent state]" in summary
    assert "Maintain a copied project" in summary
    assert "8 tests passed" in summary
    assert "report.md(report)" in summary
