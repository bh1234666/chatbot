"""特征测试:orchestrator_checks.py 抽取(2026-05-20)。

orchestrator.py 的 macro 升级信号 / deliverable candidate facts / sibling 文件检查 helpers
抽到 orchestrator_checks.py。本测试用纯 AST 校验(离线无依赖):
  1. orchestrator_checks 定义了预期的 13 个符号;
  2. orchestrator.py 通过 re-export 仍暴露这 13 个符号(import-before-use)。
不依赖第三方库,可离线运行。
"""
import ast
import json
import os

ROOT = os.path.join(os.path.dirname(__file__), "..", "app", "core")

EXPECTED = {
    "_MACRO_YELLOW_ITER_THRESHOLD", "_MACRO_HARD_ITER_THRESHOLD",
    "_MACRO_BATCH_TIMEOUT_KEYWORDS", "_check_macro_escalation_signals",
    "_has_workspace_files_produced", "_AUTOFIX_DELIVERY_EXTS",
    "_AUTOFIX_SKIP_PATTERNS", "_AUTOFIX_INTERMEDIATE_SCRIPT_PREFIXES",
    "_AUTOFIX_PRODUCTION_HINTS", "_AUTOFIX_FILE_INTENT_KEYWORDS",
    "_AUTOFIX_FINAL_DELIVERABLE_EXTS", "_check_sibling_files",
    "_collect_deliverable_candidates", "_collect_voice_reply_file_candidates",
}


