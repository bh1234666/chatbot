from __future__ import annotations

import json

from app.core import debug
from app.core import agent_state
from app.core import toolchain_cache


def _delegate_done_messages(report: str) -> list[dict]:
    return [
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
            "content": json.dumps({
                "ok": True,
                "results": [{"task_id": "impl", "status": "done", "report": report}],
            }),
        },
    ]


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
    assert "processing_records" in first["continued_toolchain_prefix"]
    assert "delegate" not in first["continued_toolchain_prefix"]
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


def test_toolchain_continue_includes_revised_round2_plan_deliverables(tmp_path, monkeypatch):
    monkeypatch.setattr(toolchain_cache.ws_tool, "_get_workspace_root", lambda: tmp_path)
    archive_id = "arch_delivery_revision"
    group_id = "group"
    user_id = "user"
    trace_id = "trace_delivery_revision"

    messages = [
        {
            "role": "assistant",
            "content": json.dumps({
                "intent": "报告文件已准备好",
                "key_points": ["报告文件已准备好。"],
                "deliverables": ["final_report.docx"],
            }, ensure_ascii=False),
        },
    ]
    appended = toolchain_cache.append_round(
        archive_id=archive_id,
        group_id=group_id,
        user_id=user_id,
        trace_id="old_delivery_revision",
        messages=messages,
        user_message="生成报告",
    )
    assert appended["entries"] == 1

    continued = toolchain_cache.continue_chain(
        archive_id=archive_id,
        group_id=group_id,
        user_id=user_id,
        trace_id=trace_id,
        reason="continue after delivery revision",
    )
    prefix = continued["continued_toolchain_prefix"]

    assert continued["ok"] is True
    assert "assistant_plan:" in prefix
    assert "final_report.docx" in prefix
    assert "helper" not in prefix.lower()
    toolchain_cache.reset_trace(trace_id)


def test_toolchain_continue_sanitizes_revised_plan_internal_terms(tmp_path, monkeypatch):
    monkeypatch.setattr(toolchain_cache.ws_tool, "_get_workspace_root", lambda: tmp_path)
    archive_id = "arch_revised_plan_terms"
    group_id = "group"
    user_id = "user"
    trace_id = "trace_revised_plan_terms"

    toolchain_cache.append_round(
        archive_id=archive_id,
        group_id=group_id,
        user_id=user_id,
        trace_id="old_revised_plan_terms",
        messages=[{
            "role": "assistant",
            "content": json.dumps({
                "intent": "helper delegation finished",
                "key_points": ["producer evidence accepted", "background_work ready"],
                "deliverables": ["_delegate_tmp.txt", "_helpers_shared/report.md", "final_report.docx"],
            }, ensure_ascii=False),
        }],
        user_message="继续报告",
    )

    continued = toolchain_cache.continue_chain(
        archive_id=archive_id,
        group_id=group_id,
        user_id=user_id,
        trace_id=trace_id,
        reason="continue after revised plan",
    )
    prefix = continued["continued_toolchain_prefix"]

    assert "final_report.docx" in prefix
    assert "helper" not in prefix.lower()
    assert "delegate" not in prefix.lower()
    assert "producer" not in prefix.lower()
    assert "background_work" not in prefix
    assert "_helpers_shared" not in prefix
    assert "_delegate_" not in prefix
    toolchain_cache.reset_trace(trace_id)


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


def test_toolchain_cache_is_user_scoped_within_same_group(tmp_path, monkeypatch):
    monkeypatch.setattr(toolchain_cache.ws_tool, "_get_workspace_root", lambda: tmp_path)
    archive_id = "arch"
    group_id = "group"

    toolchain_cache.append_round(
        archive_id=archive_id,
        group_id=group_id,
        user_id="user_a",
        trace_id="old_trace_a",
        messages=_delegate_done_messages("user A wrote alpha_report.md"),
        user_message="build alpha report",
    )
    toolchain_cache.append_round(
        archive_id=archive_id,
        group_id=group_id,
        user_id="user_b",
        trace_id="old_trace_b",
        messages=_delegate_done_messages("user B wrote beta_report.md"),
        user_message="build beta report",
    )

    user_b = toolchain_cache.continue_chain(
        archive_id=archive_id,
        group_id=group_id,
        user_id="user_b",
        trace_id="trace_user_b",
        reason="continue beta",
    )
    assert user_b["ok"] is True
    assert "beta_report.md" in user_b["continued_toolchain_prefix"]
    assert "alpha_report.md" not in user_b["continued_toolchain_prefix"]

    user_a = toolchain_cache.continue_chain(
        archive_id=archive_id,
        group_id=group_id,
        user_id="user_a",
        trace_id="trace_user_a",
        reason="continue alpha",
    )
    assert user_a["ok"] is True
    assert "alpha_report.md" in user_a["continued_toolchain_prefix"]
    assert "beta_report.md" not in user_a["continued_toolchain_prefix"]


