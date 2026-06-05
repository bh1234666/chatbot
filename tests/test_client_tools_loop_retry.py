import asyncio
import json
from types import SimpleNamespace

import pytest


def test_artifact_acceptance_key_recognizes_successful_office_read():
    from app.llm.client_tools_loop import _artifact_acceptance_key

    result = json.dumps({"ok": True, "action": "read", "paragraphs": 12}, ensure_ascii=False)

    key = _artifact_acceptance_key(
        "office",
        {"action": "read", "path": "_env/db_index_paper.docx"},
        result,
    )

    assert key == "office:read:_env/db_index_paper.docx"


def test_artifact_acceptance_key_ignores_nonchecking_office_write():
    from app.llm.client_tools_loop import _artifact_acceptance_key

    result = json.dumps({"ok": True, "action": "write"}, ensure_ascii=False)

    assert _artifact_acceptance_key(
        "office",
        {"action": "write", "path": "_env/db_index_paper.docx"},
        result,
    ) is None


def test_artifact_acceptance_key_recognizes_workspace_docx_validation():
    from app.llm.client_tools_loop import _artifact_acceptance_key

    result = json.dumps({"ok": True, "stdout": "python-docx ok"}, ensure_ascii=False)

    key = _artifact_acceptance_key(
        "workspace",
        {
            "action": "run",
            "path": "_env/db_index_paper.docx",
            "command": "python verify_docx.py _env/db_index_paper.docx",
        },
        result,
    )

    assert key == "workspace:run:_env/db_index_paper.docx"


class _Msg(SimpleNamespace):
    pass


class _Choice(SimpleNamespace):
    pass


class _Resp(SimpleNamespace):
    pass


class _Collector(SimpleNamespace):
    def has_partial(self):
        return False

    def to_response(self, model):
        return self.resp


class _PartialToolCollector(SimpleNamespace):
    def has_partial(self):
        return True

    def to_response(self, model):
        return self.resp


class _SingleChunkStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def close(self):
        return None


class _FakeCompletions:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self.calls == 1:
            return _Resp(choices=[_Choice(message=_Msg(
                content="",
                tool_calls=[_delegate_call("call_delegate_1", {
                    "action": "spawn",
                    "tasks": [{"task_id": "fix_birth_death", "kind": "code", "mode": "easy"}],
                })],
            ))])
        if self.calls == 2:
            return _Resp(choices=[_Choice(message=_Msg(
                content=json.dumps({"intent": "失败了，准备回复用户"}, ensure_ascii=False),
                tool_calls=[],
            ))])
        if self.calls == 3:
            return _Resp(choices=[_Choice(message=_Msg(
                content="",
                tool_calls=[_delegate_call("call_delegate_2", {
                    "action": "spawn",
                    "tasks": [{
                        "task_id": "fix_birth_death",
                        "resume": True,
                        "kind": "code",
                        "mode": "hard",
                    }],
                })],
            ))])
        return _Resp(choices=[_Choice(message=_Msg(
            content=json.dumps({"intent": "已重试后完成", "key_points": ["完成修复"]}, ensure_ascii=False),
            tool_calls=[],
        ))])


class _FakeRepeatedPrematureFinalizeCompletions:
    def __init__(self):
        self.calls = 0
        self.kwargs_by_call = []

    async def create(self, **kwargs):
        self.calls += 1
        self.kwargs_by_call.append(kwargs)
        if self.calls == 1:
            return _Resp(choices=[_Choice(message=_Msg(
                content="",
                tool_calls=[_delegate_call("call_delegate_1", {
                    "action": "spawn",
                    "tasks": [{"task_id": "fix_birth_death", "kind": "code", "mode": "easy"}],
                })],
            ))])
        if self.calls in (2, 3):
            return _Resp(choices=[_Choice(message=_Msg(
                content=json.dumps({"intent": f"失败了，准备回复用户 {self.calls}"}, ensure_ascii=False),
                tool_calls=[],
            ))])
        if self.calls == 4:
            return _Resp(choices=[_Choice(message=_Msg(
                content="",
                tool_calls=[_delegate_call("call_delegate_2", {
                    "action": "spawn",
                    "tasks": [{
                        "task_id": "fix_birth_death",
                        "resume": True,
                        "kind": "code",
                        "mode": "hard",
                    }],
                })],
            ))])
        return _Resp(choices=[_Choice(message=_Msg(
            content=json.dumps({"intent": "已重试后完成", "key_points": ["完成修复"]}, ensure_ascii=False),
            tool_calls=[],
        ))])


class _FakeExhaustedRetryCheckpointCompletions:
    def __init__(self):
        self.calls = 0
        self.kwargs_by_call = []

    async def create(self, **kwargs):
        self.calls += 1
        self.kwargs_by_call.append(kwargs)
        if self.calls == 1:
            return _Resp(choices=[_Choice(message=_Msg(
                content="",
                tool_calls=[_delegate_call("call_delegate_1", {
                    "action": "spawn",
                    "tasks": [{"task_id": "read_chen", "kind": "read", "mode": "easy"}],
                })],
            ))])
        if self.calls <= 5:
            return _Resp(choices=[_Choice(message=_Msg(
                content=json.dumps({"intent": f"带缺口收尾 {self.calls}"}, ensure_ascii=False),
                tool_calls=[],
            ))])
        if self.calls == 6:
            return _Resp(choices=[_Choice(message=_Msg(
                content="",
                tool_calls=[_delegate_call("call_delegate_2", {
                    "action": "spawn",
                    "tasks": [{
                        "task_id": "read_chen",
                        "resume": True,
                        "kind": "read",
                        "mode": "hard",
                    }],
                })],
            ))])
        return _Resp(choices=[_Choice(message=_Msg(
            content=json.dumps({"intent": "读取完成后收尾", "key_points": ["read_chen 已补读"]}, ensure_ascii=False),
            tool_calls=[],
        ))])


class _FakeCommitAfterDelegateFailureCompletions:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return _Resp(choices=[_Choice(message=_Msg(
                content="",
                tool_calls=[_delegate_call("call_delegate_1", {
                    "action": "spawn",
                    "tasks": [{"task_id": "gen_summary", "kind": "general", "mode": "easy"}],
                })],
            ))])
        if self.calls == 2:
            return _Resp(choices=[_Choice(message=_Msg(
                content="",
                tool_calls=[_tool_call("call_commit_1", "commit_to_main", {
                    "paths": ["shorttest_summary.md"],
                })],
            ))])
        return _Resp(choices=[_Choice(message=_Msg(
            content=json.dumps({
                "intent": "已生成并验证 shorttest_summary.md",
                "key_points": ["shorttest_summary.md 已提交到主工作区"],
                "deliverables": ["shorttest_summary.md"],
            }, ensure_ascii=False),
            tool_calls=[],
        ))])


def _delegate_call(call_id, args):
    return _tool_call(call_id, "delegate", args)


def _tool_call(call_id, name, args):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(args, ensure_ascii=False),
        ),
    )


def _serialize_assistant_message(msg):
    return {
        "role": "assistant",
        "content": getattr(msg, "content", "") or "",
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in (getattr(msg, "tool_calls", None) or [])
        ],
    }


def test_forced_finalize_does_not_collect_generic_path_fields():
    from pathlib import Path

    src = Path("app/llm/client_tools_loop.py").read_text(encoding="utf-8")
    assert 'r\'"path":\\s*"' not in src
    assert "committed_files|files|outputs" in src
    assert "saved_path" in src


def test_unparsed_tool_markup_is_detected():
    from app.llm.client_tools_loop import _looks_like_unparsed_tool_markup

    assert _looks_like_unparsed_tool_markup("<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name=\"run_shell\">")
    assert _looks_like_unparsed_tool_markup("<tool_call>{}</tool_call>")
    assert _looks_like_unparsed_tool_markup('<Read file="scripts/check_project.py" />')
    assert _looks_like_unparsed_tool_markup('<Write file="docs/report.md">')
    assert _looks_like_unparsed_tool_markup('<Glob pattern="**/*.py" />')
    assert _looks_like_unparsed_tool_markup('<env_read path="app/core/context.py" />')
    assert _looks_like_unparsed_tool_markup('<env_search pattern="truncated" path="tests" />')
    assert not _looks_like_unparsed_tool_markup('{"intent":"done"}')


