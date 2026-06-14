import asyncio
import json
from types import SimpleNamespace

import pytest


def test_final_plan_self_assessment_is_detected():
    from app.llm.client_tools_loop import _looks_like_final_plan_self_assessment

    text = (
        "The final JSON already satisfies the active task contract completely:\n"
        "- M1-M4: all four mechanisms documented\n"
        "- O1-O3: three optimization points\n"
        "- R1-R3: three correctness risks\n"
        "The output is contract-complete; no adjustments needed."
    )

    assert _looks_like_final_plan_self_assessment(text) is True
    assert _looks_like_final_plan_self_assessment(
        "O1: verified finding with evidence path; R1: low-confidence hypothesis."
    ) is False
    assert _looks_like_final_plan_self_assessment(
        json.dumps(
            {
                "final_json_status": "complete",
                "contract_verification": "verified against active task contract",
                "acceptance_points": {"status": "satisfied"},
                "further_tools_needed": False,
            },
            ensure_ascii=False,
        )
    ) is True
    assert _looks_like_final_plan_self_assessment(
        json.dumps(
            {
                "intent": "缓存机制审计",
                "key_points": ["O1: verified finding with app/core/context.py evidence"],
            },
            ensure_ascii=False,
        )
    ) is False


def test_audit_response_plan_needs_evidence_review_detection():
    from app.llm.client_tools_loop import _response_plan_needs_audit_evidence_review

    audit_plan = {
        "intent": "缓存与上下文机制审计",
        "key_points": [
            "O1: app/core/context.py L1200 动态上下文可优化",
            "R1: cache prefix 变更存在风险",
        ],
        "internal_note": "analysis only",
    }
    ordinary_plan = {
        "intent": "完成文件修改",
        "key_points": ["测试通过", "已更新 README"],
    }

    assert _response_plan_needs_audit_evidence_review(
        json.dumps(audit_plan, ensure_ascii=False)
    ) is True
    assert _response_plan_needs_audit_evidence_review(
        json.dumps(ordinary_plan, ensure_ascii=False)
    ) is False
    assert _response_plan_needs_audit_evidence_review("not json") is False


def test_audit_evidence_review_regression_detection_rejects_checklist_collapse():
    from app.llm.client_tools_loop import _audit_review_content_regressed

    previous = {
        "intent": "缓存与上下文机制审计",
        "key_points": [
            "M1 Stable Prefix: app/core/context.py L1200 说明 system stable prefix 与 user dynamic tail 分离。",
            "M2 Dynamic Context: app/core/context.py L1280 注入 Round2 Dynamic Context。",
            "M3 Tool Schema Slimming: app/core/toolchain_cache.py L530 使用 filter_tools_for_trace。",
            "M4 Round3 Evidence Passing: app/core/context.py L1561 使用 round3_messages。",
            "O1: app/core/toolchain_cache.py L540 可复用单工具 slim cache。",
            "R1: app/llm/client.py L240 的 LCP 观测 key 需要 trace 维度。",
        ],
        "internal_note": "analysis only",
    }
    current = {
        "intent": "基于工具执行结果回应用户",
        "key_points": [
            "_strip_static_knowledge_sections 存在于 context.py L1335",
            "round3_messages 存在于 context.py L1561",
        ],
        "internal_note": "JSON中所有声明均以行级证据支撑，无需修正",
    }

    assert _audit_review_content_regressed(
        json.dumps(previous, ensure_ascii=False),
        json.dumps(current, ensure_ascii=False),
    ) is True


def test_audit_evidence_review_regression_detection_allows_evidence_downgrade():
    from app.llm.client_tools_loop import _audit_review_content_regressed

    previous = {
        "intent": "cache/context audit",
        "key_points": [
            "M1: supported finding",
            "M2: supported finding",
            "M3: supported finding",
            "O1: weak claim",
            "R1: weak claim",
        ],
    }
    current = {
        "intent": "cache/context audit",
        "key_points": [
            "Only M1 has direct evidence; O1/R1 are low-confidence hypotheses with missing direct evidence.",
        ],
    }

    assert _audit_review_content_regressed(
        json.dumps(previous, ensure_ascii=False),
        json.dumps(current, ensure_ascii=False),
    ) is False


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


def test_office_write_artifact_key_recognizes_successful_doc_build_write():
    from app.llm.client_tools_loop import _office_write_artifact_key

    result = json.dumps({"ok": True, "action": "append"}, ensure_ascii=False)

    key = _office_write_artifact_key(
        "office",
        {"action": "append", "path": "db_index_paper.docx"},
        result,
    )

    assert key == "db_index_paper.docx"
    assert _office_write_artifact_key(
        "office",
        {"action": "read", "path": "db_index_paper.docx"},
        result,
    ) is None
    assert _office_write_artifact_key(
        "office",
        {"action": "append", "path": "db_index_paper.docx"},
        json.dumps({"ok": False}, ensure_ascii=False),
    ) is None


def test_main_env_run_convergence_family_groups_db_and_verifier_commands():
    from app.llm.client_tools_loop import (
        _main_env_run_convergence_family,
        _main_verifier_command_text,
    )

    assert _main_env_run_convergence_family(
        "env_run",
        {"command": "python -c \"import sqlite3; sqlite3.connect('users.db')\""},
    ) == "db:users.db"
    assert _main_env_run_convergence_family(
        "env_run",
        {"command": "python verify_results.py"},
    ) == "verifier:verify_results.py"
    assert _main_env_run_convergence_family(
        "env_run",
        {"python_code": "import subprocess, sys\nsubprocess.run([sys.executable, 'verify_results.py'])"},
    ) == "verifier:verify_results.py"
    assert "verify_results.py" in _main_verifier_command_text(
        "env_run",
        {"python_code": "import subprocess, sys\nsubprocess.run([sys.executable, 'verify_results.py'])"},
    )
    assert _main_env_run_convergence_family(
        "workspace",
        {"action": "run", "command": "node check-output.js"},
    ) == "verifier:check-output.js"
    assert _main_env_run_convergence_family(
        "workspace",
        {"action": "locate", "pattern": "*.py"},
    ) is None


def test_verifier_visible_artifact_paths_from_listing_ignores_scripts():
    from app.llm.client_tools_loop import _verifier_visible_artifact_paths_from_listing

    paths = _verifier_visible_artifact_paths_from_listing(
        "env_list_tree",
        {
            "items": [
                {"type": "file", "path": "verify_results.py"},
                {"type": "file", "path": "active_users.csv"},
                {"type": "file", "path": "notes.md"},
                {"type": "dir", "path": "reports"},
            ],
        },
    )

    assert paths == ["active_users.csv", "notes.md"]
    assert _verifier_visible_artifact_paths_from_listing(
        "env_inventory",
        {"resources": [{"project_path": "summary.txt"}, {"project_path": "check_summary.py"}]},
    ) == ["summary.txt"]


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


def test_browser_repro_requirement_is_current_task_fact(monkeypatch):
    import app.llm.client_tools_loop as loop

    monkeypatch.setattr(
        loop,
        "_current_task_plan_focus_snapshot",
        lambda max_chars=2400: "Use the browser tool to reproduce the old bug.",
    )

    messages = [{
        "role": "user",
        "content": (
            "## Current Message To Answer\n"
            "There is a page running at http://127.0.0.1:5555/. Use the browser tool "
            "to reproduce the bug in the host browser, fix the frontend, and verify it."
        ),
    }]

    assert loop._active_task_explicitly_requires_browser_repro(messages) is True
    assert loop._active_task_explicitly_requires_browser_repro([
        {"role": "user", "content": "Fix the frontend form after reading the files."}
    ]) is True
    monkeypatch.setattr(loop, "_current_task_plan_focus_snapshot", lambda max_chars=2400: "")
    assert loop._active_task_explicitly_requires_browser_repro([
        {"role": "user", "content": "Fix the frontend form after reading the files."}
    ]) is False
    monkeypatch.setattr(
        loop,
        "_current_task_plan_focus_snapshot",
        lambda max_chars=2400: "Use the browser tool to reproduce the active bug.",
    )
    assert loop._active_task_explicitly_requires_browser_repro([
        {"role": "user", "content": "继续"}
    ]) is True


def test_browser_pre_edit_requirement_is_order_specific(monkeypatch):
    import app.llm.client_tools_loop as loop

    monkeypatch.setattr(loop, "_current_task_plan_focus_snapshot", lambda max_chars=2400: "")

    assert loop._active_task_requires_browser_pre_edit_evidence([
        {
            "role": "user",
            "content": (
                "Browse the docs in the host browser to confirm the contract, "
                "then patch report_client.py."
            ),
        }
    ]) is True
    assert loop._active_task_requires_browser_pre_edit_evidence([
        {
            "role": "user",
            "content": "Use the browser tool to reproduce the bug in the host browser, fix the frontend, and verify it.",
        }
    ]) is True
    assert loop._active_task_requires_browser_pre_edit_evidence([
        {
            "role": "user",
            "content": "Patch the frontend, then verify the fixed page in the host browser.",
        }
    ]) is False

    monkeypatch.setattr(
        loop,
        "_current_task_plan_focus_snapshot",
        lambda max_chars=2400: "Use host browser to confirm the docs, then update the client.",
    )
    assert loop._active_task_requires_browser_pre_edit_evidence([
        {"role": "user", "content": "继续"}
    ]) is True


def test_browser_evidence_fact_detection_uses_existing_current_task_facts(monkeypatch):
    import app.llm.client_tools_loop as loop

    monkeypatch.setattr(loop, "_current_task_plan_focus_snapshot", lambda max_chars=4000: "")

    request_only = [{
        "role": "user",
        "content": "Use the browser tool to reproduce the bug, then patch the client.",
    }]
    assert loop._active_task_has_browser_evidence_fact(request_only) is False

    supplied_fact = [{
        "role": "user",
        "content": (
            "Current task facts: Playwright chromium loaded http://127.0.0.1:5555/ "
            "and observed the docs page rendering the API table before edits."
        ),
    }]
    assert loop._active_task_has_browser_evidence_fact(supplied_fact) is True

    monkeypatch.setattr(
        loop,
        "_current_task_plan_focus_snapshot",
        lambda max_chars=4000: "Host browser evidence: screenshot showed the form error before any code change.",
    )
    assert loop._active_task_has_browser_evidence_fact([{"role": "user", "content": "继续"}]) is True

    monkeypatch.setattr(loop, "_current_task_plan_focus_snapshot", lambda max_chars=4000: "")
    docs_fact_without_browser_tool_evidence = [{
        "role": "user",
        "content": (
            "The docs have been confirmed from the live URL. Exact facts: "
            "endpoint /v2/reports, required headers X-Workspace-Id and Authorization, "
            "rate limit 120/min, max payload 10 MiB."
        ),
    }]
    assert loop._active_task_has_browser_evidence_fact(docs_fact_without_browser_tool_evidence) is False

    browser_requirement_without_evidence = [{
        "role": "user",
        "content": (
            "Current task plan: Browser evidence is required before edits. "
            "Use host browser to confirm the docs, then update the client."
        ),
    }]
    assert loop._active_task_has_browser_evidence_fact(browser_requirement_without_evidence) is False


def test_browser_pre_edit_mutation_detection_is_edit_focused():
    from app.llm.client_tools_loop import _tool_call_is_pre_edit_mutation

    assert _tool_call_is_pre_edit_mutation("edit_file", {"path": "_env/app.js"}) is True
    assert _tool_call_is_pre_edit_mutation("multi_edit", {"path": "_env/app.js"}) is True
    assert _tool_call_is_pre_edit_mutation("env_apply_replace", {"path": "app.js"}) is True
    assert _tool_call_is_pre_edit_mutation("env_apply_create", {"path": "app.js"}) is True
    assert _tool_call_is_pre_edit_mutation(
        "workspace", {"action": "write", "path": "_env/app.js", "content": "x"}
    ) is True
    assert _tool_call_is_pre_edit_mutation(
        "env_run",
        {"python_code": "with open('report_client.py', 'w', encoding='utf-8') as f:\n    f.write('x')"},
    ) is True
    assert _tool_call_is_pre_edit_mutation(
        "env_run",
        {"python_code": "from pathlib import Path\nPath('api_notes.md').write_text('x', encoding='utf-8')"},
    ) is True
    assert _tool_call_is_pre_edit_mutation("env_run", {"command": "node verify_form.cjs"}) is False
    assert _tool_call_is_pre_edit_mutation("workspace", {"action": "run", "command": "npm test"}) is False
    assert _tool_call_is_pre_edit_mutation("read_file", {"path": "_env/app.js"}) is False