def test_continue_toolchain_handler_uses_current_user_scope(tmp_path, monkeypatch):
    import asyncio
    from app.llm.tools import registry

    monkeypatch.setattr(toolchain_cache.ws_tool, "_get_workspace_root", lambda: tmp_path)
    archive_id = "arch"
    group_id = "group"
    toolchain_cache.append_round(
        archive_id=archive_id,
        group_id=group_id,
        user_id="user_a",
        trace_id="old_trace_a",
        messages=_delegate_done_messages("private user A continuation facts"),
        user_message="continue A",
    )

    debug.set_trace_id("trace_handler_b")
    user_b_raw = asyncio.run(registry._handle_continue_toolchain(
        archive_id,
        group_id,
        "user_b",
        {"reason": "continue"},
    ))
    user_b = json.loads(user_b_raw)
    assert user_b["ok"] is True
    assert user_b["entries"] == 0
    assert "private user A continuation facts" not in user_b["continued_toolchain_prefix"]

    debug.set_trace_id("trace_handler_a")
    user_a_raw = asyncio.run(registry._handle_continue_toolchain(
        archive_id,
        group_id,
        "user_a",
        {"reason": "continue"},
    ))
    user_a = json.loads(user_a_raw)
    assert user_a["ok"] is True
    assert "private user A continuation facts" in user_a["continued_toolchain_prefix"]
    toolchain_cache.reset_trace("trace_handler_a")
    toolchain_cache.reset_trace("trace_handler_b")


def test_toolchain_summary_preserves_group_file_source_attribution(tmp_path, monkeypatch):
    monkeypatch.setattr(toolchain_cache.ws_tool, "_get_workspace_root", lambda: tmp_path)
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call_fetch",
                "function": {
                    "name": "fetch_group_file",
                    "arguments": json.dumps({"kb_node_id": "kb1"}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_fetch",
            "content": json.dumps({
                "ok": True,
                "path": "source.txt",
                "source_attribution": {
                    "scope": "shared_group_file",
                    "filename": "source.txt",
                    "uploader_id": "user_b",
                    "uploader_name": "Bob",
                    "current_user_match": False,
                    "current_user_relation": "other_user_upload",
                },
            }, ensure_ascii=False),
        },
    ]

    appended = toolchain_cache.append_round(
        archive_id="arch",
        group_id="group",
        user_id="user_a",
        trace_id="old_trace_fetch",
        messages=messages,
        user_message="看这个文件",
    )
    assert appended["entries"] == 1

    continued = toolchain_cache.continue_chain(
        archive_id="arch",
        group_id="group",
        user_id="user_a",
        trace_id="trace_fetch_continue",
        reason="continue file inspection",
    )

    prefix = continued["continued_toolchain_prefix"]
    assert "fetch_group_file" in prefix
    assert "source_attribution" in prefix
    assert "uploader_name=Bob" in prefix
    assert "current_user_relation=other_user_upload" in prefix
    assert "current_user_match=False" in prefix
    toolchain_cache.reset_trace("trace_fetch_continue")


def test_toolchain_summary_keeps_source_attribution_before_verbose_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(toolchain_cache.ws_tool, "_get_workspace_root", lambda: tmp_path)
    noisy = "x" * 5000
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call_fetch",
                "function": {
                    "name": "fetch_group_file",
                    "arguments": json.dumps({"kb_node_id": "kb_verbose"}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_fetch",
            "content": json.dumps({
                "ok": True,
                "action": noisy,
                "summary": noisy,
                "note": noisy,
                "path": "shared_notes.txt",
                "stdout": noisy,
                "test_summary": noisy,
                "source_attribution": {
                    "scope": "shared_group_file",
                    "filename": "shared_notes.txt",
                    "uploader_id": "user_b",
                    "uploader_name": "Bob",
                    "current_user_match": False,
                    "current_user_relation": "other_user_upload",
                },
            }, ensure_ascii=False),
        },
    ]

    appended = toolchain_cache.append_round(
        archive_id="arch",
        group_id="group",
        user_id="user_a",
        trace_id="old_trace_verbose_fetch",
        messages=messages,
        user_message="继续看群文件",
    )
    assert appended["entries"] == 1

    continued = toolchain_cache.continue_chain(
        archive_id="arch",
        group_id="group",
        user_id="user_a",
        trace_id="trace_verbose_fetch_continue",
        reason="continue verbose group file",
    )

    prefix = continued["continued_toolchain_prefix"]
    assert "source_attribution" in prefix
    assert "uploader_name=Bob" in prefix
    assert "current_user_relation=other_user_upload" in prefix
    assert "current_user_match=False" in prefix
    toolchain_cache.reset_trace("trace_verbose_fetch_continue")