def test_tool_names_from_schemas_collects_current_loop_tools():
    from app.llm.client_tools_loop import _tool_names_from_schemas

    tools = [
        {"type": "function", "function": {"name": "ocr", "parameters": {}}},
        {"type": "function", "function": {"name": "workspace", "parameters": {}}},
        {"type": "function", "function": {"name": "", "parameters": {}}},
    ]

    assert _tool_names_from_schemas(tools) == {"ocr", "workspace"}


@pytest.mark.asyncio
async def test_non_timeout_stream_failure_injects_recovery_and_continues(monkeypatch):
    from app.llm import client as llm_client
    from app.llm.client_tools_loop import chat_with_tools_loop

    monkeypatch.setattr(
        llm_client,
        "_legacy_model_spec",
        lambda lite=False, reasoning="high": SimpleNamespace(
            model="fake", reasoning=reasoning, provider=None,
        ),
    )
    monkeypatch.setattr(llm_client, "_client_for_spec", lambda spec: SimpleNamespace())
    monkeypatch.setattr(llm_client, "_thinking_extra_body", lambda reasoning, provider=None: {})
    monkeypatch.setattr(llm_client, "_maybe_clear_stale_upgrade", lambda *a, **k: None)
    monkeypatch.setattr(llm_client, "_try_extract_json_locally", lambda content: None)
    monkeypatch.setattr(llm_client, "_is_thinking_enabled", lambda extra: False)

    calls = {"n": 0}

    async def fake_stream(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("streaming failed at iter 1: APIConnectionError: Connection error.")
        return (
            _Resp(choices=[_Choice(message=_Msg(
                content=json.dumps({"intent": "recovered", "key_points": ["ok"]}),
                tool_calls=[],
            ))]),
            _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
            "ok",
        )

    monkeypatch.setattr(llm_client, "_call_llm_streaming_with_idle", fake_stream)

    content, msgs = await chat_with_tools_loop(
        [{"role": "system", "content": "return json"}, {"role": "user", "content": "work"}],
        [{"type": "function", "function": {"name": "delegate", "parameters": {"type": "object"}}}],
        dispatcher=lambda name, args: "{}",
        require_first_tool_call=False,
    )

    assert calls["n"] == 2
    assert json.loads(content)["intent"] == "recovered"
    assert any(
        "llm_call_recovery" in (m.get("content") or "")
        for m in msgs
        if isinstance(m, dict)
    )


def test_tools_loop_shape_labels_align_with_usage_tags():
    from app.core.cache_report import _shape_label_tag_hint
    from app.llm.client_tools_loop import _tools_loop_shape_label, _tools_loop_usage_tag

    assert _tools_loop_shape_label(2, None) == "tools_loop.iter2.main"
    assert _tools_loop_usage_tag(None) == "main"
    assert _tools_loop_shape_label(2, None, "no tools") == "tools_loop.iter2.main.call.no_tools"
    assert _shape_label_tag_hint(_tools_loop_shape_label(2, None, "no tools")) == "main"
    assert _tools_loop_shape_label(3, "read docs", "final cleanup") == (
        "tools_loop.iter3.helper.read_docs.call.final_cleanup"
    )
    assert _tools_loop_usage_tag("read docs") == "helper.read_docs"
    assert _shape_label_tag_hint(_tools_loop_shape_label(3, "read docs", "final cleanup")) == (
        "helper.read_docs"
    )


def test_large_tool_result_uses_structured_summary_before_truncation():
    from app.llm.client_tools_loop import _summarize_large_tool_result

    payload = {
        "ok": True,
        "task_ok": False,
        "helpers_completed": 2,
        "results": [
            {
                "task_id": "read_big_source",
                "kind": "read",
                "ok": True,
                "terminal_reason": "completed",
                "report": "VERDICT: PASS\n" + "full evidence line\n" * 2000,
                "outputs_check": {"outputs_complete": True},
                "files": ["read_big_source_extracted.txt"],
            }
        ],
        "stdout": "raw content\n" * 5000,
    }

    summary = _summarize_large_tool_result(
        "delegate",
        json.dumps(payload, ensure_ascii=False),
        2500,
    )

    assert summary is not None
    parsed = json.loads(summary.split("\n[structured summary shortened]", 1)[0])
    assert parsed["summarized"] is True
    assert parsed["action"] == "delegate"
    assert parsed["result_items"][0]["task_id"] == "read_big_source"
    assert parsed["result_items"][0]["files"] == ["read_big_source_extracted.txt"]
    assert "full evidence line\nfull evidence line\nfull evidence line\nfull evidence line" not in summary


def test_structured_tool_summary_compacts_read_file_content_and_long_lists():
    from app.llm.client_tools_loop import _summarize_large_tool_result

    payload = {
        "ok": True,
        "action": "read_file",
        "path": "_helpers_shared/cache_probe/cache_probe_evidence.txt",
        "total_lines": 200,
        "shown_range": [1, 200],
        "content": "line with evidence\n" * 500,
        "env_skipped_read_evidence": [f"_env/app/file_{idx}.py" for idx in range(80)],
    }

    summary = _summarize_large_tool_result(
        "read_file",
        json.dumps(payload, ensure_ascii=False),
        4096,
    )

    assert summary is not None
    parsed = json.loads(summary)
    assert parsed["summarized"] is True
    assert parsed["path"] == "_helpers_shared/cache_probe/cache_probe_evidence.txt"
    assert parsed["total_lines"] == 200
    assert parsed["shown_range"] == [1, 200]
    assert parsed["content_excerpt"].count("line with evidence") < 100
    assert "env_skipped_read_evidence" not in parsed


def test_structured_tool_summary_can_force_medium_read_file_compaction():
    from app.llm.client_tools_loop import _summarize_large_tool_result

    payload = {
        "ok": True,
        "action": "read_file",
        "path": "_env/app/core/cache_report.py",
        "total_lines": 1304,
        "shown_range": [650, 750],
        "content": "targeted code line\n" * 200,
        "truncated": False,
    }
    raw = json.dumps(payload, ensure_ascii=False)
    assert len(raw) < 8192

    assert _summarize_large_tool_result("read_file", raw, 8192) is None
    forced = _summarize_large_tool_result("read_file", raw, 8192, force=True)

    assert forced is not None
    parsed = json.loads(forced)
    assert parsed["path"] == "_env/app/core/cache_report.py"
    assert parsed["shown_range"] == [650, 750]
    assert "content_excerpt" in parsed
    assert "P44 summarized by head/tail fallback -" not in forced


def test_compact_structured_summary_keeps_search_evidence_without_large_payloads():
    from app.llm.client_tools_loop import _summarize_large_tool_result

    payload = {
        "ok": True,
        "action": "search_in_file",
        "path": "_env/app/core/cache_report.py",
        "pattern": "def ",
        "is_regex": True,
        "matches": [
            {
                "line": idx,
                "preview": f"def function_{idx}(): " + ("body " * 80),
                "extra": "x" * 500,
            }
            for idx in range(1, 30)
        ],
    }

    summary = _summarize_large_tool_result(
        "search_in_file",
        json.dumps(payload, ensure_ascii=False),
        4096,
        force=True,
        compact=True,
    )

    assert summary is not None
    parsed = json.loads(summary)
    assert parsed["path"] == "_env/app/core/cache_report.py"
    assert parsed["pattern"] == "def "
    assert parsed["matches"][0]["line"] == 1
    assert "function_1" in parsed["matches"][0]["preview"]
    assert parsed["matches_more"] == 23
    assert "extra" not in summary
    assert "summary_policy" not in parsed
    assert parsed["policy"].startswith("compact")
    assert len(summary) < 2600


def test_compact_structured_summary_uses_shorter_read_file_excerpt():
    from app.llm.client_tools_loop import _summarize_large_tool_result

    payload = {
        "ok": True,
        "action": "read_file",
        "path": "_env/app/core/cache_report.py",
        "total_lines": 1400,
        "shown_range": [200, 420],
        "content": "important source evidence line\n" * 220,
    }

    normal = _summarize_large_tool_result(
        "read_file",
        json.dumps(payload, ensure_ascii=False),
        8192,
        force=True,
        compact=False,
    )
    compact = _summarize_large_tool_result(
        "read_file",
        json.dumps(payload, ensure_ascii=False),
        8192,
        force=True,
        compact=True,
    )

    assert normal is not None
    assert compact is not None
    normal_data = json.loads(normal)
    compact_data = json.loads(compact)
    assert compact_data["path"] == payload["path"]
    assert compact_data["shown_range"] == [200, 420]
    assert len(compact_data["content_excerpt"]) < len(normal_data["content_excerpt"])
    assert "important source evidence line" in compact_data["content_excerpt"]


def test_compact_read_file_summary_adds_outline_anchors_for_targeted_followups():
    from app.llm.client_tools_loop import _summarize_large_tool_result

    content = "\n".join(
        [
            '   1: """module docs"""',
            "   2: from __future__ import annotations",
            "  10: def parse_debug_log_text(text: str) -> CacheReport:",
            "  40: class CacheStats:",
            "  80: def _warm_stats(stats, skip_first=2):",
            " 120: def evaluate_warm_hit_rate_gate(report, minimum_by_tag):",
            " 180: def evaluate_shape_coverage_gate(report, minimum_by_tag):",
            " 220: def load_shape_coverage_gate_baseline(path):",
        ]
        + [f"{idx:4d}: body line {idx}" for idx in range(221, 520)]
    )
    payload = {
        "ok": True,
        "action": "read_file",
        "path": "_env/app/core/cache_report.py",
        "total_lines": 520,
        "shown_range": [1, 520],
        "content": content,
    }

    summary = _summarize_large_tool_result(
        "read_file",
        json.dumps(payload, ensure_ascii=False),
        8192,
        force=True,
        compact=True,
    )

    assert summary is not None
    parsed = json.loads(summary)
    assert any("def _warm_stats" in item for item in parsed["content_outline"])
    assert any("class CacheStats" in item for item in parsed["content_outline"])
    assert len(parsed["content_excerpt"]) <= 180
    assert parsed["content_original_chars"] == len(content)


def test_compact_read_file_full_scan_summary_stays_small_with_outline():
    from app.llm.client_tools_loop import _summarize_large_tool_result

    content = "\n".join(
        [f"{idx:4d}: def function_{idx}(): pass" for idx in range(1, 80)]
        + [f"{idx:4d}: ordinary implementation detail {idx} {'x' * 80}" for idx in range(80, 500)]
    )
    payload = {
        "ok": True,
        "action": "read_file",
        "path": "_env/app/core/large.py",
        "total_lines": 500,
        "shown_range": [1, 500],
        "content": content,
    }

    summary = _summarize_large_tool_result(
        "read_file",
        json.dumps(payload, ensure_ascii=False),
        8192,
        force=True,
        compact=True,
    )

    assert summary is not None
    parsed = json.loads(summary)
    assert len(summary) < 2200
    assert len(parsed["content_outline"]) == 18
    assert parsed["content_excerpt"].startswith("   1: def function_1")
    assert "ordinary implementation detail 499" not in summary


def test_read_helper_prompt_adds_completion_discipline_without_affecting_code_helpers():
    from app.llm.tools.delegate_runner import _build_helper_user_prompt

    read_prompt = _build_helper_user_prompt(
        prompt="Inspect source files and report functions.",
        dynamic_prompt_prefix_parts=[],
        kind="read",
    )
    code_prompt = _build_helper_user_prompt(
        prompt="Implement a module.",
        dynamic_prompt_prefix_parts=[],
        kind="code",
    )

    assert read_prompt.startswith("## Read Helper Operating Contract")
    assert "return the concise handoff report" in read_prompt
    assert "用搜索、结构和定向范围取证" in read_prompt
    assert "## Read Helper Operating Contract" not in code_prompt


def test_forced_finalize_empty_evidence_is_explicitly_not_complete():
    from pathlib import Path

    src = Path("app/llm/client_tools_loop.py").read_text(encoding="utf-8")
    assert "本轮处理未完成,尚未取得可核查结果" in src
    assert "This turn produced no verifiable tool result" in src
    assert "不能声称完成或给出具体结论" in src


def test_named_tool_call_stream_uses_short_idle_budget():
    from pathlib import Path

    src = Path("app/llm/client.py").read_text(encoding="utf-8")
    assert "has_named_tool_call" in src
    assert "args_are_parseable" in src
    assert "json.loads(args_text)" in src
    assert "chunk_idle_budget = min(chunk_idle_budget, 12.0)" in src


def test_delegate_incomplete_helpers_are_not_success_findings():
    from app.llm.client_tools_loop import (
        _delegate_item_is_incomplete,
        _delegate_items_from_result,
    )

    payload = {
        "ok": True,
        "task_ok": False,
        "results": [
            {
                "task_id": "bad_stats",
                "ok": False,
                "terminal_reason": "resource_required",
                "report": "扫描文件总数: 999\n总行数: 999999",
                "outputs_check": {"outputs_complete": False},
            },
            {
                "task_id": "good_stats",
                "ok": True,
                "report": "扫描文件总数: 180\n总行数: 86979",
                "outputs_check": {"outputs_complete": True},
            },
        ],
    }

    items = _delegate_items_from_result(json.dumps(payload, ensure_ascii=False))
    usable = [
        item.get("report")
        for item in items
        if not _delegate_item_is_incomplete(item)
    ]

    assert usable == ["扫描文件总数: 180\n总行数: 86979"]


def test_retryable_delegate_facts_include_resource_required():
    from app.llm.client_tools_loop import _retryable_delegate_facts_from_result

    payload = {
        "ok": True,
        "task_ok": False,
        "results": [{
            "task_id": "paper_edit",
            "ok": False,
            "terminal_reason": "resource_required",
            "resource_required": {"suggested_helper_kind": "draw"},
            "next_action": {
                "type": "resume_upgraded",
                "params": {
                    "action": "spawn",
                    "task_id": "paper_edit",
                    "resume": True,
                    "kind": "edit",
                    "mode": "hard",
                },
            },
            "outputs_check": {"outputs_complete": False, "outputs_missing": ["chart.png"]},
        }],
    }

    facts = _retryable_delegate_facts_from_result(json.dumps(payload, ensure_ascii=False))

    assert len(facts) == 1
    assert facts[0]["task_id"] == "paper_edit"
    assert facts[0]["terminal_reason"] == "resource_required"


def test_retryable_delegate_facts_include_plain_incomplete_failure():
    from app.llm.client_tools_loop import _retryable_delegate_facts_from_result

    payload = {
        "ok": True,
        "task_ok": False,
        "results": [{
            "task_id": "scan_stats",
            "ok": False,
            "kind": "code",
            "terminal_reason": "failed",
            "error": "verification failed",
            "outputs_check": {
                "outputs_complete": False,
                "outputs_missing": ["report.md"],
                "delivered_count": 0,
            },
        }],
    }

    facts = _retryable_delegate_facts_from_result(json.dumps(payload, ensure_ascii=False))

    assert len(facts) == 1
    assert facts[0]["task_id"] == "scan_stats"
    assert facts[0]["terminal_reason"] == "failed"
    assert facts[0]["next_action_type"] == "resume_upgraded"
    assert facts[0]["params"]["task_id"] == "scan_stats"
    assert facts[0]["params"]["resume"] is True
    assert facts[0]["params"]["kind"] == "code"
    assert facts[0]["params"]["mode"] == "hard"


def test_retryable_delegate_facts_include_output_format_repair_action():
    from app.llm.client_tools_loop import _retryable_delegate_facts_from_result

    payload = {
        "ok": True,
        "task_ok": False,
        "results": [{
            "task_id": "paper_framework",
            "ok": False,
            "kind": "edit",
            "mode": "easy",
            "terminal_reason": "output_format_invalid",
            "retry_instruction": "Resume the same helper task_id and fix Output files.",
            "next_action": {
                "type": "resume_same_task_fix_output_format",
                "params": {
                    "action": "spawn",
                    "task_id": "paper_framework",
                    "resume": True,
                    "kind": "edit",
                    "mode": "easy",
                    "prompt": "Repair only the final report format.",
                },
            },
            "outputs_check": {
                "outputs_complete": False,
                "outputs_missing": [],
                "quality_warnings": [
                    "The helper report attempted to declare output files but did not use the required Output files JSON block."
                ],
            },
        }],
    }

    facts = _retryable_delegate_facts_from_result(json.dumps(payload, ensure_ascii=False))

    assert len(facts) == 1
    assert facts[0]["task_id"] == "paper_framework"
    assert facts[0]["terminal_reason"] == "output_format_invalid"
    assert facts[0]["next_action_type"] == "resume_same_task_fix_output_format"
    assert facts[0]["params"]["resume"] is True
    assert facts[0]["params"]["task_id"] == "paper_framework"


def test_retryable_delegate_facts_build_output_format_repair_params_when_missing():
    from app.llm.client_tools_loop import _retryable_delegate_facts_from_result

    payload = {
        "ok": True,
        "task_ok": False,
        "results": [{
            "task_id": "paper_framework",
            "ok": False,
            "kind": "edit",
            "mode": "easy",
            "terminal_reason": "output_format_invalid",
            "next_action": {"type": "resume_same_task_fix_output_format"},
            "outputs_check": {
                "outputs_complete": False,
                "outputs_missing": [],
            },
        }],
    }

    facts = _retryable_delegate_facts_from_result(json.dumps(payload, ensure_ascii=False))

    assert len(facts) == 1
    params = facts[0]["params"]
    assert facts[0]["next_action_type"] == "resume_same_task_fix_output_format"
    assert params["action"] == "spawn"
    assert params["task_id"] == "paper_framework"
    assert params["resume"] is True
    assert params["kind"] == "edit"
    assert params["mode"] == "easy"
    assert "Resume the same helper task from the preserved workspace" in params["prompt"]
    assert "instead of creating a v2 task" in params["prompt"]
    assert "Output files" in params["prompt"]


def test_helper_read_to_write_checkpoint_is_wired_into_tool_loop():
    from pathlib import Path

    src = Path("app/llm/client_tools_loop.py").read_text(encoding="utf-8")

    assert "helper_read_to_write_checkpoint" in src
    assert "_helper_read_only_streak >= 6" in src
    assert "llm.tools.helper_read_to_write_checkpoint" in src
    assert "task_id is not None" in src


def test_race_lost_delegate_item_is_not_retryable_or_incomplete():
    from app.llm.client_tools_loop import (
        _delegate_item_is_incomplete,
        _retryable_delegate_facts_from_result,
    )

    item = {
        "task_id": "dijkstra-impl_hard",
        "ok": False,
        "interrupted": True,
        "terminal_reason": "interrupted",
        "race_lost_to": "dijkstra-impl",
        "next_action": {
            "type": "resume_upgraded",
            "params": {
                "action": "spawn",
                "task_id": "dijkstra-impl_hard",
                "resume": True,
                "kind": "code",
                "mode": "hard",
            },
        },
        "outputs_check": {"outputs_complete": False},
    }
    payload = {"ok": True, "task_ok": True, "results": [item]}

    assert _delegate_item_is_incomplete(item) is False
    assert _retryable_delegate_facts_from_result(json.dumps(payload, ensure_ascii=False)) == []


def test_main_stuck_recovery_does_not_force_abort_when_strategy_can_change():
    from pathlib import Path

    src = Path("app/llm/client_tools_loop.py").read_text(encoding="utf-8")
    hints_src = Path("app/llm/tools/runtime_hints.py").read_text(encoding="utf-8")
    assert "[SYSTEM_HINT/strategy_recovery]" in hints_src
    assert "strategy_recovery(" in src
    assert "llm.tools.stuck.strategy_recovery" in src
    assert "main thread stuck x%d, forcing abort" not in src
    assert "main thread stuck for %d consecutive iters at trigger=%d, forcing abort" not in src


def test_tool_result_signal_marks_delegate_incomplete_as_failure():
    from app.llm.message_utils import _tool_result_signal

    payload = {
        "ok": True,
        "task_ok": False,
        "helpers_completed": 1,
        "helpers_still_running": 0,
        "incomplete_count": 1,
        "resource_required_count": 1,
        "results": [{
            "task_id": "paper_edit",
            "ok": False,
            "terminal_reason": "resource_required",
        }],
    }

    ok, summary = _tool_result_signal(json.dumps(payload, ensure_ascii=False))

    assert ok is False
    assert "delegate blocked" in summary
    assert "resource_required=1" in summary


def test_completed_todo_count_requires_all_items_complete():
    from app.llm.client_tools_loop import _completed_todo_count_from_result

    complete = json.dumps({
        "ok": True,
        "action": "todo_write",
        "counts": {"total": 6, "completed": 6, "in_progress": 0, "pending": 0},
    })
    partial = json.dumps({
        "ok": True,
        "action": "todo_write",
        "counts": {"total": 6, "completed": 5, "in_progress": 1, "pending": 0},
    })
    failed = json.dumps({
        "ok": False,
        "action": "todo_write",
        "counts": {"total": 6, "completed": 6, "in_progress": 0, "pending": 0},
    })

    assert _completed_todo_count_from_result("todo_write", complete) == 6
    assert _completed_todo_count_from_result("todo_write", partial) is None
    assert _completed_todo_count_from_result("todo_write", failed) is None
    assert _completed_todo_count_from_result("workspace", complete) is None


@pytest.mark.asyncio
async def test_retryable_delegate_next_action_blocks_premature_finalize(monkeypatch):
    from app.llm import client as llm_client
    from app.llm.client_tools_loop import chat_with_tools_loop

    fake_comp = _FakeCompletions()
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_comp))

    monkeypatch.setattr(llm_client, "_client_for_spec", lambda spec: fake_client)
    monkeypatch.setattr(
        llm_client,
        "_legacy_model_spec",
        lambda lite=False, reasoning="high": SimpleNamespace(
            model="fake", reasoning=reasoning, provider=None,
        ),
    )
    monkeypatch.setattr(llm_client, "_thinking_extra_body", lambda reasoning, provider=None: {})
    monkeypatch.setattr(llm_client, "_retry", lambda fn, label="", provider=None: fn())
    monkeypatch.setattr(llm_client, "_serialize_assistant_message", _serialize_assistant_message)
    monkeypatch.setattr(
        llm_client,
        "_normalize_tool_call_args_for_dispatch",
        lambda raw: (json.loads(raw or "{}"), None, False),
    )
    monkeypatch.setattr(llm_client, "_maybe_clear_stale_upgrade", lambda *a, **k: None)
    monkeypatch.setattr(llm_client, "_try_extract_json_locally", lambda content: None)
    monkeypatch.setattr(llm_client, "_tool_result_signal", lambda result: (True, None))
    monkeypatch.setattr(llm_client, "_is_thinking_enabled", lambda extra: False)

    async def fake_stream(**kwargs):
        resp = await fake_comp.create(**kwargs)
        return resp, _Collector(resp=resp, content="", reasoning_content="", tool_calls=[]), "ok"

    monkeypatch.setattr(llm_client, "_call_llm_streaming_with_idle", fake_stream)

    async def dispatcher(name, args):
        task = (args.get("tasks") or [{}])[0]
        if task.get("resume"):
            return json.dumps({
                "ok": True,
                "results": [{
                    "task_id": "fix_birth_death",
                    "ok": True,
                    "report": "完成修复",
                    "files": ["snake.html"],
                    "outputs_check": {"outputs_complete": True},
                }],
            }, ensure_ascii=False)
        return json.dumps({
            "ok": True,
            "results": [{
                "task_id": "fix_birth_death",
                "ok": False,
                "terminal_reason": "stuck",
                "stuck_reason": "连续 4 次工具调用失败,无任何进展",
                "next_action": {
                    "type": "resume_upgraded",
                    "rationale": "upgrade",
                    "params": {
                        "action": "spawn",
                        "task_id": "fix_birth_death",
                        "resume": True,
                        "kind": "code",
                        "mode": "hard",
                    },
                },
                "outputs_check": {
                    "outputs_complete": False,
                    "outputs_missing": ["_env/snake.html"],
                    "delivered_count": 0,
                },
            }],
        }, ensure_ascii=False)

    content, msgs = await chat_with_tools_loop(
        [{"role": "system", "content": "return json"}, {"role": "user", "content": "fix bug"}],
        [{"type": "function", "function": {"name": "delegate", "parameters": {"type": "object"}}}],
        dispatcher=dispatcher,
        require_first_tool_call=False,
    )

    assert fake_comp.calls >= 4
    assert "失败了，准备回复用户" not in content
    assert "已重试后完成" in content
    assert any(
        "retry_required_before_final" in (m.get("content") or "")
        for m in msgs
        if isinstance(m, dict)
    )