def test_browser_pre_edit_delegate_boundary_detection():
    import app.llm.client_tools_loop as loop

    authoring_delegate = {
        "action": "spawn",
        "tasks": [{
            "task_id": "patch_client",
            "kind": "code",
            "prompt": "Read docs/index.html, patch report_client.py, and write api_notes.md.",
            "expected_outputs": ["_env/report_client.py", "api_notes.md"],
        }],
    }
    assert loop._delegate_call_is_pre_edit_mutation(authoring_delegate) is True
    assert loop._delegate_call_declares_browser_pre_edit_boundary(authoring_delegate) is False

    browser_aware_delegate = {
        "action": "spawn",
        "tasks": [{
            "task_id": "patch_client",
            "kind": "code",
            "prompt": (
                "First collect browser evidence with Playwright/Chromium by visiting the target URL "
                "and observing the page. Only then patch report_client.py and write api_notes.md."
            ),
            "expected_outputs": ["_env/report_client.py", "api_notes.md"],
            "acceptance_checks": [
                "Report the Playwright/Chromium browser evidence before edits.",
                "All tests pass after the patch.",
            ],
        }],
    }
    assert loop._delegate_call_is_pre_edit_mutation(browser_aware_delegate) is True
    assert loop._delegate_call_declares_browser_pre_edit_boundary(browser_aware_delegate) is True

    read_only_browser_delegate = {
        "action": "spawn",
        "tasks": [{
            "task_id": "browser_probe",
            "kind": "read",
            "prompt": "Use Playwright browser evidence to inspect the page before any implementation task.",
            "expected_outputs": ["browser_report.md"],
        }],
    }
    assert loop._delegate_call_is_pre_edit_mutation(read_only_browser_delegate) is True
    assert loop._delegate_call_declares_browser_pre_edit_boundary(read_only_browser_delegate) is True

    top_level_browser_delegate = {
        "action": "spawn",
        "task_id": "browser_probe",
        "kind": "code",
        "prompt": "First use Playwright to visit the URL and observe browser evidence, then write the report.",
        "expected_outputs": ["browser_report.md"],
    }
    assert loop._delegate_call_is_pre_edit_mutation(top_level_browser_delegate) is True
    assert loop._delegate_call_declares_browser_pre_edit_boundary(top_level_browser_delegate) is True


def test_browser_pre_edit_fact_attaches_to_delegate_without_blocking():
    import app.llm.client_tools_loop as loop

    args = {
        "action": "spawn",
        "tasks": [{
            "task_id": "patch_client",
            "kind": "code",
            "prompt": "Patch report_client.py and write api_notes.md.",
            "expected_outputs": ["_env/report_client.py", "_env/api_notes.md"],
        }],
    }

    changed = loop._attach_browser_pre_edit_fact_to_delegate(args, warning_count=2)

    assert changed is True
    assert args["_browser_pre_edit_fact_attached"] is True
    task = args["tasks"][0]
    assert "Runtime fact: the active task asks for browser/host-browser evidence before edits" in task["dispatch_reason"]
    assert any("browser/host-browser evidence requirement" in item for item in task["acceptance_checks"])


def test_runtime_fact_merge_preserves_real_tool_success():
    import app.llm.client_tools_loop as loop

    result = loop._merge_tool_result_facts(
        json.dumps({"ok": True, "path": "_env/report_client.py"}, ensure_ascii=False),
        [
            loop._browser_pre_edit_missing_fact_payload(
                tool_name="workspace",
                path="_env/report_client.py",
                warning_count=1,
                pre_edit_required=True,
            )
        ],
    )

    parsed = json.loads(result)
    assert parsed["ok"] is True
    assert parsed["path"] == "_env/report_client.py"
    assert parsed["warnings"] == ["browser_reproduction_evidence_missing_before_edit"]
    assert parsed["runtime_facts"][0]["blocked_until_browser_evidence"] is False
    assert "allowed to execute" in parsed["runtime_facts"][0]["fact"]
    serialized = json.dumps(parsed["runtime_facts"][0], ensure_ascii=False).lower()
    for forbidden in ("helper", "delegate", "委派", "background work delegation"):
        assert forbidden not in serialized


def test_browser_pre_edit_predecision_guidance_is_factual():
    import app.llm.client_tools_loop as loop

    hint = loop._browser_pre_edit_predecision_guidance(iteration=3)

    assert "[SYSTEM_HINT/browser_pre_edit_evidence_boundary]" in hint
    assert "current tool loop has no browser-family evidence yet" in hint
    assert "not a forced decision" in hint
    assert "Source reads" in hint
    assert "当前任务要求浏览器" in hint
    lowered = hint.lower()
    for forbidden in ("helper", "delegate", "委派", "background work delegation"):
        assert forbidden not in lowered


def test_browser_evidence_detection_recognizes_browser_automation_not_plain_http():
    from app.llm.client_tools_loop import _delegate_workflow_result_summary, _tool_result_is_browser_repro_evidence

    assert _tool_result_is_browser_repro_evidence(
        "env_run",
        {"command": "node verify_form.cjs http://127.0.0.1:5555/"},
        '{"ok": false, "stderr": "Timeout while waiting in Playwright chromium"}',
    ) is True
    assert _tool_result_is_browser_repro_evidence(
        "bash",
        {"command": "python -m selenium http://localhost:3000"},
        '{"ok": true}',
    ) is True
    assert _tool_result_is_browser_repro_evidence(
        "env_run",
        {"command": "curl -i http://127.0.0.1:5555/"},
        '{"ok": true, "stdout": "HTTP/1.0 200 OK"}',
    ) is False
    assert _tool_result_is_browser_repro_evidence(
        "task_plan",
        {"action": "update"},
        '{"ok": true, "goal": "Use the browser tool at http://127.0.0.1:5555/ to reproduce"}',
    ) is False
    assert _tool_result_is_browser_repro_evidence(
        "env_list_tree",
        {"path": "."},
        '{"ok": true, "files": [".clawbench_playwright_chromium_patch.cjs"], "next_action_instruction": "If the user requested browser reproduction, source reading is not the same evidence."}',
    ) is False
    assert _tool_result_is_browser_repro_evidence(
        "env_run",
        {"command": "curl -s http://127.0.0.1:5555/"},
        '{"ok": true, "stdout": "Use the host browser at http://127.0.0.1:5555/ to read these docs."}',
    ) is False
    assert _tool_result_is_browser_repro_evidence(
        "bash",
        {"command": "node verify_form.cjs http://127.0.0.1:5555/"},
        '{"ok": true, "_family_evidence": "command execution for browser automation"}',
    ) is True
    assert _tool_result_is_browser_repro_evidence(
        "env_run",
        {
            "python_code": (
                "from playwright.sync_api import sync_playwright\n"
                "with sync_playwright() as p:\n"
                "    browser = p.chromium.launch()\n"
                "    page = browser.new_page()\n"
                "    page.goto('http://127.0.0.1:5555/')\n"
                "    browser.close()\n"
            )
        },
        '{"ok": true, "stdout": "loaded"}',
    ) is True
    delegate_result = {
        "ok": True,
        "task_ok": True,
        "success_count": 1,
        "results": [
            {
                "task_id": "browse_docs_and_patch",
                "ok": True,
                "terminal_reason": "completed",
                "report": (
                    "Playwright Chromium visited http://127.0.0.1:5555/ and observed "
                    "the docs page rendering the API table before editing."
                ),
                "outputs_check": {
                    "outputs_complete": True,
                    "producer_self_verified": True,
                },
            }
        ],
    }
    summary = _delegate_workflow_result_summary(delegate_result)
    assert summary["browser_evidence_facts"][0]["task_id"] == "browse_docs_and_patch"
    assert summary["browser_evidence_facts"][0]["urls"] == ["http://127.0.0.1:5555/"]
    assert _tool_result_is_browser_repro_evidence(
        "delegate",
        {"action": "wait"},
        json.dumps(delegate_result, ensure_ascii=False),
    ) is True
    plain_http_delegate = {
        "ok": True,
        "task_ok": True,
        "results": [
            {
                "task_id": "fetch_docs",
                "ok": True,
                "report": "Fetched http://127.0.0.1:5555/ with urllib and parsed endpoint docs.",
                "outputs_check": {"outputs_complete": True, "producer_self_verified": True},
            }
        ],
    }
    assert _tool_result_is_browser_repro_evidence(
        "delegate",
        {"action": "wait"},
        json.dumps(plain_http_delegate, ensure_ascii=False),
    ) is False
    blocked_browser_delegate = {
        "ok": True,
        "task_ok": True,
        "results": [
            {
                "task_id": "patch_with_http_only",
                "ok": True,
                "report": (
                    "Browser/host-browser evidence not satisfied: no Playwright, Selenium, "
                    "Chromium, Chrome, Firefox, or WebKit available. Curl/plain HTTP evidence "
                    "confirmed http://127.0.0.1:5555/, but this is HTTP evidence, not browser evidence."
                ),
                "outputs_check": {"outputs_complete": True, "producer_self_verified": True},
            }
        ],
    }
    blocked_summary = _delegate_workflow_result_summary(blocked_browser_delegate)
    assert blocked_summary["browser_evidence_facts"] == []
    assert blocked_summary["browser_evidence_gap_facts"][0]["task_id"] == "patch_with_http_only"
    assert blocked_summary["browser_evidence_gap_facts"][0]["urls"] == ["http://127.0.0.1:5555/"]
    assert _tool_result_is_browser_repro_evidence(
        "delegate",
        {"action": "wait"},
        json.dumps(blocked_browser_delegate, ensure_ascii=False),
    ) is False


def test_main_source_edit_path_detects_source_writes_only():
    from app.llm.client_tools_loop import _main_source_edit_path

    assert _main_source_edit_path("edit_file", {"path": "_env/app.js"}) == "_env/app.js"
    assert _main_source_edit_path(
        "workspace", {"action": "write", "path": "_env/app.js", "content": "x"}
    ) == "_env/app.js"
    assert _main_source_edit_path(
        "workspace", {"action": "write", "path": "notes.md", "content": "x"}
    ) is None
    assert _main_source_edit_path(
        "workspace", {"action": "run", "command": "node verify_form.cjs"}
    ) is None
    assert _main_source_edit_path(
        "env_run",
        {"python_code": "open('report_client.py', 'w', encoding='utf-8').write('x')"},
    ) == "report_client.py"
    assert _main_source_edit_path(
        "env_run",
        {"python_code": "from pathlib import Path\nPath('api_notes.md').write_text('x')"},
    ) == "api_notes.md"
    assert _main_source_edit_path("env_apply_replace", {"path": "app.js"}) is None


def test_delegate_result_staged_paths_extracts_helper_outputs():
    from app.llm.client_tools_loop import _delegate_result_staged_paths

    result = json.dumps({
        "ok": True,
        "main_available_files": ["_env/app.js"],
        "result_items": [
            {
                "staged_project_files": ["_env/src/index.ts"],
                "copied_project_files": [],
                "copy_stats": {"env_copied_files": ["_env/service/render.py"]},
            }
        ],
    }, ensure_ascii=False)

    assert _delegate_result_staged_paths(result) == {
        "_env/app.js",
        "app.js",
        "_env/src/index.ts",
        "src/index.ts",
        "_env/service/render.py",
        "service/render.py",
    }
    assert _delegate_result_staged_paths(json.dumps({"content": result}, ensure_ascii=False)) == {
        "_env/app.js",
        "app.js",
        "_env/src/index.ts",
        "src/index.ts",
        "_env/service/render.py",
        "service/render.py",
    }


def test_delegate_result_helper_owned_paths_require_clean_producer_boundary():
    from app.llm.client_tools_loop import (
        _delegate_result_helper_owned_paths,
        _env_apply_uses_helper_staged_source,
    )

    result = json.dumps({
        "results": [
            {
                "task_id": "write-notes",
                "ok": True,
                "terminal_reason": "completed",
                "main_available_files": ["api_notes.md"],
                "staged_project_files": ["_env/report_client.py"],
                "outputs_check": {
                    "outputs_complete": True,
                    "producer_self_verified": True,
                    "quality_warnings": [],
                },
            },
            {
                "task_id": "blocked-doc",
                "ok": False,
                "terminal_reason": "quality_blocked",
                "main_available_files": ["bad.docx"],
                "outputs_check": {
                    "outputs_complete": True,
                    "producer_self_verified": False,
                    "quality_blocked": True,
                    "blocking_quality_warnings": [{"issue": "invalid"}],
                },
            },
        ],
    }, ensure_ascii=False)

    helper_owned = _delegate_result_helper_owned_paths(result)

    assert "api_notes.md" in helper_owned
    assert "_env/report_client.py" in helper_owned
    assert "report_client.py" in helper_owned
    assert "bad.docx" not in helper_owned
    assert _env_apply_uses_helper_staged_source(
        {"ok": True, "path": "api_notes.md"},
        {"path": "api_notes.md", "workspace_path": "api_notes.md"},
        helper_owned,
    ) is True