def _top_level_names(path):
    t = ast.parse(open(path, encoding="utf-8").read())
    names = set()
    for n in t.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            for tg in n.targets:
                if isinstance(tg, ast.Name):
                    names.add(tg.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
    return names, t


def test_checks_module_defines_expected_symbols():
    names, _ = _top_level_names(os.path.join(ROOT, "orchestrator_checks.py"))
    missing = EXPECTED - names
    assert not missing, f"orchestrator_checks 缺少: {missing}"


def test_orchestrator_reexports_all_symbols():
    path = os.path.join(ROOT, "orchestrator.py")
    t = ast.parse(open(path, encoding="utf-8").read())
    reexported = set()
    for n in ast.walk(t):
        if isinstance(n, ast.ImportFrom) and n.module == "app.core.orchestrator_checks":
            reexported |= {a.name for a in n.names}
    missing = EXPECTED - reexported
    assert not missing, f"orchestrator.py 未 re-export: {missing}"


def test_reexport_before_first_use():
    """re-export import 必须出现在任何符号被引用之前(否则运行时 NameError)。"""
    path = os.path.join(ROOT, "orchestrator.py")
    lines = open(path, encoding="utf-8").read().split("\n")
    rex_line = next(
        (i for i, l in enumerate(lines, 1)
         if "from app.core.orchestrator_checks import" in l),
        None,
    )
    assert rex_line, "未找到 orchestrator_checks 的 re-export import"
    import re
    for i, l in enumerate(lines, 1):
        if i >= rex_line:
            break
        for s in EXPECTED:
            assert not re.search(r"\b" + re.escape(s) + r"\b", l), \
                f"符号 {s} 在 re-export(L{rex_line})之前被引用(L{i})"


def _plan(**kwargs):
    from app.schemas.api import ResponsePlan

    data = {
        "intent": "test",
        "key_points": [],
        "tone": "natural",
        "length_hint": "short",
    }
    data.update(kwargs)
    return ResponsePlan(**data)


def test_delivery_candidates_require_current_production_evidence_for_empty_plan(tmp_path):
    from app.core.orchestrator_checks import _collect_deliverable_candidates

    (tmp_path / "foreign_voice.wav").write_bytes(b"RIFFforeign")
    plan = _plan(deliverables=[])

    candidates = _collect_deliverable_candidates(
        plan,
        user_message="看看这个网页内容",
        needs_tools=True,
        workspace_dir=str(tmp_path),
        files_before=set(),
        current_tool_messages=[
            {"role": "tool", "content": json.dumps({"ok": False, "action": "delegate"})},
        ],
        require_current_evidence=True,
    )

    assert candidates == []
    assert plan.deliverables == []


def test_delivery_candidates_collect_current_tool_produced_candidate_without_mutating(tmp_path):
    from app.core.orchestrator_checks import _collect_deliverable_candidates

    (tmp_path / "final_report.docx").write_bytes(b"PK\x03\x04doc")
    plan = _plan(deliverables=[])

    candidates = _collect_deliverable_candidates(
        plan,
        user_message="生成报告",
        needs_tools=True,
        workspace_dir=str(tmp_path),
        files_before=set(),
        current_tool_messages=[
            {
                "role": "tool",
                "content": json.dumps(
                    {"ok": True, "action": "write", "path": "final_report.docx"},
                    ensure_ascii=False,
                ),
            },
        ],
        require_current_evidence=True,
    )

    assert candidates == ["final_report.docx"]
    assert plan.deliverables == []


def test_delivery_candidates_do_not_promote_files_for_non_artifact_request(tmp_path):
    from app.core.orchestrator_checks import _collect_deliverable_candidates

    (tmp_path / "page_inspection_audio.wav").write_bytes(b"RIFF" + b"\0" * 128)
    (tmp_path / "final_page_report.docx").write_bytes(b"PK\x03\x04doc")
    plan = _plan(deliverables=[])

    candidates = _collect_deliverable_candidates(
        plan,
        user_message="查看这个网页并告诉我内容",
        needs_tools=True,
        workspace_dir=str(tmp_path),
        files_before=set(),
        current_tool_messages=[
            {
                "role": "tool",
                "content": json.dumps(
                    {"ok": True, "action": "tts", "path": "page_inspection_audio.wav"},
                    ensure_ascii=False,
                ),
            },
            {
                "role": "tool",
                "content": json.dumps(
                    {"ok": True, "action": "write", "path": "final_page_report.docx"},
                    ensure_ascii=False,
                ),
            },
        ],
        require_current_evidence=True,
    )

    assert candidates == []
    assert plan.deliverables == []


def test_voice_reply_candidates_collect_current_tts_tool_and_helper_results():
    from app.core.orchestrator_checks import _collect_voice_reply_file_candidates

    candidates = _collect_voice_reply_file_candidates([
        {
            "role": "tool",
            "content": json.dumps({
                "ok": True,
                "action": "tts",
                "voice_reply_file_candidate": "direct_reply.wav",
                "paths": ["direct_reply.wav"],
            }),
        },
        {
            "role": "tool",
            "content": json.dumps({
                "ok": True,
                "action": "delegate",
                "results": [{
                    "task_id": "voice",
                    "kind": "tts",
                    "ok": True,
                    "terminal_reason": "completed",
                    "voice_reply_file_candidate": "tts_reply.wav",
                    "deliverable_candidate": "tts_reply.wav",
                    "outputs_check": {
                        "outputs_complete": True,
                        "producer_self_verified": True,
                    },
                    "files": ["tts_reply.wav"],
                }],
            }),
        },
    ])

    assert candidates == ["direct_reply.wav", "tts_reply.wav"]


def test_voice_reply_candidates_ignore_failed_or_non_tts_helper_results():
    from app.core.orchestrator_checks import _collect_voice_reply_file_candidates

    candidates = _collect_voice_reply_file_candidates([
        {
            "role": "tool",
            "content": json.dumps({
                "ok": True,
                "action": "delegate",
                "results": [
                    {
                        "task_id": "failed_voice",
                        "kind": "tts",
                        "ok": False,
                        "terminal_reason": "failed",
                        "voice_reply_file_candidate": "failed_reply.wav",
                        "outputs_check": {"outputs_complete": False},
                    },
                    {
                        "task_id": "white_noise",
                        "kind": "code",
                        "ok": True,
                        "terminal_reason": "completed",
                        "files": ["white_noise.wav"],
                        "outputs_check": {
                            "outputs_complete": True,
                            "producer_self_verified": True,
                        },
                    },
                ],
            }),
        },
    ])

    assert candidates == []


def test_delivery_candidates_collect_sibling_without_mutating_existing_plan(tmp_path):
    from app.core.orchestrator_checks import _collect_deliverable_candidates

    for name in ("results_quick.csv", "results_merge.csv", "results_insert.csv"):
        (tmp_path / name).write_text("algo,time\nx,1\n", encoding="utf-8")
    plan = _plan(deliverables=["results_quick.csv", "results_merge.csv"])

    candidates = _collect_deliverable_candidates(
        plan,
        user_message="生成排序实验结果",
        needs_tools=True,
        workspace_dir=str(tmp_path),
        files_before=set(),
        current_tool_messages=[
            {
                "role": "tool",
                "content": json.dumps(
                    {"ok": True, "action": "write", "path": "results_insert.csv"},
                    ensure_ascii=False,
                ),
            },
        ],
        require_current_evidence=True,
    )

    assert candidates == ["results_insert.csv"]
    assert plan.deliverables == ["results_quick.csv", "results_merge.csv"]


def test_delivery_candidates_collect_mentioned_without_mutating_existing_plan(tmp_path):
    from app.core.orchestrator_checks import _collect_deliverable_candidates

    (tmp_path / "summary.docx").write_bytes(b"doc")
    (tmp_path / "appendix.pdf").write_bytes(b"pdf")
    plan = _plan(
        key_points=["summary.docx 和 appendix.pdf 都已生成。"],
        deliverables=["summary.docx"],
    )

    candidates = _collect_deliverable_candidates(
        plan,
        user_message="生成材料",
        needs_tools=True,
        workspace_dir=str(tmp_path),
        files_before=set(),
        current_tool_messages=[
            {
                "role": "tool",
                "content": json.dumps(
                    {"ok": True, "action": "write", "path": "appendix.pdf"},
                    ensure_ascii=False,
                ),
            },
        ],
        require_current_evidence=True,
    )

    assert candidates == ["appendix.pdf"]
    assert plan.deliverables == ["summary.docx"]


def test_delivery_candidates_skip_internal_evidence_and_input_dirs(tmp_path):
    from app.core.orchestrator_checks import _collect_deliverable_candidates

    for rel in (
        "_tool_results/1781252902_read_file_content.txt",
        "_scratch/fetch_page.py",
        "_downloaded_media/source.jpg",
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    plan = _plan(deliverables=[])

    candidates = _collect_deliverable_candidates(
        plan,
        user_message="生成文件",
        needs_tools=True,
        workspace_dir=str(tmp_path),
        files_before=set(),
        current_tool_messages=[
            {
                "role": "tool",
                "content": json.dumps(
                    {"ok": True, "action": "write", "path": "_tool_results/1781252902_read_file_content.txt"},
                    ensure_ascii=False,
                ),
            },
            {
                "role": "tool",
                "content": json.dumps(
                    {"ok": True, "action": "write", "path": "_scratch/fetch_page.py"},
                    ensure_ascii=False,
                ),
            },
            {
                "role": "tool",
                "content": json.dumps(
                    {"ok": True, "action": "write", "path": "_downloaded_media/source.jpg"},
                    ensure_ascii=False,
                ),
            },
        ],
        require_current_evidence=True,
    )

    assert candidates == []
    assert plan.deliverables == []


def test_round2_delivery_revision_decides_before_plan_mutation(monkeypatch):
    import asyncio
    from app.core import orchestrator
    from app.core.prompt_cache_observer import compare_prompt_cache_prefix, describe_prompt_cache_input
    import app.llm.model_pool as model_pool

    captured = {}
    original_round2_messages = [
        {"role": "system", "content": "round2 system"},
        {"role": "user", "content": "original task"},
    ]

    async def fake_chat_json(messages, **kwargs):
        captured["messages"] = messages
        return {
            "intent": "报告文件已准备好",
            "key_points": ["报告文件已准备好。"],
            "deliverables": ["final_report.docx"],
        }

    monkeypatch.setattr(orchestrator.llm, "chat_json", fake_chat_json)
    monkeypatch.setattr(model_pool, "resolve_task", lambda _task: object())
    plan = _plan(deliverables=[])

    revised_msgs = asyncio.run(orchestrator._ask_round2_to_revise_plan_for_delivery_candidates(
        plan,
        round2_messages=original_round2_messages,
        model_spec=object(),
        user_message="生成报告",
        candidate_files=["final_report.docx"],
        trace_id="test",
    ))

    assert plan.deliverables == ["final_report.docx"]
    assert revised_msgs is not None
    assert any(
        "Delivery Candidate Facts For Current Round2 Plan" in str(m.get("content") or "")
        for m in captured["messages"]
        if m.get("role") == "user"
    )
    assert revised_msgs[-1]["role"] == "assistant"
    assert captured["messages"][:2] == original_round2_messages
    assert revised_msgs[:2] == original_round2_messages
    assert describe_prompt_cache_input(messages=captured["messages"])["system_static_hash"] == (
        describe_prompt_cache_input(messages=original_round2_messages)["system_static_hash"]
    )
    assert compare_prompt_cache_prefix(
        left_messages=original_round2_messages,
        right_messages=captured["messages"],
    )["common_prefix_ratio"] > 0.99


def test_round2_delivery_revision_can_accept_key_point_candidates(monkeypatch):
    import asyncio
    from app.core import orchestrator
    import app.llm.model_pool as model_pool

    captured = {}
    original_round2_messages = [
        {"role": "system", "content": "round2 system"},
        {"role": "user", "content": "original task"},
    ]

    async def fake_chat_json(messages, **kwargs):
        captured["messages"] = messages
        return {
            "intent": "answer with verified facts",
            "key_points": ["verified runtime fact"],
            "deliverables": [],
        }

    monkeypatch.setattr(orchestrator.llm, "chat_json", fake_chat_json)
    monkeypatch.setattr(model_pool, "resolve_task", lambda _task: object())
    plan = _plan(deliverables=[])

    revised_msgs = asyncio.run(orchestrator._ask_round2_to_revise_plan_for_delivery_candidates(
        plan,
        round2_messages=original_round2_messages,
        model_spec=object(),
        user_message="summarize verified result",
        candidate_files=[],
        candidate_key_points=["verified runtime fact"],
        trace_id="test",
    ))

    assert plan.deliverables == []
    assert plan.key_points == ["verified runtime fact"]
    assert revised_msgs is not None
    assert any(
        "candidate_key_points" in str(m.get("content") or "")
        for m in captured["messages"]
        if m.get("role") == "user"
    )
    assert captured["messages"][:2] == original_round2_messages


def test_round2_delivery_revision_can_accept_voice_reply_candidate(monkeypatch):
    import asyncio
    from app.core import orchestrator
    import app.llm.model_pool as model_pool

    captured = {}
    original_round2_messages = [
        {"role": "system", "content": "round2 system"},
        {"role": "user", "content": "original task"},
    ]

    async def fake_chat_json(messages, **kwargs):
        captured["messages"] = messages
        return {
            "intent": "send the spoken reply",
            "key_points": ["reply audio is ready"],
            "deliverables": ["reply.wav"],
            "voice_reply_file": "reply.wav",
        }

    monkeypatch.setattr(orchestrator.llm, "chat_json", fake_chat_json)
    monkeypatch.setattr(model_pool, "resolve_task", lambda _task: object())
    plan = _plan(deliverables=[])

    revised_msgs = asyncio.run(orchestrator._ask_round2_to_revise_plan_for_delivery_candidates(
        plan,
        round2_messages=original_round2_messages,
        model_spec=object(),
        user_message="用语音回复",
        candidate_files=[],
        candidate_voice_reply_files=["reply.wav"],
        trace_id="test",
    ))

    assert plan.voice_reply_file == "reply.wav"
    assert plan.deliverables == []
    assert revised_msgs is not None
    assert any(
        "candidate_voice_reply_files" in str(m.get("content") or "")
        for m in captured["messages"]
        if m.get("role") == "user"
    )
    assert captured["messages"][:2] == original_round2_messages


def test_round2_delivery_revision_can_clear_preexisting_voice_reply_by_llm(monkeypatch):
    import asyncio
    from app.core import orchestrator
    import app.llm.model_pool as model_pool

    captured = {}
    original_round2_messages = [
        {"role": "system", "content": "round2 system"},
        {"role": "user", "content": "original task"},
    ]

    async def fake_chat_json(messages, **kwargs):
        captured["messages"] = messages
        return {
            "intent": "current task is not the old voice reply",
            "key_points": [],
            "deliverables": [],
            "voice_reply_file": "",
        }

    monkeypatch.setattr(orchestrator.llm, "chat_json", fake_chat_json)
    monkeypatch.setattr(model_pool, "resolve_task", lambda _task: object())
    plan = _plan(deliverables=[], voice_reply_file="old_reply.wav")

    revised_msgs = asyncio.run(orchestrator._ask_round2_to_revise_plan_for_delivery_candidates(
        plan,
        round2_messages=original_round2_messages,
        model_spec=object(),
        user_message="生成一个新的语音回复",
        candidate_files=[],
        voice_reply_review_facts=[{
            "voice_reply_file": "old_reply.wav",
            "preexisting_at_round_start": True,
            "has_current_tts_or_helper_candidate": False,
        }],
        trace_id="test",
    ))

    assert plan.voice_reply_file == ""
    assert revised_msgs is not None
    assert any(
        "voice_reply_review_facts" in str(m.get("content") or "")
        and "preexisting_at_round_start" in str(m.get("content") or "")
        for m in captured["messages"]
        if m.get("role") == "user"
    )
    assert captured["messages"][:2] == original_round2_messages


def test_self_check_key_points_are_candidates_not_plan_mutations(monkeypatch, tmp_path):
    import asyncio
    import json
    from types import SimpleNamespace
    from app.core import orchestrator
    from app.schemas.api import ResponsePlan
    import app.llm.model_pool as model_pool
    import app.llm.client as llm_client

    (tmp_path / "final_report.md").write_text("done", encoding="utf-8")

    class _Message:
        content = json.dumps({
            "missing_deliverables": [],
            "missing_key_points": ["verified fact from self-check"],
        })

    class _Completions:
        async def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=_Message())])

    class _Client:
        chat = SimpleNamespace(completions=_Completions())

    async def _fake_retry(call, **_kwargs):
        return await call()

    spec = SimpleNamespace(model="test-model", provider="test")
    monkeypatch.setattr(model_pool, "resolve_task", lambda _task: spec)
    monkeypatch.setattr(llm_client, "_client_for_spec", lambda _spec: _Client())
    monkeypatch.setattr(llm_client, "_retry", _fake_retry)

    plan = ResponsePlan(
        intent="answer",
        key_points=[],
        tone="plain",
        length_hint="short",
        deliverables=[],
    )

    candidates = asyncio.run(orchestrator._self_check_plan(
        plan,
        str(tmp_path),
        trace_id="test",
    ))

    assert candidates["deliverables"] == []
    assert candidates["key_points"] == ["verified fact from self-check"]
    assert plan.key_points == []
    assert plan.deliverables == []