@pytest.mark.asyncio
async def test_retryable_delegate_next_action_gives_multiple_recovery_checkpoints(monkeypatch):
    from app.llm import client as llm_client
    from app.llm.client_tools_loop import chat_with_tools_loop

    fake_comp = _FakeRepeatedPrematureFinalizeCompletions()
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_comp))

    monkeypatch.setattr(llm_client, "_client_for_spec", lambda spec: fake_client)
    monkeypatch.setattr(
        llm_client,
        "_legacy_model_spec",
        lambda lite=False, reasoning="high": SimpleNamespace(
            model="fake", reasoning=reasoning, provider=None,
        ),
    )
    monkeypatch.setattr(llm_client, "_thinking_extra_body", lambda reasoning, provider=None: {})
    monkeypatch.setattr(llm_client, "_retry", lambda fn, label="", provider=None: fn())
    monkeypatch.setattr(llm_client, "_serialize_assistant_message", _serialize_assistant_message)
    monkeypatch.setattr(
        llm_client,
        "_normalize_tool_call_args_for_dispatch",
        lambda raw: (json.loads(raw or "{}"), None, False),
    )
    monkeypatch.setattr(llm_client, "_maybe_clear_stale_upgrade", lambda *a, **k: None)
    monkeypatch.setattr(llm_client, "_try_extract_json_locally", lambda content: None)
    monkeypatch.setattr(llm_client, "_tool_result_signal", lambda result: (True, None))
    monkeypatch.setattr(llm_client, "_is_thinking_enabled", lambda extra: False)

    async def fake_stream(**kwargs):
        resp = await fake_comp.create(**kwargs)
        return resp, _Collector(resp=resp, content="", reasoning_content="", tool_calls=[]), "ok"

    monkeypatch.setattr(llm_client, "_call_llm_streaming_with_idle", fake_stream)

    async def dispatcher(name, args):
        task = (args.get("tasks") or [{}])[0]
        if task.get("resume"):
            return json.dumps({
                "ok": True,
                "results": [{
                    "task_id": "fix_birth_death",
                    "ok": True,
                    "report": "完成修复",
                    "files": ["snake.html"],
                    "outputs_check": {"outputs_complete": True},
                }],
            }, ensure_ascii=False)
        return json.dumps({
            "ok": True,
            "results": [{
                "task_id": "fix_birth_death",
                "ok": False,
                "terminal_reason": "failed",
                "error": "verification failed",
                "outputs_check": {
                    "outputs_complete": False,
                    "outputs_missing": ["_env/snake.html"],
                    "delivered_count": 0,
                },
            }],
        }, ensure_ascii=False)

    content, msgs = await chat_with_tools_loop(
        [{"role": "system", "content": "return json"}, {"role": "user", "content": "fix bug"}],
        [{"type": "function", "function": {"name": "delegate", "parameters": {"type": "object"}}}],
        dispatcher=dispatcher,
        require_first_tool_call=False,
    )

    assert fake_comp.calls >= 5
    assert "失败了，准备回复用户" not in content
    assert "已重试后完成" in content
    retry_hints = [
        m.get("content") or ""
        for m in msgs
        if isinstance(m, dict) and "retry_required_before_final" in (m.get("content") or "")
    ]
    assert len(retry_hints) >= 2
    assert any("Recovery checkpoint 2/3" in hint for hint in retry_hints)