@pytest.mark.asyncio
async def test_schema_retry_expansion_clears_after_non_schema_retry_result(monkeypatch):
    from app.core import debug, toolchain_cache
    from app.llm import client as llm_client
    from app.llm.client_tools_loop import chat_with_tools_loop

    trace_id = "trace_schema_retry_attempt_clears"
    debug.set_trace_id(trace_id)
    toolchain_cache.reset_trace(trace_id)

    monkeypatch.setattr(
        llm_client,
        "_legacy_model_spec",
        lambda lite=False, reasoning="high": SimpleNamespace(
            model="fake", reasoning=reasoning, provider=None,
        ),
    )
    monkeypatch.setattr(llm_client, "_client_for_spec", lambda spec: SimpleNamespace())
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

    calls = {"n": 0, "saw_hint": False}

    async def fake_stream(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                _Resp(choices=[_Choice(message=_Msg(
                    content="",
                    tool_calls=[_tool_call("call_workspace_1", "workspace", {"bad": True})],
                ))]),
                _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
                "ok",
            )
        if calls["n"] == 2:
            assert "Tool Schema Retry Facts" in "\n".join(
                str(m.get("content") or "") for m in kwargs["msgs"]
            )
            return (
                _Resp(choices=[_Choice(message=_Msg(
                    content="",
                    tool_calls=[_tool_call("call_workspace_2", "workspace", {"action": "run"})],
                ))]),
                _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
                "ok",
            )
        return (
            _Resp(choices=[_Choice(message=_Msg(
                content=json.dumps({"intent": "done", "key_points": ["checked"]}),
                tool_calls=[],
            ))]),
            _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
            "ok",
        )

    monkeypatch.setattr(llm_client, "_call_llm_streaming_with_idle", fake_stream)

    async def dispatcher(name, args):
        if calls["n"] == 1:
            return json.dumps({"ok": False, "error": "missing required action"}, ensure_ascii=False)
        return json.dumps({"ok": False, "error": "command timed out while running tests"}, ensure_ascii=False)

    content, msgs = await chat_with_tools_loop(
        [{"role": "system", "content": "return json"}, {"role": "user", "content": "work"}],
        [{"type": "function", "function": {
            "name": "workspace",
            "description": "workspace full schema description",
            "parameters": {"type": "object", "properties": {"action": {"type": "string"}}},
        }}],
        dispatcher=dispatcher,
        require_first_tool_call=False,
    )

    assert calls["n"] == 3
    assert "done" in content
    assert toolchain_cache.expanded_schema_tools(trace_id) == set()
    assert not any(
        "Tool Schema Retry Facts" in (m.get("content") or "")
        for m in msgs
        if isinstance(m, dict)
    )


@pytest.mark.asyncio
async def test_browser_pre_edit_main_edit_executes_with_runtime_fact(monkeypatch):
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
    monkeypatch.setattr(llm_client, "_retry", lambda fn, label="", provider=None: fn())
    monkeypatch.setattr(llm_client, "_serialize_assistant_message", _serialize_assistant_message)
    monkeypatch.setattr(
        llm_client,
        "_normalize_tool_call_args_for_dispatch",
        lambda raw: (json.loads(raw or "{}"), None, False),
    )
    monkeypatch.setattr(llm_client, "_maybe_clear_stale_upgrade", lambda *a, **k: None)
    monkeypatch.setattr(llm_client, "_try_extract_json_locally", lambda content: None)
    monkeypatch.setattr(llm_client, "_tool_result_signal", lambda result: (json.loads(result).get("ok") is not False, None))
    monkeypatch.setattr(llm_client, "_is_thinking_enabled", lambda extra: False)

    calls = {"llm": 0, "dispatch": 0}
    first_call_messages = []

    async def fake_stream(**kwargs):
        if calls["llm"] == 0:
            first_call_messages.extend(kwargs.get("msgs") or kwargs.get("messages") or [])
        calls["llm"] += 1
        if calls["llm"] == 1:
            return (
                _Resp(choices=[_Choice(message=_Msg(
                    content="",
                    tool_calls=[_tool_call(
                        "call_workspace_write",
                        "workspace",
                        {"action": "write", "path": "_env/report_client.py", "content": "x"},
                    )],
                ))]),
                _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
                "ok",
            )
        return (
            _Resp(choices=[_Choice(message=_Msg(
                content=json.dumps({"intent": "done", "key_points": ["tool executed"]}),
                tool_calls=[],
            ))]),
            _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
            "ok",
        )

    monkeypatch.setattr(llm_client, "_call_llm_streaming_with_idle", fake_stream)

    async def dispatcher(name, args):
        calls["dispatch"] += 1
        return json.dumps({"ok": True, "path": args.get("path")}, ensure_ascii=False)

    content, msgs = await chat_with_tools_loop(
        [
            {"role": "system", "content": "return json"},
            {
                "role": "user",
                "content": "Use the browser tool to reproduce the bug in the host browser before editing, then fix the frontend and verify it.",
            },
        ],
        [{"type": "function", "function": {"name": "workspace", "parameters": {"type": "object"}}}],
        dispatcher=dispatcher,
        require_first_tool_call=False,
    )

    assert calls["dispatch"] == 1
    assert "done" in content
    first_visible = "\n".join(str(m.get("content") or "") for m in first_call_messages if isinstance(m, dict))
    assert "[SYSTEM_HINT/browser_pre_edit_evidence_boundary]" in first_visible
    assert "not a forced decision" in first_visible
    tool_messages = [m for m in msgs if isinstance(m, dict) and m.get("role") == "tool"]
    assert len(tool_messages) == 1
    visible = json.loads(tool_messages[0]["content"])
    assert visible["ok"] is True
    assert visible["path"] == "_env/report_client.py"
    warnings = visible.get("warnings") or []
    assert "browser_reproduction_evidence_missing_before_edit" in warnings
    assert "main_source_edit_should_delegate" in warnings
    assert all(fact.get("blocked_once") is False for fact in visible["runtime_facts"])


@pytest.mark.asyncio
async def test_post_tool_abort_budgets_large_tool_result_before_finalize(monkeypatch, tmp_path):
    from app.llm import client as llm_client
    from app.llm.client_tools_loop import chat_with_tools_loop

    abort_event = asyncio.Event()
    workspace_dir = str(tmp_path)

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

    class FakeCompletions:
        async def create(self, **kwargs):
            return _Resp(choices=[_Choice(message=_Msg(
                content=json.dumps({"intent": "aborted", "key_points": ["kept bounded evidence"]}),
                tool_calls=[],
            ))])

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    monkeypatch.setattr(llm_client, "_client_for_spec", lambda spec: fake_client)

    async def fake_stream(**kwargs):
        resp = _Resp(choices=[_Choice(message=_Msg(
            content="",
            tool_calls=[_tool_call("call_workspace_1", "workspace", {"action": "run"})],
        ))])
        return resp, _Collector(resp=resp, content="", reasoning_content="", tool_calls=[]), "ok"

    monkeypatch.setattr(llm_client, "_call_llm_streaming_with_idle", fake_stream)

    async def dispatcher(name, args):
        _ = workspace_dir
        abort_event.set()
        return json.dumps({
            "ok": True,
            "stdout": "HEAD\n" + ("x" * 40_000),
            "stderr": "short",
        }, ensure_ascii=False)

    content, msgs = await chat_with_tools_loop(
        [{"role": "system", "content": "return json"}, {"role": "user", "content": "run once"}],
        [{"type": "function", "function": {"name": "workspace", "parameters": {"type": "object"}}}],
        dispatcher=dispatcher,
        abort_event=abort_event,
        require_first_tool_call=False,
    )

    tool_messages = [m for m in msgs if isinstance(m, dict) and m.get("role") == "tool"]
    assert content
    assert len(tool_messages) == 1
    visible = json.loads(tool_messages[0]["content"])
    assert visible["tool_result_truncated"] is True
    assert visible["stdout_truncated"] is True
    assert len(visible["stdout"]) < 4000
    assert "x" * 10_000 not in tool_messages[0]["content"]
    assert (tmp_path / visible["full_result_saved_path"]).is_file()
    assert (tmp_path / visible["stdout_full_saved_path"]).read_text(encoding="utf-8").startswith("HEAD\n")


@pytest.mark.asyncio
async def test_workspace_scanning_verifier_fact_blocks_premature_chat_only_finalize(monkeypatch):
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
    monkeypatch.setattr(llm_client, "_retry", lambda fn, label="", provider=None: fn())
    monkeypatch.setattr(llm_client, "_serialize_assistant_message", _serialize_assistant_message)
    monkeypatch.setattr(
        llm_client,
        "_normalize_tool_call_args_for_dispatch",
        lambda raw: (json.loads(raw or "{}"), None, False),
    )
    monkeypatch.setattr(llm_client, "_maybe_clear_stale_upgrade", lambda *a, **k: None)
    monkeypatch.setattr(llm_client, "_try_extract_json_locally", lambda content: None)
    monkeypatch.setattr(llm_client, "_is_thinking_enabled", lambda extra: False)

    calls = {"n": 0}

    async def fake_stream(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                _Resp(choices=[_Choice(message=_Msg(
                    content="",
                    tool_calls=[_tool_call("call_read_verify", "env_read", {"path": "verify_summary.py"})],
                ))]),
                _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
                "ok",
            )
        if calls["n"] == 2:
            return (
                _Resp(choices=[_Choice(message=_Msg(
                    content=json.dumps({"intent": "answer only", "deliverables": [], "internal_note": "no artifacts"}),
                    tool_calls=[],
                ))]),
                _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
                "ok",
            )
        if calls["n"] == 3:
            joined = "\n".join(str(m.get("content") or "") for m in kwargs["msgs"])
            assert "verifier_visible_artifact_fact" in joined
            assert "chat-only final response is not verifier-visible" in joined
            calls["saw_hint"] = True
            return (
                _Resp(choices=[_Choice(message=_Msg(
                    content="",
                    tool_calls=[_tool_call("call_run_verify", "env_run", {"command": "python verify_summary.py"})],
                ))]),
                _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
                "ok",
            )
        return (
            _Resp(choices=[_Choice(message=_Msg(
                content=json.dumps({"intent": "verified", "key_points": ["verifier ran"]}),
                tool_calls=[],
            ))]),
            _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
            "ok",
        )

    monkeypatch.setattr(llm_client, "_call_llm_streaming_with_idle", fake_stream)

    async def dispatcher(name, args):
        if name == "env_read":
            return json.dumps({
                "ok": True,
                "path": "verify_summary.py",
                "acceptance_script_fact": {
                    "kind": "acceptance_script_read_fact",
                    "path": "verify_summary.py",
                    "scans_project_or_workspace_text": True,
                    "literal_string_lists": [{"name": "needed", "strings": ["decision"]}],
                },
            }, ensure_ascii=False)
        if name == "env_run":
            return json.dumps({
                "ok": True,
                "action": "env_run",
                "command": "python verify_summary.py",
                "returncode": 0,
                "stdout": "PASS\n",
            }, ensure_ascii=False)
        return json.dumps({"ok": True}, ensure_ascii=False)

    content, msgs = await chat_with_tools_loop(
        [{"role": "system", "content": "return json"}, {"role": "user", "content": "work"}],
        [
            {"type": "function", "function": {"name": "env_read", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "env_run", "parameters": {"type": "object"}}},
        ],
        dispatcher=dispatcher,
        require_first_tool_call=False,
    )

    assert calls["n"] == 4
    assert json.loads(content)["intent"] == "verified"
    assert calls["saw_hint"] is True


@pytest.mark.asyncio
async def test_listed_acceptance_script_fact_blocks_premature_chat_only_finalize(monkeypatch):
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
    monkeypatch.setattr(llm_client, "_retry", lambda fn, label="", provider=None: fn())
    monkeypatch.setattr(llm_client, "_serialize_assistant_message", _serialize_assistant_message)
    monkeypatch.setattr(
        llm_client,
        "_normalize_tool_call_args_for_dispatch",
        lambda raw: (json.loads(raw or "{}"), None, False),
    )
    monkeypatch.setattr(llm_client, "_maybe_clear_stale_upgrade", lambda *a, **k: None)
    monkeypatch.setattr(llm_client, "_try_extract_json_locally", lambda content: None)
    monkeypatch.setattr(llm_client, "_is_thinking_enabled", lambda extra: False)

    calls = {"n": 0}

    async def fake_stream(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                _Resp(choices=[_Choice(message=_Msg(
                    content="",
                    tool_calls=[_tool_call("call_list", "env_list_tree", {"path": "."})],
                ))]),
                _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
                "ok",
            )
        if calls["n"] == 2:
            return (
                _Resp(choices=[_Choice(message=_Msg(
                    content=json.dumps({"intent": "chat csv only", "deliverables": []}),
                    tool_calls=[],
                ))]),
                _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
                "ok",
            )
        if calls["n"] == 3:
            joined = "\n".join(str(m.get("content") or "") for m in kwargs["msgs"])
            assert "verifier_visible_artifact_fact" in joined
            assert "verify_results.py" in joined
            assert "script body not read in this loop" in joined
            calls["saw_hint"] = True
            return (
                _Resp(choices=[_Choice(message=_Msg(
                    content="",
                    tool_calls=[_tool_call("call_run_verify", "env_run", {"command": "python verify_results.py"})],
                ))]),
                _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
                "ok",
            )
        return (
            _Resp(choices=[_Choice(message=_Msg(
                content=json.dumps({"intent": "verified after listed script fact"}),
                tool_calls=[],
            ))]),
            _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
            "ok",
        )

    monkeypatch.setattr(llm_client, "_call_llm_streaming_with_idle", fake_stream)

    async def dispatcher(name, args):
        if name == "env_list_tree":
            return json.dumps({
                "ok": True,
                "root": ".",
                "items": [
                    {"path": "users.db", "type": "file", "size": 4096},
                    {"path": "verify_results.py", "type": "file", "size": 800},
                ],
                "truncated": False,
                "acceptance_script_paths": ["verify_results.py"],
            }, ensure_ascii=False)
        if name == "env_run":
            return json.dumps({
                "ok": True,
                "action": "env_run",
                "command": "python verify_results.py",
                "returncode": 0,
                "stdout": "PASS\n",
            }, ensure_ascii=False)
        return json.dumps({"ok": True}, ensure_ascii=False)

    content, msgs = await chat_with_tools_loop(
        [{"role": "system", "content": "return json"}, {"role": "user", "content": "Return the answer as CSV"}],
        [
            {"type": "function", "function": {"name": "env_list_tree", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "env_run", "parameters": {"type": "object"}}},
        ],
        dispatcher=dispatcher,
        require_first_tool_call=False,
    )

    assert calls["n"] == 4
    assert json.loads(content)["intent"] == "verified after listed script fact"
    assert calls["saw_hint"] is True