def test_toolchain_summary_renames_internal_helper_terms(tmp_path, monkeypatch):
    monkeypatch.setattr(toolchain_cache.ws_tool, "_get_workspace_root", lambda: tmp_path)
    messages = _delegate_done_messages(
        "helper wrote _helpers_shared/fetch/page.txt and helper reports are ready; "
        "producer-owned output from main process is helper_producer_self_verified"
    )
    appended = toolchain_cache.append_round(
        archive_id="arch",
        group_id="group",
        user_id="user",
        trace_id="old_trace_internal_terms",
        messages=messages,
        user_message="继续刚才的网页检查",
    )
    assert appended["entries"] == 1

    continued = toolchain_cache.continue_chain(
        archive_id="arch",
        group_id="group",
        user_id="user",
        trace_id="trace_internal_terms_continue",
        reason="continue",
    )

    prefix = continued["continued_toolchain_prefix"]
    assert "processing_records=[" in prefix
    assert "available evidence" in prefix
    assert "output_self_verified" in prefix
    assert "processing record" in prefix
    assert "work unit" not in prefix
    assert "helper" not in prefix.lower()
    assert "main process" not in prefix.lower()
    assert "main thread" not in prefix.lower()
    assert "background_work" not in prefix
    assert "producer" not in prefix.lower()
    assert "_helpers_shared" not in prefix
    toolchain_cache.reset_trace("trace_internal_terms_continue")


def test_toolchain_summary_sanitizes_structured_agent_state(tmp_path, monkeypatch):
    monkeypatch.setattr(toolchain_cache.ws_tool, "_get_workspace_root", lambda: tmp_path)
    trace_id = "trace_structured_state_terms"
    agent_state.reset_trace(trace_id)
    agent_state.register_helper_resource_request(
        trace_id=trace_id,
        task_id="doc_helper",
        helper_kind="edit",
        request={
            "resource_kind": "draw",
            "needed_outputs": ["_helpers_shared/doc_helper/figure.png"],
        },
    )
    agent_state.register_artifact(
        trace_id=trace_id,
        path="_delegate_doc_helper/draft.md",
        artifact_type="helper report",
        created_by="doc_helper",
        status=agent_state.ARTIFACT_READY,
    )

    toolchain_cache.append_round(
        archive_id="arch",
        group_id="group",
        user_id="user",
        trace_id=trace_id,
        messages=[],
        user_message="continue document work",
    )
    continued = toolchain_cache.continue_chain(
        archive_id="arch",
        group_id="group",
        user_id="user",
        trace_id="trace_structured_state_continue",
        reason="continue",
    )

    prefix = continued["continued_toolchain_prefix"]
    assert "blocked_work=" in prefix
    assert "ready_artifacts=" in prefix
    assert "helper" not in prefix.lower()
    assert "delegate" not in prefix.lower()
    assert "_helpers_shared" not in prefix
    assert "_delegate_" not in prefix
    toolchain_cache.reset_trace("trace_structured_state_continue")
    agent_state.reset_trace(trace_id)