@pytest.mark.asyncio
async def test_retryable_delegate_stays_blocked_after_checkpoint_limit(monkeypatch):
    from app.llm import client as llm_client
    from app.llm.client_tools_loop import chat_with_tools_loop

    fake_comp = _FakeExhaustedRetryCheckpointCompletions()
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_comp))

    monkeypatch.setattr(llm_client, "_client_for_spec", lambda spec: fake_client)
    monkeypatch.setattr(
        llm_client,
        "_legacy_model_spec",
        lambda lite=False, reasoning="high": SimpleNamespace(
            model="fake", reasoning=reasoning, provider=None,
        ),
    )
    monkeypatch.setattr(llm_client, "_thinking_extra_body", lambda reasoning, provider=None: {})
    monkeypatch.setattr(llm_client, "_retry", lambda fn, label="", provider=None: fn())
    monkeypatch.setattr(llm_client, "_serialize_assistant_message", _serialize_assistant_message)
    monkeypatch.setattr(
        llm_client,
        "_normalize_tool_call_args_for_dispatch",
        lambda raw: (json.loads(raw or "{}"), None, False),
    )
    monkeypatch.setattr(llm_client, "_maybe_clear_stale_upgrade", lambda *a, **k: None)
    monkeypatch.setattr(llm_client, "_try_extract_json_locally", lambda content: None)
    monkeypatch.setattr(llm_client, "_tool_result_signal", lambda result: (True, None))
    monkeypatch.setattr(llm_client, "_is_thinking_enabled", lambda extra: False)

    async def fake_stream(**kwargs):
        resp = await fake_comp.create(**kwargs)
        return resp, _Collector(resp=resp, content="", reasoning_content="", tool_calls=[]), "ok"

    monkeypatch.setattr(llm_client, "_call_llm_streaming_with_idle", fake_stream)

    async def dispatcher(name, args):
        task = (args.get("tasks") or [{}])[0]
        if task.get("resume"):
            return json.dumps({
                "ok": True,
                "results": [{
                    "task_id": "read_chen",
                    "kind": "read",
                    "ok": True,
                    "terminal_reason": "completed",
                    "internal_evidence_files": ["read_chen_evidence.txt"],
                    "read_evidence_summary": {"has_complete_evidence": True},
                    "outputs_check": {"outputs_complete": True},
                }],
            }, ensure_ascii=False)
        return json.dumps({
            "ok": True,
            "task_ok": False,
            "results": [{
                "task_id": "read_chen",
                "kind": "read",
                "ok": False,
                "terminal_reason": "failed",
                "error": "source directory not located",
                "outputs_check": {
                    "outputs_complete": False,
                    "outputs_missing": ["read_chen_evidence.txt"],
                    "delivered_count": 0,
                },
            }],
        }, ensure_ascii=False)

    content, msgs = await chat_with_tools_loop(
        [{"role": "system", "content": "return json"}, {"role": "user", "content": "read all reports"}],
        [{"type": "function", "function": {"name": "delegate", "parameters": {"type": "object"}}}],
        dispatcher=dispatcher,
        require_first_tool_call=False,
    )

    assert fake_comp.calls >= 7
    assert "带缺口收尾" not in content
    assert "读取完成后收尾" in content
    retry_required = [
        m.get("content") or ""
        for m in msgs
        if isinstance(m, dict) and "retry_required_before_final" in (m.get("content") or "")
    ]
    retry_still_required = [
        m.get("content") or ""
        for m in msgs
        if isinstance(m, dict) and "retry_still_required_before_final" in (m.get("content") or "")
    ]
    assert len(retry_required) == 3
    assert retry_still_required
    forced_retry_calls = [
        kwargs.get("tool_choice")
        for kwargs in fake_comp.kwargs_by_call
        if kwargs.get("tool_choice")
    ]
    assert forced_retry_calls
    assert forced_retry_calls[-1] == {
        "type": "function",
        "function": {"name": "delegate"},
    }