@pytest.mark.asyncio
async def test_main_project_discovery_injects_source_path_handoff_fact(monkeypatch):
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

    calls = {"n": 0, "saw_hint": False}

    async def fake_stream(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                _Resp(choices=[_Choice(message=_Msg(
                    content="",
                    tool_calls=[_tool_call("call_list", "env_list_tree", {"path": "."})],
                ))]),
                _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
                "ok",
            )
        joined = "\n".join(str(m.get("content") or "") for m in kwargs["msgs"])
        assert "main_source_path_handoff_fact" in joined
        assert "contracts/customer_event.py" in joined
        assert "service/render.py" in joined
        assert "delegate prompt 不粘贴" in joined
        assert "do not paste complete source-code blocks" in joined
        assert "focused code helper" not in joined
        assert "helper can read source bodies" not in joined
        assert "helper-owned reading" not in joined
        calls["saw_hint"] = True
        return (
            _Resp(choices=[_Choice(message=_Msg(
                content=json.dumps({"intent": "delegate from path facts"}),
                tool_calls=[],
            ))]),
            _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
            "ok",
        )

    monkeypatch.setattr(llm_client, "_call_llm_streaming_with_idle", fake_stream)

    async def dispatcher(name, args):
        assert name == "env_list_tree"
        return json.dumps({
            "ok": True,
            "items": [
                {"path": "contracts/customer_event.py", "type": "file", "size": 100},
                {"path": "contracts/tests/test_schema.py", "type": "file", "size": 100},
                {"path": "service/render.py", "type": "file", "size": 100},
                {"path": "service/tests/test_client.py", "type": "file", "size": 100},
            ],
            "helper_handoff_fact": {
                "project_paths": [
                    "contracts/customer_event.py",
                    "contracts/tests/test_schema.py",
                    "service/render.py",
                    "service/tests/test_client.py",
                ],
            },
        }, ensure_ascii=False)

    content, _msgs = await chat_with_tools_loop(
        [{"role": "system", "content": "return json"}, {"role": "user", "content": "migrate fields"}],
        [{"type": "function", "function": {"name": "env_list_tree", "parameters": {"type": "object"}}}],
        dispatcher=dispatcher,
        require_first_tool_call=False,
    )

    assert calls["saw_hint"] is True
    assert json.loads(content)["intent"] == "delegate from path facts"


@pytest.mark.asyncio
async def test_main_project_discovery_handoff_fact_is_after_parallel_results(monkeypatch):
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

    calls = {"n": 0, "ordered": False}

    async def fake_stream(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                _Resp(choices=[_Choice(message=_Msg(
                    content="",
                    tool_calls=[
                        _tool_call("call_list", "env_list_tree", {"path": "."}),
                        _tool_call("call_search", "env_search", {"query": "customer_name"}),
                    ],
                ))]),
                _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
                "ok",
            )
        contents = [str(m.get("content") or "") for m in kwargs["msgs"]]
        combined = "\n".join(contents)
        hint_index = combined.index("main_source_path_handoff_fact")
        search_index = combined.index('"matches"')
        assert hint_index > search_index
        calls["ordered"] = True
        return (
            _Resp(choices=[_Choice(message=_Msg(
                content=json.dumps({"intent": "saw ordered hint"}),
                tool_calls=[],
            ))]),
            _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
            "ok",
        )

    monkeypatch.setattr(llm_client, "_call_llm_streaming_with_idle", fake_stream)

    async def dispatcher(name, args):
        if name == "env_list_tree":
            return json.dumps({
                "ok": True,
                "items": [
                    {"path": "contracts/customer_event.py", "type": "file", "size": 100},
                    {"path": "service/render.py", "type": "file", "size": 100},
                ],
                "helper_handoff_fact": {
                    "project_paths": [
                        "contracts/customer_event.py",
                        "service/render.py",
                    ],
                },
            }, ensure_ascii=False)
        assert name == "env_search"
        return json.dumps({
            "ok": True,
            "matches": [
                {"path": "contracts/customer_event.py", "line": 2, "text": "customer_name"},
                {"path": "service/render.py", "line": 2, "text": "customer_name"},
            ],
        }, ensure_ascii=False)

    content, _msgs = await chat_with_tools_loop(
        [{"role": "system", "content": "return json"}, {"role": "user", "content": "migrate fields"}],
        [
            {"type": "function", "function": {"name": "env_list_tree", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "env_search", "parameters": {"type": "object"}}},
        ],
        dispatcher=dispatcher,
        require_first_tool_call=False,
    )

    assert calls["ordered"] is True
    assert json.loads(content)["intent"] == "saw ordered hint"


@pytest.mark.asyncio
async def test_main_helper_handoff_ready_fact_is_not_injected_immediately(monkeypatch):
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

    calls = {"n": 0, "saw_no_ready": False}

    async def fake_stream(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                _Resp(choices=[_Choice(message=_Msg(
                    content="",
                    tool_calls=[_tool_call("call_list", "env_list_tree", {"path": "."})],
                ))]),
                _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
                "ok",
            )
        joined = "\n".join(str(m.get("content") or "") for m in kwargs["msgs"])
        assert "main_helper_handoff_ready_fact" not in joined
        assert "users.db" in joined
        assert "verify_results.py" in joined
        calls["saw_no_ready"] = True
        return (
            _Resp(choices=[_Choice(message=_Msg(
                content=json.dumps({"intent": "no immediate handoff ready"}),
                tool_calls=[],
            ))]),
            _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
            "ok",
        )

    monkeypatch.setattr(llm_client, "_call_llm_streaming_with_idle", fake_stream)

    async def dispatcher(name, args):
        return json.dumps({
            "ok": True,
            "items": [
                {"path": "users.db", "type": "file", "size": 100},
                {"path": "verify_results.py", "type": "file", "size": 100},
            ],
            "helper_handoff_fact": {
                "project_paths": ["users.db", "verify_results.py"],
                "data_paths": ["users.db"],
                "acceptance_script_paths": ["verify_results.py"],
            },
        }, ensure_ascii=False)

    content, _msgs = await chat_with_tools_loop(
        [{"role": "system", "content": "return json"}, {"role": "user", "content": "query db"}],
        [{"type": "function", "function": {"name": "env_list_tree", "parameters": {"type": "object"}}}],
        dispatcher=dispatcher,
        require_first_tool_call=False,
    )

    assert calls["saw_no_ready"] is True
    assert json.loads(content)["intent"] == "no immediate handoff ready"


@pytest.mark.asyncio
async def test_main_text_material_handoff_fact_after_compact_listing(monkeypatch):
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

    calls = {"n": 0, "saw_hint": False}

    async def fake_stream(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                _Resp(choices=[_Choice(message=_Msg(
                    content="",
                    tool_calls=[_tool_call("call_list", "env_list_tree", {"path": "."})],
                ))]),
                _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
                "ok",
            )
        joined = "\n".join(str(m.get("content") or "") for m in kwargs["msgs"])
        assert "main_text_material_handoff_fact" in joined
        assert "inbox/01.txt" in joined
        assert "prefs.yaml" in joined
        assert "not a forced decision" in joined
        assert "one focused reading/implementation step" in joined
        assert "focused read/edit/code helper" not in joined
        calls["saw_hint"] = True
        return (
            _Resp(choices=[_Choice(message=_Msg(
                content=json.dumps({"intent": "delegate material reading"}),
                tool_calls=[],
            ))]),
            _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
            "ok",
        )

    monkeypatch.setattr(llm_client, "_call_llm_streaming_with_idle", fake_stream)

    async def dispatcher(name, args):
        assert name == "env_list_tree"
        return json.dumps({
            "ok": True,
            "items": [
                {"path": "inbox/01.txt", "type": "file", "size": 100},
                {"path": "inbox/02.txt", "type": "file", "size": 100},
                {"path": "inbox/03.txt", "type": "file", "size": 100},
                {"path": "prefs.yaml", "type": "file", "size": 100},
                {"path": "verify_outputs.py", "type": "file", "size": 100},
            ],
            "text_material_handoff_fact": {
                "kind": "compact_text_material_set",
                "material_paths": ["inbox/01.txt", "inbox/02.txt", "inbox/03.txt", "prefs.yaml"],
                "acceptance_script_paths": ["verify_outputs.py"],
            },
        }, ensure_ascii=False)

    content, _msgs = await chat_with_tools_loop(
        [{"role": "system", "content": "return json"}, {"role": "user", "content": "triage these messages"}],
        [{"type": "function", "function": {"name": "env_list_tree", "parameters": {"type": "object"}}}],
        dispatcher=dispatcher,
        require_first_tool_call=False,
    )

    assert calls["saw_hint"] is True
    assert json.loads(content)["intent"] == "delegate material reading"