def test_group_multi_user_continuation_keeps_cache_scope_and_file_attribution(tmp_path, monkeypatch):
    import asyncio
    from app.llm.tools import registry

    monkeypatch.setattr(toolchain_cache.ws_tool, "_get_workspace_root", lambda: tmp_path)
    archive_id = "arch"
    group_id = "group"

    def fetch_messages(kb_node_id: str, filename: str, uploader_id: str, relation: str) -> list[dict]:
        return [
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": f"call_fetch_{kb_node_id}",
                    "function": {
                        "name": "fetch_group_file",
                        "arguments": json.dumps({"kb_node_id": kb_node_id}),
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": f"call_fetch_{kb_node_id}",
                "content": json.dumps({
                    "ok": True,
                    "path": filename,
                    "summary": f"fetched {filename}",
                    "source_attribution": {
                        "scope": "shared_group_file",
                        "filename": filename,
                        "uploader_id": uploader_id,
                        "uploader_name": "Alice" if uploader_id == "user_a" else "Bob",
                        "current_user_match": relation == "same_speaker_upload",
                        "current_user_relation": relation,
                    },
                }, ensure_ascii=False),
            },
        ]

    toolchain_cache.append_round(
        archive_id=archive_id,
        group_id=group_id,
        user_id="user_a",
        trace_id="old_trace_a_fetch",
        messages=fetch_messages("kb_a", "alice_plan.txt", "user_a", "same_speaker_upload"),
        user_message="继续看我刚上传的计划",
    )
    toolchain_cache.append_round(
        archive_id=archive_id,
        group_id=group_id,
        user_id="user_b",
        trace_id="old_trace_b_fetch",
        messages=fetch_messages("kb_a", "alice_plan.txt", "user_a", "other_user_upload"),
        user_message="我也看看 Alice 的计划",
    )

    debug.set_trace_id("trace_multi_user_a")
    user_a = json.loads(asyncio.run(registry._handle_continue_toolchain(
        archive_id,
        group_id,
        "user_a",
        {"reason": "continue own group file"},
    )))
    debug.set_trace_id("trace_multi_user_b")
    user_b = json.loads(asyncio.run(registry._handle_continue_toolchain(
        archive_id,
        group_id,
        "user_b",
        {"reason": "continue other user's group file"},
    )))

    assert user_a["ok"] is True
    assert user_b["ok"] is True
    prefix_a = user_a["continued_toolchain_prefix"]
    prefix_b = user_b["continued_toolchain_prefix"]
    assert "source_attribution" in prefix_a
    assert "source_attribution" in prefix_b
    assert "current_user_relation=same_speaker_upload" in prefix_a
    assert "current_user_relation=other_user_upload" in prefix_b
    assert "current_user_match=True" in prefix_a
    assert "current_user_match=False" in prefix_b
    assert "continue other user's group file" not in prefix_a
    assert "continue own group file" not in prefix_b
    toolchain_cache.reset_trace("trace_multi_user_a")
    toolchain_cache.reset_trace("trace_multi_user_b")


def test_filter_tools_reuses_slim_view_for_rebuilt_same_schema():
    trace_id = "trace_filter_rebuilt"
    toolchain_cache.reset_trace(trace_id)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "workspace",
                "description": "workspace " + ("long details " * 40),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "action " + ("long details " * 20),
                            "enum": ["read", "write"],
                        }
                    },
                    "required": ["action"],
                },
            },
        }
    ]

    first = toolchain_cache.filter_tools_for_trace(tools, trace_id)
    rebuilt = json.loads(json.dumps(tools))
    second = toolchain_cache.filter_tools_for_trace(rebuilt, trace_id)
    changed = json.loads(json.dumps(tools))
    changed[0]["function"]["description"] += " changed"
    third = toolchain_cache.filter_tools_for_trace(changed, trace_id)

    assert second is first
    assert third is not first
    assert first[0]["function"]["parameters"]["properties"]["action"]["enum"] == ["read", "write"]


def test_filter_tools_reuses_unchanged_single_tool_when_toolset_changes():
    trace_id = "trace_filter_single_reuse"
    toolchain_cache.reset_trace(trace_id)
    workspace_tool = {
        "type": "function",
        "function": {
            "name": "workspace",
            "description": "workspace " + ("long details " * 40),
            "parameters": {"type": "object", "properties": {}},
        },
    }
    delegate_tool = {
        "type": "function",
        "function": {
            "name": "delegate",
            "description": "delegate " + ("long details " * 40),
            "parameters": {"type": "object", "properties": {}},
        },
    }
    changed_delegate_tool = json.loads(json.dumps(delegate_tool))
    changed_delegate_tool["function"]["description"] += " changed"

    first = toolchain_cache.filter_tools_for_trace([workspace_tool, delegate_tool], trace_id)
    second = toolchain_cache.filter_tools_for_trace([workspace_tool, changed_delegate_tool], trace_id)

    assert second is not first
    assert second[0] is first[0]
    assert second[1] is not first[1]


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


def test_clear_tool_schema_retry_removes_transient_expansion():
    trace_id = "trace_schema_clear"
    toolchain_cache.reset_trace(trace_id)

    toolchain_cache.mark_tool_schema_retry("workspace", "bad args", trace_id)
    assert "workspace" in toolchain_cache.expanded_schema_tools(trace_id)

    toolchain_cache.clear_tool_schema_retry("workspace", trace_id, reason="model changed tool")

    assert "workspace" not in toolchain_cache.expanded_schema_tools(trace_id)


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