@pytest.mark.asyncio
async def test_retryable_delegate_does_not_block_after_commit_to_main(monkeypatch):
    from app.llm import client as llm_client
    from app.llm.client_tools_loop import (
        _committed_files_from_recent_tools,
        chat_with_tools_loop,
    )

    fake_comp = _FakeCommitAfterDelegateFailureCompletions()
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_comp))

    monkeypatch.setattr(llm_client, "_client_for_spec", lambda spec: fake_client)
    monkeypatch.setattr(
        llm_client,
        "_legacy_model_spec",
        lambda lite=False, reasoning="high": SimpleNamespace(
            model="fake", reasoning=reasoning, provider=None,
        ),
    )
    monkeypatch.setattr(llm_client, "_thinking_extra_body", lambda reasoning, provider=None: {})
    monkeypatch.setattr(llm_client, "_retry", lambda fn, label="", provider=None: fn())
    monkeypatch.setattr(llm_client, "_serialize_assistant_message", _serialize_assistant_message)
    monkeypatch.setattr(
        llm_client,
        "_normalize_tool_call_args_for_dispatch",
        lambda raw: (json.loads(raw or "{}"), None, False),
    )
    monkeypatch.setattr(llm_client, "_maybe_clear_stale_upgrade", lambda *a, **k: None)
    monkeypatch.setattr(llm_client, "_try_extract_json_locally", lambda content: None)
    monkeypatch.setattr(llm_client, "_tool_result_signal", lambda result: (True, None))
    monkeypatch.setattr(llm_client, "_is_thinking_enabled", lambda extra: False)

    async def fake_stream(**kwargs):
        resp = await fake_comp.create(**kwargs)
        return resp, _Collector(resp=resp, content="", reasoning_content="", tool_calls=[]), "ok"

    monkeypatch.setattr(llm_client, "_call_llm_streaming_with_idle", fake_stream)

    async def dispatcher(name, args):
        if name == "commit_to_main":
            return json.dumps({
                "ok": True,
                "action": "commit_to_main",
                "promoted": ["shorttest_summary.md"],
                "skipped": [],
            }, ensure_ascii=False)
        return json.dumps({
            "ok": True,
            "task_ok": False,
            "results": [{
                "task_id": "gen_summary",
                "ok": False,
                "terminal_reason": "resource_required",
                "resource_required": {"suggested_helper_kind": "edit"},
                "outputs_check": {
                    "outputs_complete": False,
                    "outputs_missing": ["shorttest_summary.md"],
                },
            }],
        }, ensure_ascii=False)

    content, msgs = await chat_with_tools_loop(
        [{"role": "system", "content": "return json"}, {"role": "user", "content": "write report"}],
        [
            {"type": "function", "function": {"name": "delegate", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "commit_to_main", "parameters": {"type": "object"}}},
        ],
        dispatcher=dispatcher,
        require_first_tool_call=False,
    )

    assert fake_comp.calls == 3
    assert "已生成并验证" in content
    assert _committed_files_from_recent_tools(msgs) == ["shorttest_summary.md"]
    assert not any(
        "retry_required_before_final" in (m.get("content") or "")
        for m in msgs
        if isinstance(m, dict)
    )