@pytest.mark.asyncio
async def test_main_helper_handoff_overwork_fact_after_repeated_direct_work(monkeypatch):
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

    calls = {"n": 0, "saw_hint": False}

    async def fake_stream(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                _Resp(choices=[_Choice(message=_Msg(
                    content="",
                    tool_calls=[_tool_call("call_list", "env_list_tree", {"path": "."})],
                ))]),
                _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
                "ok",
            )
        if calls["n"] <= 6:
            tool = ["env_read", "env_run", "workspace", "bash", "python"][calls["n"] - 2]
            args = {"path": "verify_results.py"} if tool == "env_read" else {"command": "python probe.py"}
            if tool == "workspace":
                args = {"action": "write", "path": "probe.py", "content": "print('probe')\n"}
            if calls["n"] == 2:
                joined = "\n".join(str(m.get("content") or "") for m in kwargs["msgs"])
                assert "processing_handoff_fact" in joined
                assert "helper_handoff_fact" not in joined
            if calls["n"] == 5:
                joined = "\n".join(str(m.get("content") or "") for m in kwargs["msgs"])
                assert "main_processing_handoff_overwork_fact" not in joined
                assert "main_helper_handoff_overwork_fact" not in joined
            return (
                _Resp(choices=[_Choice(message=_Msg(
                    content="",
                    tool_calls=[_tool_call(f"call_direct_{calls['n']}", tool, args)],
                ))]),
                _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
                "ok",
            )
        if calls["n"] == 7:
            joined = "\n".join(str(m.get("content") or "") for m in kwargs["msgs"])
            assert "main_processing_handoff_overwork_fact" in joined
            assert "main_helper_handoff_overwork_fact" not in joined
            assert "users.db" in joined
            assert "verify_results.py" in joined
            assert "not a forced decision" in joined
            assert "focused-step input paths" in joined
            assert "focused helper" not in joined
            assert "helper-suitable" not in joined
            calls["saw_hint"] = True
            return (
                _Resp(choices=[_Choice(message=_Msg(
                    content="",
                    tool_calls=[_tool_call("call_delegate", "delegate", {"tasks": [{"task_id": "query", "kind": "code"}]})],
                ))]),
                _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
                "ok",
            )
        return (
            _Resp(choices=[_Choice(message=_Msg(
                content=json.dumps({"intent": "delegated after overwork fact"}),
                tool_calls=[],
            ))]),
            _Collector(resp=None, content="", reasoning_content="", tool_calls=[]),
            "ok",
        )

    monkeypatch.setattr(llm_client, "_call_llm_streaming_with_idle", fake_stream)

    async def dispatcher(name, args):
        if name == "env_list_tree":
            return json.dumps({
                "ok": True,
                "items": [
                    {"path": "users.db", "type": "file", "size": 100},
                    {"path": "verify_results.py", "type": "file", "size": 100},
                ],
                "helper_handoff_fact": {
                    "project_paths": ["users.db", "verify_results.py"],
                    "data_paths": ["users.db"],
                    "acceptance_script_paths": ["verify_results.py"],
                },
            }, ensure_ascii=False)
        if name == "delegate":
            return json.dumps({"ok": True, "helpers_completed": 1, "helpers_still_running": 0}, ensure_ascii=False)
        return json.dumps({"ok": True, "action": name}, ensure_ascii=False)

    content, _msgs = await chat_with_tools_loop(
        [{"role": "system", "content": "return json"}, {"role": "user", "content": "query db"}],
        [
            {"type": "function", "function": {"name": "env_list_tree", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "env_read", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "env_run", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "workspace", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "python", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "delegate", "parameters": {"type": "object"}}},
        ],
        dispatcher=dispatcher,
        require_first_tool_call=False,
    )

    assert calls["saw_hint"] is True
    assert calls["n"] >= 8
    assert json.loads(content)["intent"] == "delegated after overwork fact"
    delegate_tool_contents = [
        m.get("content", "")
        for m in _msgs
        if m.get("role") == "tool" and "results_returned" in str(m.get("content", ""))
    ]
    assert delegate_tool_contents
    delegate_tool_text = "\n".join(str(x) for x in delegate_tool_contents).lower()
    assert "helpers_completed" not in delegate_tool_text
    assert "helpers_still_running" not in delegate_tool_text
    assert "helper" not in delegate_tool_text


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
    assert not any(
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


def test_final_cleanup_fidelity_rejects_identifier_rename():
    from app.llm.client_tools_loop import _cleanup_preserves_exact_tokens

    original = (
        "Schema evidence: users.referrer_id is the FK to channels.id. "
        "The SQL used JOIN channels ch ON u.referrer_id = ch.id and output alias acquisition_channel."
    )
    cleanup = json.dumps({
        "intent": "summarize schema",
        "key_points": [
            "JOIN logic: users.channel_id -> channels.id",
            "Output alias acquisition_channel is valid.",
        ],
    }, ensure_ascii=False)

    ok, reason = _cleanup_preserves_exact_tokens(original, cleanup)
    assert ok is False
    assert "users.referrer_id" in reason


def test_final_cleanup_fidelity_accepts_preserved_identifiers():
    from app.llm.client_tools_loop import _cleanup_preserves_exact_tokens

    original = (
        "Schema evidence: users.referrer_id is the FK to channels.id. "
        "The SQL used JOIN channels ch ON u.referrer_id = ch.id and output alias acquisition_channel."
    )
    cleanup = json.dumps({
        "intent": "summarize schema",
        "key_points": [
            "JOIN logic: users.referrer_id -> channels.id",
            "Output alias acquisition_channel is an output alias, not a source-table field.",
        ],
    }, ensure_ascii=False)

    ok, reason = _cleanup_preserves_exact_tokens(original, cleanup)
    assert ok is True
    assert "preserved=" in reason


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


def test_large_tool_result_preserves_tts_voice_candidate_fields():
    from app.llm.client_tools_loop import _summarize_large_tool_result

    payload = {
        "ok": True,
        "task_ok": True,
        "helpers_completed": 1,
        "results": [
            {
                "task_id": "voice_reply",
                "kind": "tts",
                "ok": True,
                "terminal_reason": "completed",
                "report": "VERDICT: PASS\n" + "speech generated\n" * 300,
                "voice_reply_file_candidate": "reply.wav",
                "deliverable_candidate": "reply.wav",
                "delivery_guidance": "use voice_reply_file when it is the final spoken reply",
                "outputs_check": {
                    "outputs_complete": True,
                    "producer_self_verified": True,
                },
                "files": ["reply.wav"],
            }
        ],
    }

    summary = _summarize_large_tool_result(
        "delegate",
        json.dumps(payload, ensure_ascii=False),
        1800,
    )

    assert summary is not None
    parsed = json.loads(summary.split("\n[structured summary shortened]", 1)[0])
    item = parsed["result_items"][0]
    assert item["kind"] == "tts"
    assert item["voice_reply_file_candidate"] == "reply.wav"
    assert item["deliverable_candidate"] == "reply.wav"
    assert "final spoken reply" in item["delivery_guidance"]


def test_large_tool_result_spills_long_stdout_to_file(tmp_path):
    from app.llm.client_tools_loop import _spill_large_tool_result_for_context

    stdout = "head-line\n" + ("x" * 20_000)
    raw = json.dumps({
        "ok": True,
        "action": "env_run",
        "stdout": stdout,
        "stderr": "short",
    }, ensure_ascii=False)

    visible = _spill_large_tool_result_for_context(
        "env_run",
        raw,
        spill_root=str(tmp_path),
        iteration=3,
        call_id="call_stdout",
        total_threshold=10_000,
        field_threshold=1_000,
        field_head_chars=200,
    )
    parsed = json.loads(visible)

    assert parsed["tool_result_truncated"] is True
    assert parsed["stdout_truncated"] is True
    assert parsed["stdout"].startswith("head-line")
    assert len(parsed["stdout"]) == 200
    saved = tmp_path / parsed["full_result_saved_path"]
    assert saved.exists()
    assert "x" * 1000 in saved.read_text(encoding="utf-8")


def test_structured_summary_preserves_spilled_tool_result_paths(tmp_path):
    from app.llm.client_tools_loop import (
        _spill_large_tool_result_for_context,
        _summarize_large_tool_result,
    )

    stdout = "head-line\n" + ("x" * 20_000)
    raw = json.dumps({
        "ok": True,
        "action": "env_run",
        "stdout": stdout,
        "stderr": "short",
    }, ensure_ascii=False)
    spilled = _spill_large_tool_result_for_context(
        "env_run",
        raw,
        spill_root=str(tmp_path),
        iteration=3,
        call_id="call_stdout",
        total_threshold=10_000,
        field_threshold=1_000,
        field_head_chars=200,
    )

    summary = _summarize_large_tool_result("env_run", spilled, 1200, force=True, compact=True)

    assert summary is not None
    parsed = json.loads(summary)
    assert parsed["tool_result_truncated"] is True
    assert parsed["full_result_saved_path"]
    assert parsed["stdout_full_saved_path"]
    assert parsed["stdout_full_saved_path"] != parsed["full_result_saved_path"]
    assert (tmp_path / parsed["full_result_saved_path"]).is_file()
    assert (tmp_path / parsed["stdout_full_saved_path"]).is_file()
    assert (tmp_path / parsed["stdout_full_saved_path"]).read_text(encoding="utf-8") == stdout


def test_large_non_json_tool_result_spills_to_file(tmp_path):
    from app.llm.client_tools_loop import _spill_large_tool_result_for_context

    raw = "ERROR\n" + ("trace line\n" * 5000)

    visible = _spill_large_tool_result_for_context(
        "bash",
        raw,
        spill_root=str(tmp_path),
        iteration=4,
        call_id="call_err",
        total_threshold=1000,
        field_threshold=1000,
        field_head_chars=120,
    )
    parsed = json.loads(visible)

    assert parsed["tool_result_truncated"] is True
    assert parsed["head_excerpt"].startswith("ERROR")
    assert len(parsed["head_excerpt"]) == 120
    saved = tmp_path / parsed["full_result_saved_path"]
    assert saved.exists()
    assert saved.read_text(encoding="utf-8") == raw


def test_p44_fallback_keeps_head_only_not_tail():
    from app.llm.client_tools_loop import _head_only_tool_result_fallback

    raw = "HEAD\n" + ("middle\n" * 2000) + "TAIL_SHOULD_NOT_BE_VISIBLE"

    visible = _head_only_tool_result_fallback(raw, original_chars=len(raw), budget_chars=1200)

    assert visible.startswith("HEAD")
    assert "TAIL_SHOULD_NOT_BE_VISIBLE" not in visible
    assert "only the head excerpt is visible" in visible
    assert "full_result_saved_path" in visible
    assert str(len(raw)) in visible


def test_large_tool_result_spills_long_message_to_file(tmp_path):
    from app.llm.client_tools_loop import _spill_large_tool_result_for_context

    message = "failure head\n" + ("m" * 20_000)
    raw = json.dumps({
        "ok": False,
        "action": "env_run",
        "message": message,
        "stderr": "short",
    }, ensure_ascii=False)

    visible = _spill_large_tool_result_for_context(
        "env_run",
        raw,
        spill_root=str(tmp_path),
        iteration=5,
        call_id="call_message",
        total_threshold=30_000,
        field_threshold=1_000,
        field_head_chars=180,
    )
    parsed = json.loads(visible)

    assert parsed["tool_result_truncated"] is True
    assert parsed["message_truncated"] is True
    assert parsed["message"].startswith("failure head")
    assert len(parsed["message"]) == 180
    saved = tmp_path / parsed["message_full_saved_path"]
    assert saved.exists()
    assert saved.read_text(encoding="utf-8") == message


def test_large_tool_result_spills_long_file_content_to_file(tmp_path):
    from app.llm.client_tools_loop import _spill_large_tool_result_for_context

    content = "first line\n" + ("file-data\n" * 4000)
    raw = json.dumps({
        "ok": True,
        "action": "read_file",
        "path": "large.md",
        "content": content,
    }, ensure_ascii=False)

    visible = _spill_large_tool_result_for_context(
        "read_file",
        raw,
        spill_root=str(tmp_path),
        iteration=6,
        call_id="call_read",
        total_threshold=50_000,
        field_threshold=1_000,
        field_head_chars=160,
    )
    parsed = json.loads(visible)

    assert parsed["tool_result_truncated"] is True
    assert parsed["content_truncated"] is True
    assert parsed["content"].startswith("first line")
    assert len(parsed["content"]) == 160
    saved = tmp_path / parsed["content_full_saved_path"]
    assert saved.exists()
    assert saved.read_text(encoding="utf-8") == content


def test_large_tool_result_spills_nested_file_content_to_file(tmp_path):
    from app.llm.client_tools_loop import _spill_large_tool_result_for_context

    content = "nested first line\n" + ("file-data\n" * 3000)
    raw = json.dumps({
        "ok": True,
        "action": "workspace",
        "files": [{"path": "large.md", "file_content": content}],
    }, ensure_ascii=False)

    visible = _spill_large_tool_result_for_context(
        "workspace",
        raw,
        spill_root=str(tmp_path),
        iteration=7,
        call_id="call_nested_read",
        total_threshold=50_000,
        field_threshold=1_000,
        field_head_chars=180,
    )
    parsed = json.loads(visible)
    item = parsed["files"][0]

    assert parsed["tool_result_truncated"] is True
    assert item["file_content_truncated"] is True
    assert item["file_content"].startswith("nested first line")
    assert len(item["file_content"]) == 180
    saved = tmp_path / item["file_content_full_saved_path"]
    assert saved.exists()
    assert saved.read_text(encoding="utf-8") == content


def test_large_tool_result_fallback_summary_keeps_field_saved_paths(tmp_path):
    from app.llm.client_tools_loop import _spill_large_tool_result_for_context

    stdout = "stdout head\n" + ("x" * 5000)
    raw = json.dumps({
        "ok": True,
        "action": "env_run",
        "stdout": stdout,
        "records": [{"idx": idx, "value": "y" * 80} for idx in range(120)],
    }, ensure_ascii=False)

    visible = _spill_large_tool_result_for_context(
        "env_run",
        raw,
        spill_root=str(tmp_path),
        iteration=8,
        call_id="call_fallback",
        total_threshold=1500,
        field_threshold=100,
        field_head_chars=80,
    )
    parsed = json.loads(visible)

    assert parsed["tool_result_truncated"] is True
    assert parsed["output_truncated"] is True
    assert parsed["full_result_saved_path"]
    assert parsed["field_full_saved_paths"]["stdout"]
    assert (tmp_path / parsed["full_result_saved_path"]).is_file()
    assert (tmp_path / parsed["field_full_saved_paths"]["stdout"]).read_text(encoding="utf-8") == stdout


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
    assert "rerun targeted reads for full evidence" not in parsed["policy"]
    assert "Only request a targeted follow-up read for a named missing detail" in parsed["policy"]
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


def test_environment_helper_input_file_path_facts_include_staged_and_bare_sizes(tmp_path):
    from app.llm.tools.delegate_runner import _helper_input_file_path_facts

    (tmp_path / "_env").mkdir()
    (tmp_path / "_env" / "users.db").write_bytes(b"sqlite data")
    (tmp_path / "users.db").write_bytes(b"")

    facts = _helper_input_file_path_facts(
        ["users.db"],
        helper_workspace=str(tmp_path),
        environment_mode=True,
    )

    assert "## Input File Path Facts" in facts
    assert "project_path=`users.db`" in facts
    assert "staged_path=`_env/users.db`" in facts
    assert "staged_exists=true" in facts
    assert "staged_size_bytes=11" in facts
    assert "bare_exists=true" in facts
    assert "bare_size_bytes=0" in facts


def test_edit_helper_prompt_encourages_batched_office_builds():
    from app.llm.tools.delegate_runner import _build_helper_user_prompt

    prompt = _build_helper_user_prompt(
        prompt="Assemble a DOCX report from evidence files.",
        dynamic_prompt_prefix_parts=[],
        kind="edit",
    )

    assert prompt.startswith("## Edit Helper Operating Contract")
    assert "batch 3-6 sections" in prompt
    assert "verify structure and claims near the end" in prompt
    assert "Office 文档批量写入、末端验证" in prompt


def test_edit_helper_prompt_requires_exact_small_structured_source_values():
    from app.llm.tools import helper_prompt_catalog

    prompt = helper_prompt_catalog._select_helper_system("edit", "easy")

    assert "For small structured sources such as CSV, TSV, and JSON under a few MB" in prompt
    assert "Do not approximate, extrapolate, or fill CSV-backed table values" in prompt
    assert "unavailable/PARTIAL" in prompt


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


def test_recent_verified_files_ignore_failed_delegate_path_noise():
    from app.llm.client_tools_loop import _recent_verified_files_from_tools

    failed = {
        "ok": True,
        "task_ok": False,
        "results": [{
            "task_id": "old_compile",
            "ok": False,
            "terminal_reason": "failed",
            "files": ["old_zstd_report.docx"],
            "outputs_check": {"outputs_complete": False, "outputs_missing": ["old_zstd_report.docx"]},
        }],
    }
    success = {
        "ok": True,
        "task_ok": True,
        "results": [{
            "task_id": "report_writer",
            "ok": True,
            "terminal_reason": "completed",
            "main_available_files": ["_env/reports/compression_report.docx"],
            "file_map": [{
                "helper_name": "reports/compression_report.docx",
                "main_name": "_env/reports/compression_report.docx",
            }],
            "outputs_check": {"outputs_complete": True, "outputs_missing": []},
        }],
    }
    messages = [
        {"role": "tool", "content": json.dumps(failed, ensure_ascii=False)},
        {"role": "tool", "content": json.dumps(success, ensure_ascii=False)},
    ]

    assert _recent_verified_files_from_tools(messages) == ["compression_report.docx"]


def test_retryable_delegate_facts_include_resource_required():
    from app.llm.client_tools_loop import _retryable_delegate_facts_from_result

    payload = {
        "ok": True,
        "task_ok": False,
        "results": [{
            "task_id": "paper_edit",
            "ok": False,
            "terminal_reason": "resource_required",
            "resource_required": {"matching_helper_kind": "draw", "suggested_helper_kind": "draw"},
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


def test_tts_resource_required_is_gap_fact_not_retry_blocker():
    from app.llm.client_tools_loop import (
        _delegate_gap_facts_from_result,
        _retryable_delegate_facts_from_result,
    )

    payload = {
        "ok": True,
        "task_ok": False,
        "results": [{
            "task_id": "catgirl_voice_reply",
            "ok": False,
            "kind": "tts",
            "terminal_reason": "resource_required",
            "resource_required": {
                "matching_helper_kind": "tts",
                "suggested_helper_kind": "tts",
                "blocked_reason": "voice generation authorization context unavailable",
            },
            "outputs_check": {
                "outputs_complete": False,
                "outputs_missing": ["catgirl_voice_reply.wav"],
            },
        }],
    }

    raw = json.dumps(payload, ensure_ascii=False)
    retry_facts = _retryable_delegate_facts_from_result(raw)
    gap_facts = _delegate_gap_facts_from_result(raw)

    assert retry_facts == []
    assert len(gap_facts) == 1
    assert gap_facts[0]["task_id"] == "catgirl_voice_reply"
    assert gap_facts[0]["nonblocking_tts_generation_fact"] is True
    assert gap_facts[0]["gap_kind"] == "tts_generation_not_completed"


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


def test_current_turn_read_only_detection_uses_current_message_tail():
    from app.llm.client_tools_loop import (
        _current_turn_forbids_file_writes,
        _latest_current_user_segment,
    )

    assert _current_turn_forbids_file_writes([
        {
            "role": "user",
            "content": (
                "## Conversation History\n"
                "[User] 不要改\n\n"
                "## Current Message To Answer\n"
                "现在请直接实现这个修复"
            ),
        }
    ]) is False
    dynamic_tail = (
        "## Conversation History\n"
        "[User] analysis only\n\n"
        "## Current Message To Answer\n"
        "Return the answer as CSV.\n\n"
        "---\n\n"
        "## Round 2 Dynamic Task Guidance\n"
        "Read-only task-local context for this planning run."
    )
    assert _current_turn_forbids_file_writes([
        {"role": "user", "content": dynamic_tail},
    ]) is False
    assert "Round 2 Dynamic Task Guidance" not in _latest_current_user_segment([
        {"role": "user", "content": dynamic_tail},
    ])
    assert _current_turn_forbids_file_writes([
        {
            "role": "user",
            "content": (
                "## Current Message To Answer\n"
                "Return the answer as CSV."
            ),
        },
        {
            "role": "user",
            "content": (
                "## Tool Loop Dynamic Guidance\n"
                "Read-only runtime guidance for the next tool-planning step."
            ),
        },
    ]) is False
    assert _current_turn_forbids_file_writes([
        {
            "role": "user",
            "content": (
                "## Conversation History\n"
                "[User] 请实现\n\n"
                "## Current Message To Answer\n"
                "查看代码并给方案，不要自行改，只读分析"
            ),
        }
    ]) is True


def test_main_workspace_write_has_read_only_warning_guard():
    from pathlib import Path

    src = Path("app/llm/client_tools_loop.py").read_text(encoding="utf-8")

    assert "current_turn_explicitly_read_only" in src
    assert "_read_only_write_warning_paths" in src


def test_post_apply_verification_checkpoint_is_model_visible_fact():
    import inspect
    from app.llm import client_tools_loop as loop

    src = inspect.getsource(loop.chat_with_tools_loop)

    assert "_main_last_successful_verifier_iter" in src
    assert "_main_last_project_state_mutation_iter" in src
    assert "_main_last_successful_verifier_seq" in src
    assert "_main_last_project_state_mutation_seq" in src
    assert "post_apply_verification_fact" in src
    assert "not a forced decision" in src
    assert "does not show a later successful verifier/check command" in src
    assert "_main_last_project_state_mutation_seq > _main_last_successful_verifier_seq" in src
    assert "not _main_last_project_state_mutation_helper_owned" in src
    assert "_env_apply_uses_helper_staged_source" in src
    assert "_main_post_apply_verifier_checkpoint_max_nudges = 3" in src
    assert "_main_post_apply_verifier_checkpoint_pair != _post_apply_pair" in src
    assert "latest_project_state_mutation_seq=" in src
    assert "checkpoint_nudge=" in src
    assert "refresh_same_tag=True" in src
    assert "blocked_once" in src


def test_dynamic_guidance_refresh_replaces_same_tag_without_duplicate():
    from app.llm.client_tools_loop import _append_tool_loop_dynamic_guidance

    msgs = []
    _append_tool_loop_dynamic_guidance(
        msgs,
        "[SYSTEM_HINT/post_apply_verification_fact]\ncheckpoint_nudge=1/3",
    )
    _append_tool_loop_dynamic_guidance(
        msgs,
        "[SYSTEM_HINT/other_fact]\nkeep this fact",
    )
    _append_tool_loop_dynamic_guidance(
        msgs,
        "[SYSTEM_HINT/post_apply_verification_fact]\ncheckpoint_nudge=2/3",
        refresh_same_tag=True,
    )

    combined = "\n".join(str(m.get("content", "")) for m in msgs)
    assert combined.count("[SYSTEM_HINT/post_apply_verification_fact]") == 1
    assert "checkpoint_nudge=1/3" not in combined
    assert "checkpoint_nudge=2/3" in combined
    assert "[SYSTEM_HINT/other_fact]" in combined


def test_blocked_large_write_compacts_assistant_tool_args():
    from app.llm.client_tools_loop import _compact_blocked_large_write_tool_args_in_last_assistant

    large = "x" * 5000
    msgs = [{
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "workspace",
                "arguments": json.dumps({
                    "action": "write",
                    "path": "large.md",
                    "content": large,
                }, ensure_ascii=False),
            },
        }],
    }]
    result = json.dumps({
        "ok": False,
        "error_kind": "main_thread_large_write_should_delegate_or_segment",
    }, ensure_ascii=False)

    changed = _compact_blocked_large_write_tool_args_in_last_assistant(
        msgs,
        call_id="call_1",
        args={"action": "write", "path": "large.md", "content": large},
        result=result,
    )

    assert changed is True
    args = json.loads(msgs[0]["tool_calls"][0]["function"]["arguments"])
    assert args["path"] == "large.md"
    assert args["content"].startswith("[content omitted")
    assert "x" * 100 not in args["content"]


def test_blocked_large_append_compacts_assistant_tool_args():
    from app.llm.client_tools_loop import _compact_blocked_large_write_tool_args_in_last_assistant

    large = "append-line\n" * 500
    msgs = [{
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_append",
            "type": "function",
            "function": {
                "name": "workspace",
                "arguments": json.dumps({
                    "action": "append",
                    "path": "large.md",
                    "content": large,
                }, ensure_ascii=False),
            },
        }],
    }]
    result = json.dumps({
        "ok": False,
        "error_kind": "main_thread_large_write_should_delegate_or_segment",
    }, ensure_ascii=False)

    changed = _compact_blocked_large_write_tool_args_in_last_assistant(
        msgs,
        call_id="call_append",
        args={"action": "append", "path": "large.md", "content": large},
        result=result,
    )

    assert changed is True
    args = json.loads(msgs[0]["tool_calls"][0]["function"]["arguments"])
    assert args["action"] == "append"
    assert args["content"].startswith("[content omitted")
    assert "append-line" not in args["content"]