def test_round2_collects_self_check_candidates_for_delivery_revision():
    src = open(os.path.join(ROOT, "orchestrator.py"), encoding="utf-8").read()
    assert "self_check_candidates = await _self_check_plan(" in src
    assert "self_check_key_point_candidates.extend" in src
    assert "delivery_candidate_files.extend(_collect_deliverable_candidates(" in src
    assert "_collect_voice_reply_file_candidates(" in src
    assert "require_current_evidence=True" in src
    assert "_ask_round2_to_revise_plan_for_delivery_candidates(" in src


def test_round2_delivery_revision_is_visible_to_toolchain_cache():
    src = open(os.path.join(ROOT, "orchestrator.py"), encoding="utf-8").read()
    revision_assign = "if revised_msgs:\n            final_msgs = revised_msgs"
    cache_append = "_toolchain_cache.append_round("

    assert revision_assign in src
    assert cache_append in src
    assert src.index(revision_assign) < src.index(cache_append)


def test_delivery_candidate_helpers_have_no_mutation_switch():
    import inspect
    from app.core import orchestrator
    from app.core.orchestrator_checks import (
        _add_mentioned_existing_deliverables,
        _collect_deliverable_candidates,
        _check_sibling_files,
    )

    assert "apply_missing_deliverables" not in inspect.signature(orchestrator._self_check_plan).parameters
    assert "apply" not in inspect.signature(_collect_deliverable_candidates).parameters
    assert "apply" not in inspect.signature(_check_sibling_files).parameters
    assert "apply" not in inspect.signature(_add_mentioned_existing_deliverables).parameters


if __name__ == "__main__":
    test_checks_module_defines_expected_symbols()
    test_orchestrator_reexports_all_symbols()
    test_reexport_before_first_use()
    print("test_orchestrator_checks: 3 passed")