def test_pending_retry_commit_only_clears_matching_missing_outputs():
    from app.llm.client_tools_loop import _pending_retry_tasks_blocking_finalize

    facts = [{
        "task_id": "impl-skiplist",
        "terminal_reason": "interrupted",
        "outputs_missing": ["benchmark_skiplist.csv", "description_skiplist.txt"],
        "outputs_complete": False,
    }]

    assert _pending_retry_tasks_blocking_finalize(
        ["impl-skiplist"],
        facts,
        ["数据库索引算法研究论文.docx"],
    ) == ["impl-skiplist"]
    assert _pending_retry_tasks_blocking_finalize(
        ["impl-skiplist"],
        facts,
        ["benchmark_skiplist.csv", "description_skiplist.txt"],
    ) == []


def test_delegate_workflow_result_summary_separates_returned_from_success():
    from app.llm.client_tools_loop import _delegate_workflow_result_summary

    summary = _delegate_workflow_result_summary({
        "task_ok": False,
        "helpers_completed": 1,
        "helpers_still_running": 0,
        "success_count": 0,
        "incomplete_count": 1,
        "results": [{
            "task_id": "impl-skiplist",
            "ok": False,
            "terminal_reason": "interrupted",
            "outputs_check": {
                "outputs_complete": False,
                "outputs_missing": ["benchmark_skiplist.csv"],
            },
        }],
    })

    assert summary["helpers_returned"] == 1
    assert summary["success_count"] == 0
    assert summary["completed_task_ids"] == []
    assert summary["failed_task_ids"] == ["impl-skiplist"]
    assert summary["missing_outputs_by_task"] == {
        "impl-skiplist": ["benchmark_skiplist.csv"]
    }


@pytest.mark.asyncio
async def test_timeout_partial_tool_call_does_not_finalize_without_evidence(monkeypatch):
    from app.llm import client as llm_client
    from app.llm import client_tools_loop
    from app.llm.client_tools_loop import chat_with_tools_loop

    calls = {"stream": 0, "dispatch": 0}

    first_tool = _delegate_call("call_env_run_1", {
        "action": "spawn",
        "tasks": [{"task_id": "scan", "kind": "general", "mode": "easy"}],
    })
    final_resp = _Resp(choices=[_Choice(message=_Msg(
        content=json.dumps({"intent": "完成", "key_points": ["已用工具核验"]}, ensure_ascii=False),
        tool_calls=[],
    ))])

    async def fake_stream(**kwargs):
        calls["stream"] += 1
        if calls["stream"] == 1:
            coll = _PartialToolCollector(
                content="",
                reasoning_content="",
                tool_calls={
                    0: {
                        "id": "call_partial",
                        "type": "function",
                        "function": {"name": "delegate", "arguments": '{"action":"spawn"'},
                    }
                },
                resp=_Resp(choices=[_Choice(message=_Msg(
                    content="",
                    tool_calls=[_delegate_call("call_partial", {"action": "spawn"})],
                ))]),
            )
            coll.resp.choices[0].message.tool_calls[0].function.arguments = '{"action":"spawn"'
            exc = TimeoutError("stream exit=idle_timeout (partial: content=0 tool_calls=1)")
            exc._stream_collector = coll
            exc._stream_exit_reason = "idle_timeout"
            raise exc
        if calls["stream"] == 2:
            return _Resp(choices=[_Choice(message=_Msg(
                content="",
                tool_calls=[first_tool],
            ))]), _Collector(resp=None, content="", reasoning_content="", tool_calls=[]), "ok"
        return final_resp, _Collector(resp=final_resp, content="", reasoning_content="", tool_calls=[]), "ok"

    monkeypatch.setattr(llm_client, "_call_llm_streaming_with_idle", fake_stream)
    monkeypatch.setattr(client_tools_loop, "_call_llm_streaming_with_idle", fake_stream)
    monkeypatch.setattr(
        llm_client,
        "_legacy_model_spec",
        lambda lite=False, reasoning="high": SimpleNamespace(
            model="fake", reasoning=reasoning, provider=None,
        ),
    )
    monkeypatch.setattr(llm_client, "_client_for_spec", lambda spec: SimpleNamespace())
    monkeypatch.setattr(llm_client, "_thinking_extra_body", lambda reasoning, provider=None: {})
    monkeypatch.setattr(llm_client, "_is_thinking_enabled", lambda extra: False)
    monkeypatch.setattr(client_tools_loop, "_is_thinking_enabled", lambda extra: False)
    monkeypatch.setattr(llm_client, "_configured_stream_first_chunk_timeout", lambda default: default)
    monkeypatch.setattr(client_tools_loop, "_configured_stream_first_chunk_timeout", lambda default: default)
    monkeypatch.setattr(llm_client, "_serialize_assistant_message", _serialize_assistant_message)
    monkeypatch.setattr(client_tools_loop, "_serialize_assistant_message", _serialize_assistant_message)
    def normalize_args(raw):
        try:
            return json.loads(raw or "{}"), None, False
        except json.JSONDecodeError as exc:
            return {}, exc, False

    monkeypatch.setattr(llm_client, "_normalize_tool_call_args_for_dispatch", normalize_args)
    monkeypatch.setattr(client_tools_loop, "_normalize_tool_call_args_for_dispatch", normalize_args)
    monkeypatch.setattr(llm_client, "_maybe_clear_stale_upgrade", lambda *a, **k: None)
    monkeypatch.setattr(client_tools_loop, "_maybe_clear_stale_upgrade", lambda *a, **k: None)
    monkeypatch.setattr(llm_client, "_try_extract_json_locally", lambda content: None)
    monkeypatch.setattr(client_tools_loop, "_try_extract_json_locally", lambda content: None)
    monkeypatch.setattr(llm_client, "_tool_result_signal", lambda result: (True, None))
    monkeypatch.setattr(client_tools_loop, "_tool_result_signal", lambda result: (True, None))
    monkeypatch.setattr(llm_client, "_build_continuation_prefix_message", lambda collector: None)
    monkeypatch.setattr(client_tools_loop, "_build_continuation_prefix_message", lambda collector: None)

    async def dispatcher(name, args):
        calls["dispatch"] += 1
        return json.dumps({"ok": True, "tool": name, "verified": True}, ensure_ascii=False)

    content, msgs = await chat_with_tools_loop(
        [{"role": "system", "content": "return json"}, {"role": "user", "content": "统计目录"}],
        [{"type": "function", "function": {"name": "delegate", "parameters": {"type": "object"}}}],
        dispatcher=dispatcher,
        require_first_tool_call=True,
    )

    assert calls["dispatch"] == 1
    assert "我猜测已经统计完成" not in content
    assert "已用工具核验" in content
    assert calls["stream"] == 3
    assert not any(
        m.get("role") == "tool" and "tool_call_args_json_broken" in (m.get("content") or "")
        for m in msgs
        if isinstance(m, dict)
    )