def test_blocked_env_apply_create_compacts_assistant_tool_args():
    from app.llm.client_tools_loop import _compact_blocked_large_write_tool_args_in_last_assistant

    large = "def f():\n    return 1\n" * 250
    msgs = [{
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_env",
            "type": "function",
            "function": {
                "name": "env_apply_create",
                "arguments": json.dumps({
                    "path": "src/generated.py",
                    "content": large,
                }, ensure_ascii=False),
            },
        }],
    }]
    result = json.dumps({
        "ok": False,
        "error_kind": "main_thread_source_create_should_delegate",
    }, ensure_ascii=False)

    changed = _compact_blocked_large_write_tool_args_in_last_assistant(
        msgs,
        tool_name="env_apply_create",
        call_id="call_env",
        args={"path": "src/generated.py", "content": large},
        result=result,
    )

    assert changed is True
    args = json.loads(msgs[0]["tool_calls"][0]["function"]["arguments"])
    assert args["path"] == "src/generated.py"
    assert args["content"].startswith("[content omitted")
    assert "def f" not in args["content"]
    assert args["_omitted_content_reason"] == "main_thread_source_create_should_delegate"


def test_blocked_environment_workspace_write_compacts_assistant_tool_args():
    from app.llm.client_tools_loop import _compact_blocked_large_write_tool_args_in_last_assistant

    large = "report line\n" * 500
    msgs = [{
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_workspace",
            "type": "function",
            "function": {
                "name": "workspace",
                "arguments": json.dumps({
                    "action": "write",
                    "path": "reports/analysis.md",
                    "content": large,
                }, ensure_ascii=False),
            },
        }],
    }]
    result = json.dumps({
        "ok": False,
        "blocked_reason": "environment_workspace_write_not_project_file",
    }, ensure_ascii=False)

    changed = _compact_blocked_large_write_tool_args_in_last_assistant(
        msgs,
        call_id="call_workspace",
        args={"action": "write", "path": "reports/analysis.md", "content": large},
        result=result,
    )

    assert changed is True
    args = json.loads(msgs[0]["tool_calls"][0]["function"]["arguments"])
    assert args["path"] == "reports/analysis.md"
    assert args["content"].startswith("[content omitted")
    assert "report line" not in args["content"]
    assert args["_omitted_content_reason"] == "environment_workspace_write_not_project_file"


def test_delegate_source_blocks_are_omitted_when_input_files_carry_paths():
    from app.llm.tools.delegate import _strip_redundant_input_file_source_blocks

    prompt = (
        "Task: update service/render.py.\n\n"
        "### service/render.py\n"
        "```python\n"
        "def render_account(event: dict[str, object]) -> str:\n"
        "    return f\"{event['customer_name']} ({event['status']})\"\n"
        "```\n\n"
        "Run pytest after editing."
    )

    compact, omitted = _strip_redundant_input_file_source_blocks(
        prompt,
        ["service/render.py", "service/tests/test_client.py"],
    )

    assert omitted
    assert "source body omitted" in compact
    assert "customer_name" not in compact
    assert "service/render.py" in compact
    assert "Run pytest" in compact


def test_delegate_source_block_compaction_rewrites_assistant_tool_args():
    from app.llm.client_tools_loop import _compact_delegate_input_file_source_blocks_in_last_assistant

    prompt = (
        "Task: update contracts/customer_event.py.\n\n"
        "### contracts/customer_event.py\n"
        "```python\n"
        "def validate_event(payload: dict[str, object]) -> dict[str, object]:\n"
        "    if \"customer_name\" not in payload:\n"
        "        raise ValueError(\"missing customer_name\")\n"
        "    return {\"customer_name\": payload[\"customer_name\"], \"status\": payload[\"status\"]}\n"
        "```\n"
    )
    args = {
        "action": "spawn",
        "tasks": [{
            "task_id": "migrate",
            "kind": "code",
            "prompt": prompt,
            "input_files": ["contracts/customer_event.py"],
        }],
    }
    msgs = [{
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_delegate",
            "type": "function",
            "function": {
                "name": "delegate",
                "arguments": json.dumps(args, ensure_ascii=False),
            },
        }],
    }]

    changed = _compact_delegate_input_file_source_blocks_in_last_assistant(
        msgs,
        call_id="call_delegate",
        args=args,
    )

    assert changed is True
    compact_args = json.loads(msgs[0]["tool_calls"][0]["function"]["arguments"])
    compact_prompt = compact_args["tasks"][0]["prompt"]
    assert "source body omitted" in compact_prompt
    assert "missing customer_name" not in compact_prompt
    assert compact_args["tasks"][0]["_omitted_source_blocks"]


@pytest.mark.asyncio
async def test_delegate_sanitize_omits_input_file_source_blocks_before_helper_prompt(tmp_path):
    from app.llm.tools.delegate import _sanitize_and_validate_tasks

    prompt = (
        "Task: update contracts/customer_event.py.\n\n"
        "### contracts/customer_event.py\n"
        "```python\n"
        "def validate_event(payload: dict[str, object]) -> dict[str, object]:\n"
        "    if \"customer_name\" not in payload:\n"
        "        raise ValueError(\"missing customer_name\")\n"
        "    return {\"customer_name\": payload[\"customer_name\"], \"status\": payload[\"status\"]}\n"
        "```\n"
        "Run pytest after editing."
    )

    cleaned = await _sanitize_and_validate_tasks(
        {
            "tasks": [{
                "task_id": "migrate",
                "kind": "code",
                "prompt": prompt,
                "input_files": ["contracts/customer_event.py"],
                "expected_outputs": ["contracts/customer_event.py"],
            }],
        },
        main_workspace=str(tmp_path),
        archive_id="arch_test",
        group_id="group_test",
        user_id="user_test",
    )

    assert isinstance(cleaned, list)
    compact_prompt = cleaned[0]["prompt"]
    assert "source body omitted" in compact_prompt
    assert "missing customer_name" not in compact_prompt
    assert "Run pytest" in compact_prompt


def test_unresolved_project_write_block_helpers_parse_workspace_redirect():
    from app.llm.client_tools_loop import (
        _resolved_project_write_path_from_result,
        _unresolved_project_write_block_from_result,
    )

    blocked = _unresolved_project_write_block_from_result(
        "workspace",
        {
            "ok": False,
            "blocked_reason": "environment_workspace_write_not_project_file",
            "blocked_path": "explainer.md",
            "project_file_created": False,
            "recovery_facts": {
                "matching_tool_shape": "env_apply_create",
                "arguments": {"path": "explainer.md", "content": "body"},
            },
        },
    )

    assert blocked is not None
    assert blocked["path"] == "explainer.md"
    assert blocked["project_file_created"] is False
    assert blocked["matching_tool_shape"] == "env_apply_create"
    assert blocked["observed_recovery_shape"]["tool"] == "env_apply_create"
    assert blocked["suggested_tool"] == "env_apply_create"
    assert "did not create project file" in blocked["fact"]
    assert _unresolved_project_write_block_from_result("env_run", blocked) is None
    assert _resolved_project_write_path_from_result(
        "env_apply_create",
        {"ok": True, "action": "env_apply_create", "path": "explainer.md"},
    ) == "explainer.md"
    assert _resolved_project_write_path_from_result(
        "env_apply_create",
        {"ok": False, "path": "explainer.md"},
    ) is None


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


def test_readonly_helper_text_evidence_without_internal_file_is_not_forced_retry():
    from app.llm.client_tools_loop import _retryable_delegate_facts_from_result

    payload = {
        "ok": True,
        "task_ok": False,
        "results": [{
            "task_id": "read-prompts",
            "kind": "read",
            "ok": False,
            "terminal_reason": "outputs_missing",
            "report": (
                "context.py lines 100-180 define stable prefix splitting; "
                "round_prompts.py lines 120-190 define Round2 JSON contract; "
                "toolchain_cache.py lines 350-420 define schema expansion. "
            ) * 3,
            "outputs_check": {
                "outputs_complete": False,
                "outputs_missing": ["_helpers_shared/read_prompts_evidence.txt"],
            },
        }],
    }

    assert _retryable_delegate_facts_from_result(json.dumps(payload, ensure_ascii=False)) == []


def test_readonly_helper_abort_extract_still_needs_recovery():
    from app.llm.client_tools_loop import _retryable_delegate_facts_from_result

    payload = {
        "ok": True,
        "task_ok": False,
        "results": [{
            "task_id": "read-prompts",
            "kind": "read",
            "ok": False,
            "terminal_reason": "outputs_missing",
            "report": (
                "[ABORT_EXTRACT v1] This report was assembled by the tool layer; "
                "the helper did not complete an LLM final summary. "
                "## Recent Reasoning Excerpt\nLet me now read the next section. "
            ) * 3,
            "outputs_check": {
                "outputs_complete": False,
                "outputs_missing": ["_helpers_shared/read_prompts_evidence.txt"],
            },
        }],
    }

    facts = _retryable_delegate_facts_from_result(json.dumps(payload, ensure_ascii=False))
    assert len(facts) == 1
    assert facts[0]["task_id"] == "read-prompts"


def test_p130_read_no_evidence_loop_is_not_auto_resumed_as_read_retry():
    from app.llm.client_tools_loop import (
        _delegate_gap_facts_from_result,
        _retryable_delegate_facts_from_result,
    )

    reason = (
        "P130 read-helper no-evidence loop: 28 read-class tool calls without writing "
        "any .txt/.md evidence file. The main process should preserve the report, "
        "route the inputs to the correct consumer kind, and avoid spawning another read helper."
    )
    payload = {
        "ok": True,
        "task_ok": False,
        "results": [{
            "task_id": "read-tool-schemas",
            "kind": "read",
            "ok": False,
            "terminal_reason": "stuck",
            "stuck": True,
            "stuck_reason": reason,
            "report": reason,
            "outputs_check": {
                "outputs_complete": False,
                "outputs_missing": ["_evidence_tool_schemas.txt"],
            },
        }],
    }

    assert _retryable_delegate_facts_from_result(json.dumps(payload, ensure_ascii=False)) == []
    gap_facts = _delegate_gap_facts_from_result(json.dumps(payload, ensure_ascii=False))
    assert len(gap_facts) == 1
    assert gap_facts[0]["task_id"] == "read-tool-schemas"
    assert gap_facts[0]["gap_kind"] == "read_no_evidence_loop"


def test_round3_visible_protocol_guard_detects_internal_read_markup():
    from app.core.orchestrator_entry import _looks_like_user_visible_protocol_text

    assert _looks_like_user_visible_protocol_text(
        '好的，我先读取文件：<read file="app/core/context.py" start="1" end="80">'
    )
    assert _looks_like_user_visible_protocol_text(
        '<env_read path="app/core/context.py" start_line="1" end_line="80" />'
    )
    assert not _looks_like_user_visible_protocol_text("我会基于已读取的证据总结优化点。")


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
    assert "processing blocked" in summary
    assert "resource_required=1" in summary