@pytest.mark.asyncio
async def test_repeated_llm_timeout_raises_instead_of_forced_finalize(monkeypatch):
    from app.llm import client as llm_client
    from app.llm import client_tools_loop
    from app.llm.client_tools_loop import chat_with_tools_loop

    calls = {"stream": 0}

    async def always_timeout(**kwargs):
        calls["stream"] += 1
        exc = TimeoutError("upstream timed out")
        exc._stream_exit_reason = "first_chunk_timeout"
        raise exc

    monkeypatch.setattr(llm_client, "_call_llm_streaming_with_idle", always_timeout)
    monkeypatch.setattr(client_tools_loop, "_call_llm_streaming_with_idle", always_timeout, raising=False)
    monkeypatch.setattr(
        llm_client,
        "_legacy_model_spec",
        lambda lite=False, reasoning="high": SimpleNamespace(
            model="fake", reasoning=reasoning, provider=None,
        ),
    )
    monkeypatch.setattr(llm_client, "_client_for_spec", lambda spec: SimpleNamespace())
    monkeypatch.setattr(llm_client, "_thinking_extra_body", lambda reasoning, provider=None: {})
    monkeypatch.setattr(client_tools_loop, "_thinking_extra_body", lambda reasoning, provider=None: {}, raising=False)
    monkeypatch.setattr(llm_client, "_is_thinking_enabled", lambda extra: False)
    monkeypatch.setattr(client_tools_loop, "_is_thinking_enabled", lambda extra: False, raising=False)
    monkeypatch.setattr(llm_client, "_configured_stream_first_chunk_timeout", lambda default: default)
    monkeypatch.setattr(client_tools_loop, "_configured_stream_first_chunk_timeout", lambda default: default, raising=False)
    monkeypatch.setattr(llm_client, "_serialize_assistant_message", _serialize_assistant_message)
    monkeypatch.setattr(client_tools_loop, "_serialize_assistant_message", _serialize_assistant_message, raising=False)
    monkeypatch.setattr(llm_client, "_maybe_clear_stale_upgrade", lambda *a, **k: None)
    monkeypatch.setattr(client_tools_loop, "_maybe_clear_stale_upgrade", lambda *a, **k: None, raising=False)

    with pytest.raises(asyncio.TimeoutError):
        await chat_with_tools_loop(
            [{"role": "system", "content": "return json"}, {"role": "user", "content": "do work"}],
            [{"type": "function", "function": {"name": "delegate", "parameters": {"type": "object"}}}],
            dispatcher=lambda name, args: "{}",
            require_first_tool_call=False,
        )

    assert calls["stream"] >= 4


@pytest.mark.asyncio
async def test_streaming_omits_tool_choice_when_thinking_enabled(monkeypatch):
    from app.llm import client as llm_client

    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _SingleChunkStream()

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    provider = SimpleNamespace(name="deepseek")

    monkeypatch.setattr(llm_client, "_retry", lambda fn, **kwargs: fn())

    resp, collector, exit_reason = await llm_client._call_llm_streaming_with_idle(
        the_client=fake_client,
        model="fake",
        provider=provider,
        msgs=[{"role": "user", "content": "work"}],
        tools=[{"type": "function", "function": {"name": "delegate", "parameters": {"type": "object"}}}],
        extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
        abort_event=None,
        iter_no=1,
        task_id="unit",
        idle_timeout=1,
        first_chunk_timeout=1,
        tool_choice={"type": "function", "function": {"name": "delegate"}},
    )

    assert exit_reason == "ok"
    assert resp.choices[0].message.tool_calls is None
    assert collector.chunk_count == 0
    assert "tool_choice" in captured
    assert captured["tool_choice"] is None


@pytest.mark.asyncio
async def test_streaming_logs_prompt_cache_shape_for_tools_loop(monkeypatch):
    from app.llm import client as llm_client

    shape_calls: list[dict] = []

    class FakeCompletions:
        async def create(self, **kwargs):
            return _SingleChunkStream()

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    provider = SimpleNamespace(name="deepseek")

    async def fake_retry(fn, **kwargs):
        return await fn()

    monkeypatch.setattr(llm_client, "_retry", fake_retry)
    monkeypatch.setattr(
        llm_client,
        "_log_prompt_cache_shape",
        lambda **kwargs: shape_calls.append(kwargs),
    )

    messages = [{"role": "user", "content": "work"}]
    tools = [{"type": "function", "function": {"name": "delegate", "parameters": {"type": "object"}}}]

    resp, collector, exit_reason = await llm_client._call_llm_streaming_with_idle(
        the_client=fake_client,
        model="fake-model",
        provider=provider,
        msgs=messages,
        tools=tools,
        extra_body={"thinking": {"type": "disabled"}},
        abort_event=None,
        iter_no=3,
        task_id=None,
        idle_timeout=1,
        first_chunk_timeout=1,
        tool_choice=None,
    )

    assert exit_reason == "ok"
    assert resp.choices[0].message.tool_calls is None
    assert collector.chunk_count == 0
    assert shape_calls == [
        {
            "label": "tools_loop.iter3.main",
            "model": "fake-model",
            "messages": messages,
            "tools": tools,
        }
    ]

    shape_calls.clear()
    await llm_client._call_llm_streaming_with_idle(
        the_client=fake_client,
        model="fake-model",
        provider=provider,
        msgs=messages,
        tools=tools,
        extra_body={"thinking": {"type": "disabled"}},
        abort_event=None,
        iter_no=4,
        task_id="read_docs",
        idle_timeout=1,
        first_chunk_timeout=1,
        tool_choice=None,
    )

    assert shape_calls[0]["label"] == "tools_loop.iter4.helper.read_docs"