def test_delegate_running_snapshot_is_not_workflow_error_status():
    from app.llm.client_tools_loop import _workflow_event_status_for_tool_result
    from app.llm.message_utils import _tool_result_signal

    payload = {
        "ok": True,
        "action": "spawn",
        "task_ok": False,
        "helpers_completed": 0,
        "helpers_still_running": 1,
        "incomplete_count": 0,
        "_task_status": "incomplete",
        "still_running": [{"task_id": "paper_edit"}],
    }
    result = json.dumps(payload, ensure_ascii=False)

    loop_ok, summary = _tool_result_signal(result)
    event_status, event_ok = _workflow_event_status_for_tool_result("delegate", result, "error")

    assert loop_ok is False
    assert "processing blocked" in summary
    assert event_status == "running"
    assert event_ok is True


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
    assert not any(
        "unresolved_helper_facts_before_final" in (m.get("content") or "")
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
    assert not any(
        "unresolved_helper_facts_before_final" in (m.get("content") or "")
        for m in msgs
        if isinstance(m, dict)
    )


@pytest.mark.asyncio
async def test_retryable_delegate_allows_model_decision_after_checkpoint_limit(monkeypatch):
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

    assert fake_comp.calls == 5
    assert "带缺口收尾" in content
    assert "读取完成后收尾" not in content
    assert not any(
        "unresolved_helper_facts_before_final" in (m.get("content") or "")
        or "unresolved_helper_facts_still_visible" in (m.get("content") or "")
        for m in msgs
        if isinstance(m, dict)
    )
    forced_retry_calls = [
        kwargs.get("tool_choice")
        for kwargs in fake_comp.kwargs_by_call
        if kwargs.get("tool_choice")
    ]
    assert not forced_retry_calls


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
        "unresolved_helper_facts_before_final" in (m.get("content") or "")
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


def test_pending_retry_commit_clears_stuck_helper_without_explicit_missing_outputs():
    from app.llm.client_tools_loop import _pending_retry_tasks_blocking_finalize

    facts = [{
        "task_id": "fix-form",
        "terminal_reason": "stuck",
        "outputs_missing": [],
        "outputs_complete": False,
    }]

    assert _pending_retry_tasks_blocking_finalize(
        ["fix-form"],
        facts,
        ["app.js"],
    ) == []
    assert _pending_retry_tasks_blocking_finalize(
        ["fix-form"],
        facts,
        [],
    ) == ["fix-form"]


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


def test_delegate_workflow_summary_preserves_tts_voice_candidate_lane():
    from app.llm.client_tools_loop import _delegate_workflow_result_summary

    summary = _delegate_workflow_result_summary({
        "task_ok": True,
        "helpers_completed": 1,
        "helpers_still_running": 0,
        "success_count": 1,
        "results": [{
            "task_id": "voice_reply",
            "kind": "tts",
            "ok": True,
            "terminal_reason": "completed",
            "voice_reply_file_candidate": "reply.wav",
            "deliverable_candidate": "reply.wav",
            "delivery_guidance": "use voice_reply_file when final spoken reply",
            "outputs_check": {
                "outputs_complete": True,
                "producer_self_verified": True,
            },
            "files": ["reply.wav"],
        }],
    })

    item = summary["result_items"][0]
    assert item["kind"] == "tts"
    assert item["voice_reply_file_candidate"] == "reply.wav"
    assert item["deliverable_candidate"] == "reply.wav"
    assert item["delivery_guidance"] == "use voice_reply_file when final spoken reply"


def test_publish_main_tool_event_preserves_project_mutation_summary(monkeypatch):
    from app.llm import client_tools_loop

    published = []

    def fake_publish(payload):
        published.append(payload)

    monkeypatch.setattr("app.core.environment_events.publish_workflow_event", fake_publish)
    monkeypatch.setattr("app.core.debug.current_trace_id", lambda: "trace")

    result = json.dumps({
        "ok": True,
        "action": "env_run",
        "stdout": "x" * 2000,
        "project_mutation_fact": {
            "kind": "env_run_project_mutation_fact",
            "created_project_files": ["result.csv"],
            "modified_project_files": [],
        },
        "project_mutations": {
            "created": ["result.csv"],
            "modified": [],
        },
    }, ensure_ascii=False)

    client_tools_loop._publish_main_tool_event(
        "main_tool_done",
        tool="env_run",
        iteration=3,
        result=result,
    )

    assert published
    payload = published[0]
    assert "project_mutation_fact" not in payload["result_preview"]
    assert payload["result_summary"]["project_mutation_fact"]["created_project_files"] == ["result.csv"]
    assert payload["result_summary"]["project_mutations"]["created"] == ["result.csv"]


def test_main_final_contract_snapshot_requires_response_plan_json():
    from pathlib import Path

    src = Path("app/llm/client_tools_loop.py").read_text(encoding="utf-8")

    assert "[SYSTEM_HINT/main_final_contract_snapshot]" in src
    assert "return only one valid ResponsePlan JSON object" in src
    assert "Do not output prose" in src
    assert "intent, key_points" in src
    assert "不输出散文" in src


def test_delegate_completion_checkpoint_extracts_clean_helper_evidence_only():
    from app.llm.client_tools_loop import _delegate_completion_checkpoint_from_result

    result = json.dumps({
        "results": [
            {
                "task_id": "assemble",
                "ok": True,
                "terminal_reason": "completed",
                "main_available_files": ["db_index_paper.docx"],
                "report": (
                    "## Output files\n```json\n{\"files\":[\"db_index_paper.docx\"]}\n```\n"
                    "## Verification recommendation\n`recommend: no, reason: checked`"
                ),
                "outputs_check": {
                    "outputs_complete": True,
                    "delivered_count": 1,
                    "quality_warnings": [],
                },
            },
            {
                "task_id": "stale",
                "ok": False,
                "terminal_reason": "outputs_missing",
                "main_available_files": ["missing.docx"],
                "outputs_check": {
                    "outputs_complete": False,
                    "outputs_missing": ["missing.docx"],
                    "quality_warnings": [{"issue": "document_too_small"}],
                },
            },
        ],
        "helpers_still_running": 0,
    }, ensure_ascii=False)

    checkpoint = _delegate_completion_checkpoint_from_result(result)

    assert checkpoint is not None
    assert checkpoint["files"] == ["db_index_paper.docx"]
    assert "outputs_complete=true" in checkpoint["facts"]
    assert "available evidence includes recommend: no" in checkpoint["facts"]
    assert "available evidence includes Output files" in checkpoint["facts"]
    assert checkpoint["warning_count"] == 0


def test_delegate_completion_checkpoint_surfaces_quality_warning_facts():
    from app.llm.client_tools_loop import _delegate_completion_checkpoint_from_result

    result = json.dumps({
        "results": [
            {
                "task_id": "assemble",
                "ok": True,
                "terminal_reason": "completed",
                "main_available_files": ["db_index_paper.docx"],
                "outputs_check": {
                    "outputs_complete": True,
                    "delivered_count": 1,
                    "quality_warnings": [
                        {
                            "issue": "docx_required_tables_short",
                            "file": "db_index_paper.docx",
                            "details": "DOCX has 2 tables; acceptance mentions at least 4.",
                        }
                    ],
                },
            },
        ],
    }, ensure_ascii=False)

    checkpoint = _delegate_completion_checkpoint_from_result(result)

    assert checkpoint is not None
    assert checkpoint["warning_count"] == 1
    assert any("docx_required_tables_short" in fact for fact in checkpoint["facts"])
    assert any("at least 4" in fact for fact in checkpoint["facts"])


def test_main_helper_completion_checkpoint_includes_contract_snapshot():
    from app.llm.tools.runtime_hints import main_helper_completion_checkpoint

    hint = main_helper_completion_checkpoint(
        iteration=12,
        files=["db_index_paper.docx"],
        facts=["outputs_complete=true"],
        warning_count=1,
        contract_snapshot=(
            "[structured task contracts]\n"
            '{"acceptance":["At least 4 comparative tables are present"]}'
        ),
    )

    assert "Current task contract snapshot" in hint
    assert "At least 4 comparative tables" in hint
    assert "quality warning" in hint


def test_task_focus_refresh_reminds_audit_counts_are_not_quotas():
    from app.llm.client_tools_loop import _task_focus_refresh_hint

    hint = _task_focus_refresh_hint(
        iteration=12,
        task_id=None,
        helper_kind=None,
        chars_total=120_000,
    )

    assert "requested counts are ceilings, not quotas" in hint
    assert "label weak leads with the missing direct evidence" in hint


def test_main_loop_convergence_thresholds_and_audit_review_prompt_are_bounded():
    import inspect
    from app.llm import client_tools_loop

    src = inspect.getsource(client_tools_loop.chat_with_tools_loop)

    assert "_MAIN_ITER_MILESTONE_WARN = 36" in src
    assert "_MAIN_ITER_FINALIZE_WARN = 60" in src
    assert "_MAIN_ITER_HARD_CAP = 120" in src
    assert "do not " in src
    assert "start broad additional exploration" in src
    assert "one narrow missing fact" in src


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


def test_final_plan_content_by_reference_detected_as_self_assessment():
    """Arena judge trial 3: a structurally valid plan whose key_points only
    point at earlier tool-loop output ("The final JSON was already produced
    above") loses the content entirely — earlier assistant text never reaches
    the reply stage. Such plans must trigger the self-assessment retry."""
    import json as _json
    from app.llm.client_tools_loop import _looks_like_final_plan_self_assessment

    by_reference = _json.dumps({
        "intent": "Describe the self-contained nature of the judging task",
        "key_points": [
            "The judging task is fully self-contained.",
            "No project files exist to inspect.",
            "The final JSON was already produced above with all scores, rationales, and comparative ordering.",
        ],
        "tone": "neutral",
        "length_hint": "short",
    })
    assert _looks_like_final_plan_self_assessment(by_reference) is True

    # A plan that CARRIES the structured content (long intact string) is fine
    # even if it also mentions "above".
    with_substance = _json.dumps({
        "intent": "Return the scoring JSON",
        "key_points": [
            "Scores produced above and reproduced here in full: "
            + _json.dumps({"scores": [{"answer_id": f"answer_{c}", "criteria_scores": {"correctness": 8, "completeness": 7, "clarity": 9}, "rationale": "solid fix with tests"} for c in "ABCDEF"], "confidence": 0.8}),
        ],
        "tone": "neutral",
        "length_hint": "short",
    })
    assert _looks_like_final_plan_self_assessment(with_substance) is False

    # Ordinary completion plans without above-references stay untouched.
    normal = _json.dumps({
        "intent": "Migration complete",
        "key_points": ["2 files patched", "14 tests pass"],
        "tone": "rigorous-controlled",
        "length_hint": "short",
    })
    assert _looks_like_final_plan_self_assessment(normal) is False


def test_final_plan_content_by_reference_detected_as_self_assessment():
    """Arena judge trial 3: round2 emitted the scoring JSON in an earlier
    tool-loop turn, then the final plan's key_points only said "The final JSON
    was already produced above". Earlier assistant text never reaches the reply
    stage, so such a plan loses the content entirely. The self-assessment
    detector must catch the content-by-reference shape and re-prompt."""
    import json as _json
    from app.llm.client_tools_loop import _looks_like_final_plan_self_assessment

    by_reference = _json.dumps({
        "intent": "Describe the self-contained judging task",
        "key_points": [
            "The judging task is fully self-contained.",
            "No project files or external evidence exist to inspect.",
            "The final JSON was already produced above with all scores and ordering.",
        ],
        "tone": "neutral",
        "length_hint": "short",
    })
    assert _looks_like_final_plan_self_assessment(by_reference) is True

    # A plan that actually CARRIES the structured content stays valid.
    real_content = _json.dumps({
        "intent": "Return the comparative judgement JSON",
        "key_points": [
            '{"scores": [' + ", ".join(
                '{"answer_id": "answer_%s", "criteria_scores": {"correctness": %d, "completeness": %d, "clarity": %d}, "rationale": "detailed reasoning about the fix approach and verification quality for this answer"}' % (c, i, i, i)
                for i, c in enumerate("ABCDEF")
            ) + '], "confidence": 0.85, "comparative_rationale": "A and F fixed the real bug"}',
        ],
        "tone": "neutral",
        "length_hint": "long",
    })
    assert _looks_like_final_plan_self_assessment(real_content) is False

    # An ordinary completion plan without above-references stays valid.
    normal = _json.dumps({
        "intent": "Report migration done",
        "key_points": ["3 files changed", "tests pass"],
        "tone": "neutral",
        "length_hint": "short",
    })
    assert _looks_like_final_plan_self_assessment(normal) is False


def test_round3_visible_protocol_guard_detects_internal_helper_structure():
    from app.core.orchestrator_entry import _looks_like_user_visible_protocol_text

    assert _looks_like_user_visible_protocol_text(
        "The read helper report says the page content was fetched."
    )
    assert _looks_like_user_visible_protocol_text(
        "I copied results from _helpers_shared/fetch_page/page.txt."
    )
    assert _looks_like_user_visible_protocol_text(
        "helpers_still_running=0 and helpers_completed=1."
    )
    assert _looks_like_user_visible_protocol_text(
        "The work unit reported the page content was fetched."
    )
    assert _looks_like_user_visible_protocol_text(
        "The processing record reported the page content was fetched."
    )
    assert _looks_like_user_visible_protocol_text(
        "Background work reported the file was generated."
    )
    assert _looks_like_user_visible_protocol_text(
        "Available evidence reported the file is ready."
    )
    assert _looks_like_user_visible_protocol_text(
        "The producer evidence says the file is ready."
    )
    assert _looks_like_user_visible_protocol_text(
        "The helper task contract is complete."
    )
    assert _looks_like_user_visible_protocol_text(
        "The helper reported the file was generated successfully."
    )
    assert _looks_like_user_visible_protocol_text(
        "This exposed the helper existence to the user."
    )
    assert _looks_like_user_visible_protocol_text(
        "An internal helper exists for this task."
    )
    assert _looks_like_user_visible_protocol_text(
        "helper 已经完成并返回了文件。"
    )
    assert _looks_like_user_visible_protocol_text(
        "这句话暴露了 helper 的存在情况。"
    )
    assert _looks_like_user_visible_protocol_text(
        "内部 helper 在跑，等它完成后我再回复。"
    )
    assert _looks_like_user_visible_protocol_text(
        "后台工具链已经把网页内容抓到了。"
    )
    assert _looks_like_user_visible_protocol_text(
        "内部工具调用返回了页面内容。"
    )
    assert _looks_like_user_visible_protocol_text(
        "I delegated to a helper to inspect the page."
    )
    assert _looks_like_user_visible_protocol_text(
        "选好了我马上让helper开工喵！"
    )
    assert _looks_like_user_visible_protocol_text(
        "后台任务已经完成并返回了文件。"
    )
    assert _looks_like_user_visible_protocol_text(
        "A background task reported the file was generated."
    )
    assert _looks_like_user_visible_protocol_text(
        "Background producers returned the requested evidence."
    )
    assert _looks_like_user_visible_protocol_text(
        "The producer-owned artifact is ready."
    )
    assert _looks_like_user_visible_protocol_text(
        "The producing helper handled validation."
    )
    assert _looks_like_user_visible_protocol_text(
        "Producer helpers returned the requested files."
    )
    assert _looks_like_user_visible_protocol_text(
        "我调用了 helper 来检查页面。"
    )
    assert not _looks_like_user_visible_protocol_text(
        "I used the available evidence to summarize the page."
    )
    assert not _looks_like_user_visible_protocol_text(
        "Ask a human helper to review this later."
    )


def test_round3_late_protocol_guard_uses_rolling_tail():
    from pathlib import Path
    from app.core.orchestrator_entry import _looks_like_user_visible_protocol_text

    assert not _looks_like_user_visible_protocol_text("The read hel")
    assert _looks_like_user_visible_protocol_text("The read helper report says")

    src = Path("app/core/orchestrator_entry.py").read_text(encoding="utf-8")
    assert "_round3_late_protocol_tail + tok" in src
    assert "final_text_parts.pop()" in src


def test_round3_protocol_fallback_sanitizes_internal_helper_terms():
    from app.core.orchestrator_entry import _plan_fallback_user_reply
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="待用户确认方向后派 helper 生成材料",
        key_points=[
            "helper report says the document can be generated",
            "background task writer failed and producer-owned output was partial",
            "I copied results from _helpers_shared/fetch_page/page.txt.",
            "helpers_still_running=0; the processing record reported success from available evidence.",
            "Producer helpers returned a draft.",
            "This exposed the helper existence to the user.",
            "内部 helper 在跑，后台工具链返回了草稿。",
            "这句话暴露了 helper 的存在情况。",
        ],
        tone="plain",
        length_hint="short",
        deliverables=[".helper_fetch_full_report.txt", "final_report.docx"],
    )

    text = _plan_fallback_user_reply(plan)

    assert "helper" not in text.lower()
    assert "helpers_still_running" not in text
    assert "work unit" not in text.lower()
    assert "processing record" not in text.lower()
    assert "background task" not in text.lower()
    assert "background work" not in text.lower()
    assert "producer-owned" not in text.lower()
    assert "producer evidence" not in text.lower()
    assert "producer helper" not in text.lower()
    assert "producer helpers" not in text.lower()
    assert "helper existence" not in text.lower()
    assert "internal helper" not in text.lower()
    assert "producer" not in text.lower()
    assert "available evidence" not in text.lower()
    assert "内部 helper" not in text
    assert "helper 的存在情况" not in text
    assert "后台工具链" not in text
    assert "内部工具调用" not in text
    assert "_helpers_shared" not in text
    assert ".helper_" not in text
    assert "final_report.docx" in text


def test_round3_gap_fact_helpers_are_neutralized_before_final_plan():
    from app.llm.client_tools_loop import _neutral_round3_gap_text, _round3_gap_missing_items

    text = _neutral_round3_gap_text(
        "helper_gap from background work: producer-owned helper report in _helpers_shared/fetch/page.txt"
    )

    lowered = text.lower()
    for forbidden in ("helper", "background work", "producer-owned", "_helpers_shared"):
        assert forbidden not in lowered
    assert "processing" in lowered

    missing = _round3_gap_missing_items([
        "_helpers_shared/fetch/page.txt",
        ".helper_fetch_full_report.txt",
        "final_report.docx",
    ])

    assert missing == ["final_report.docx"]