@pytest.mark.asyncio
async def test_streaming_debug_log_has_shape_and_cache_stats_for_report(tmp_path, monkeypatch):
    from app.core import debug, metrics
    from app.core.cache_report import (
        evaluate_shape_coverage_gate,
        parse_debug_logs,
    )
    from app.llm import client as llm_client

    metrics.reset()
    debug._close_log_file_on_exit()
    monkeypatch.setattr(debug.settings, "debug_mode", True)
    monkeypatch.setattr(debug.settings, "debug_log_dir", str(tmp_path))
    monkeypatch.setattr(debug.settings, "debug_verbose", False)
    monkeypatch.setattr(debug, "_log_file", None)
    monkeypatch.setattr(debug, "_log_file_path", "")
    debug.set_trace_id("trace-cache-shape")

    class Usage:
        def __init__(self):
            self.prompt_tokens = 1000
            self.completion_tokens = 20
            self.prompt_cache_hit_tokens = 900
            self.prompt_cache_miss_tokens = 100

    class UsageChunk:
        usage = Usage()
        choices = []

    class UsageOnlyStream:
        def __init__(self):
            self._done = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._done:
                raise StopAsyncIteration
            self._done = True
            return UsageChunk()

        async def close(self):
            return None

    class FakeCompletions:
        async def create(self, **kwargs):
            return UsageOnlyStream()

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    provider = SimpleNamespace(name="deepseek")

    async def fake_retry(fn, **kwargs):
        return await fn()

    monkeypatch.setattr(llm_client, "_retry", fake_retry)

    await llm_client._call_llm_streaming_with_idle(
        the_client=fake_client,
        model="deepseek-unit",
        provider=provider,
        msgs=[
            {"role": "system", "content": "## Context And Safety Contract\nstable"},
            {"role": "user", "content": "## Current Time\n10:00\n\n## Current Message To Answer\nwork"},
        ],
        tools=[{"type": "function", "function": {"name": "delegate", "parameters": {"type": "object"}}}],
        extra_body={"thinking": {"type": "disabled"}},
        abort_event=None,
        iter_no=1,
        task_id=None,
        idle_timeout=1,
        first_chunk_timeout=1,
        tool_choice=None,
    )
    debug._close_log_file_on_exit()

    logs = list(tmp_path.glob("debug_*.log"))
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8")
    assert "[llm.prompt_cache_shape]" in text
    assert "[llm.cache_stats]" in text

    report = parse_debug_logs(logs)
    assert len(report.shapes) == 1
    assert len(report.stats) == 1
    assert report.shapes[0].tag_hint == "main"
    assert report.stats[0].tag == "main"
    assert evaluate_shape_coverage_gate(report, minimum_by_tag={"main": 100}) == []


@pytest.mark.asyncio
async def test_streaming_debug_log_records_nested_cache_usage_for_report(tmp_path, monkeypatch):
    from app.core import debug, metrics
    from app.core.cache_report import parse_debug_logs
    from app.llm import client as llm_client

    metrics.reset()
    debug._close_log_file_on_exit()
    monkeypatch.setattr(debug.settings, "debug_mode", True)
    monkeypatch.setattr(debug.settings, "debug_log_dir", str(tmp_path))
    monkeypatch.setattr(debug.settings, "debug_verbose", False)
    monkeypatch.setattr(debug, "_log_file", None)
    monkeypatch.setattr(debug, "_log_file_path", "")
    debug.set_trace_id("trace-cache-nested")

    class Usage:
        def __init__(self):
            self.prompt_tokens = 1000
            self.completion_tokens = 20
            self.prompt_tokens_details = {"cached_tokens": 850}

    class UsageChunk:
        usage = Usage()
        choices = []

    class UsageOnlyStream:
        def __init__(self):
            self._done = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._done:
                raise StopAsyncIteration
            self._done = True
            return UsageChunk()

        async def close(self):
            return None

    class FakeCompletions:
        async def create(self, **kwargs):
            return UsageOnlyStream()

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    provider = SimpleNamespace(name="deepseek")

    async def fake_retry(fn, **kwargs):
        return await fn()

    monkeypatch.setattr(llm_client, "_retry", fake_retry)

    await llm_client._call_llm_streaming_with_idle(
        the_client=fake_client,
        model="deepseek-unit",
        provider=provider,
        msgs=[
            {"role": "system", "content": "## Context And Safety Contract\nstable"},
            {"role": "user", "content": "## Current Time\n10:00\n\n## Current Message To Answer\nwork"},
        ],
        tools=[{"type": "function", "function": {"name": "delegate", "parameters": {"type": "object"}}}],
        extra_body={"thinking": {"type": "disabled"}},
        abort_event=None,
        iter_no=1,
        task_id="read_docs",
        idle_timeout=1,
        first_chunk_timeout=1,
        tool_choice=None,
    )
    debug._close_log_file_on_exit()

    logs = list(tmp_path.glob("debug_*.log"))
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8")
    assert "cache_hit=850 cache_miss=150 hit_rate=85%" in text

    report = parse_debug_logs(logs)
    assert len(report.stats) == 1
    assert report.stats[0].tag == "helper.read_docs"
    assert report.stats[0].cache_hit_tokens == 850
    assert report.stats[0].cache_miss_tokens == 150


@pytest.mark.asyncio
async def test_streaming_debug_log_records_responses_style_cache_usage_for_report(tmp_path, monkeypatch):
    from app.core import debug, metrics
    from app.core.cache_report import parse_debug_logs
    from app.llm import client as llm_client

    metrics.reset()
    debug._close_log_file_on_exit()
    monkeypatch.setattr(debug.settings, "debug_mode", True)
    monkeypatch.setattr(debug.settings, "debug_log_dir", str(tmp_path))
    monkeypatch.setattr(debug.settings, "debug_verbose", False)
    monkeypatch.setattr(debug, "_log_file", None)
    monkeypatch.setattr(debug, "_log_file_path", "")
    debug.set_trace_id("trace-cache-responses")

    class Usage:
        def __init__(self):
            self.input_tokens = 1200
            self.output_tokens = 25
            self.input_tokens_details = {"cached_tokens": 960}

    class UsageChunk:
        usage = Usage()
        choices = []

    class UsageOnlyStream:
        def __init__(self):
            self._done = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._done:
                raise StopAsyncIteration
            self._done = True
            return UsageChunk()

        async def close(self):
            return None

    class FakeCompletions:
        async def create(self, **kwargs):
            return UsageOnlyStream()

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    provider = SimpleNamespace(name="deepseek")

    async def fake_retry(fn, **kwargs):
        return await fn()

    monkeypatch.setattr(llm_client, "_retry", fake_retry)

    await llm_client._call_llm_streaming_with_idle(
        the_client=fake_client,
        model="deepseek-unit",
        provider=provider,
        msgs=[
            {"role": "system", "content": "## Context And Safety Contract\nstable"},
            {"role": "user", "content": "## Current Time\n10:00\n\n## Current Message To Answer\nwork"},
        ],
        tools=[{"type": "function", "function": {"name": "delegate", "parameters": {"type": "object"}}}],
        extra_body={"thinking": {"type": "disabled"}},
        abort_event=None,
        iter_no=1,
        task_id="read_docs",
        idle_timeout=1,
        first_chunk_timeout=1,
        tool_choice=None,
    )
    debug._close_log_file_on_exit()

    logs = list(tmp_path.glob("debug_*.log"))
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8")
    assert "cache_hit=960 cache_miss=240 hit_rate=80%" in text

    report = parse_debug_logs(logs)
    assert len(report.stats) == 1
    assert report.stats[0].tag == "helper.read_docs"
    assert report.stats[0].prompt_tokens == 1200
    assert report.stats[0].completion_tokens == 25
    assert report.stats[0].cache_hit_tokens == 960
    assert report.stats[0].cache_miss_tokens == 240


def test_sanitize_tool_choice_for_thinking_removes_none_choice():
    from app.llm import client as llm_client

    kwargs = {
        "model": "fake",
        "messages": [],
        "stream": False,
        "tool_choice": "none",
        "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
    }
    cleaned = llm_client._sanitize_tool_choice_for_thinking(
        kwargs,
        provider=SimpleNamespace(name="deepseek"),
        label="unit",
    )

    assert "tool_choice" not in cleaned
    assert "tool_choice" in kwargs


def test_helper_tool_result_budget_is_tighter_than_main_budget():
    import inspect
    from app.llm import client_tools_loop

    src = inspect.getsource(client_tools_loop.chat_with_tools_loop)

    assert "_P44_HELPER_TOOL_RESULT_BUDGET" in src
    assert '"read_file":           8 * 1024' in src
    assert '"workspace":           8 * 1024' in src
    assert "if task_id is not None:" in src
    assert "_budget = _tool_result_budget_for_loop(tc_name)" in src
    assert "_should_structurally_summarize_tool_result" in src
    assert '"read_file": 1200' in src
    module_src = inspect.getsource(client_tools_loop)
    assert "excerpt_text_limit = max(360, min(1200, budget // 4))" in module_src
    assert "if task_id is None and it > 0 and it % 5 == 0:" in src


def test_helper_loop_keeps_routine_fold_main_only_for_cache_prefix():
    import inspect
    from app.llm import client_tools_loop

    src = inspect.getsource(client_tools_loop.chat_with_tools_loop)

    assert "elif task_id is None and it > 5:" in src
    assert "helper 只保留上面的预算压力折叠" in src
