from __future__ import annotations

from collections.abc import Iterable
import ast
from pathlib import Path


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


_MOJIBAKE_MARKERS = (
    "\ufffd",
    "銆",
    "锛",
    "绋",
    "鐢",
    "鍙",
    "涓",
    "鏄",
    "杩",
    "鎿",
    "浣",
    "妯",
    "淇",
    "鍏",
    "鍐",
    "璇",
    "瑙",
    "姣",
    "闂",
    "闄",
    "骞",
    "鐨",
    "浜",
    "櫒",
)


def _looks_like_mojibake(text: str) -> bool:
    return any(marker in text for marker in _MOJIBAKE_MARKERS)


def _assert_no_mojibake_or_inline_cjk(name: str, text: str) -> None:
    assert not _looks_like_mojibake(text), f"{name} contains mojibake-like text"


def _first_paragraph(text: str) -> str:
    return (text or "").strip().split("\n\n", 1)[0]


def _first_content_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        return stripped
    return ""


def _iter_descriptions(obj, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(obj, dict):
        desc = obj.get("description")
        if isinstance(desc, str):
            yield f"{path}.description", desc
        for key, value in obj.items():
            yield from _iter_descriptions(value, f"{path}.{key}" if path else str(key))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from _iter_descriptions(value, f"{path}[{index}]")


def _static_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append("{}")
            else:
                return None
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_text(node.left)
        right = _static_text(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _iter_static_system_role_injections() -> Iterable[tuple[str, str]]:
    roots = [Path("app/api"), Path("app/llm"), Path("app/core"), Path("app/memory")]
    for root in roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                has_system_role = False
                content_node: ast.AST | None = None
                for key, value in zip(node.keys, node.values):
                    if _const_str(key) == "role" and _const_str(value) == "system":
                        has_system_role = True
                    elif _const_str(key) == "content":
                        content_node = value
                if not has_system_role or content_node is None:
                    continue
                text = _static_text(content_node)
                if text is not None:
                    yield f"{path}:{getattr(content_node, 'lineno', getattr(node, 'lineno', 0))}", text


def _iter_promptish_constants() -> Iterable[tuple[str, str]]:
    roots = [Path("app/api"), Path("app/llm"), Path("app/core"), Path("app/memory")]
    keywords = ("PROMPT", "SYSTEM", "INSTRUCTION", "DIRECTIVE", "CONTRACT", "RULES", "GUIDANCE", "ADDON")
    for root in roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                targets: list[str] = []
                value: ast.AST | None = None
                if isinstance(node, ast.Assign):
                    value = node.value
                    targets.extend(t.id for t in node.targets if isinstance(t, ast.Name))
                elif isinstance(node, ast.AnnAssign):
                    value = node.value
                    if isinstance(node.target, ast.Name):
                        targets.append(node.target.id)
                if value is None:
                    continue
                for target in targets:
                    if not any(keyword in target.upper() for keyword in keywords):
                        continue
                    text = _static_text(value)
                    if text is not None and len(text.strip()) >= 40:
                        yield f"{path}:{getattr(node, 'lineno', 0)}:{target}", text


def _iter_model_visible_hint_literals() -> Iterable[tuple[str, str]]:
    roots = [Path("app/api"), Path("app/llm"), Path("app/core"), Path("app/memory")]
    markers = (
        "[SYSTEM_",
        "FIX_HINT",
        "next_action_instruction",
        "NEXT_ACTION",
        "usage_hint",
        "recovery",
        "retry",
    )
    for root in roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents: dict[ast.AST, ast.AST] = {}
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[child] = parent

            def _is_docstring_literal(node: ast.AST) -> bool:
                parent = parents.get(node)
                grand = parents.get(parent) if parent is not None else None
                if not isinstance(parent, ast.Expr):
                    return False
                if not isinstance(grand, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    return False
                return bool(grand.body and grand.body[0] is parent)

            for node in ast.walk(tree):
                if _is_docstring_literal(node):
                    continue
                text = _static_text(node)
                if not text or len(text.strip()) < 40:
                    continue
                if any(marker in text for marker in markers):
                    yield f"{path}:{getattr(node, 'lineno', 0)}", text


def _iter_static_model_visible_dict_fields() -> Iterable[tuple[str, str]]:
    roots = [Path("app/llm/tools"), Path("app/core"), Path("app/memory")]
    field_names = {
        "error",
        "message",
        "hint",
        "FIX_HINT",
        "fix_it_hint",
        "batch_hint",
        "delegate_hint",
        "empty_output_warning",
        "environment_project_hint",
        "_next_action_instruction",
        "_next_step_hint",
        "_post_helper_hint",
        "_repeated_command_failure",
        "_rewrite_suggestion",
        "next_action",
        "next_step",
        "next_step_hint",
        "normalization_warning",
        "orphan_image_hint",
        "raw_text_hint",
        "recommended_workflow",
        "_redundant_bash_warning",
        "_redundant_code_index_warning",
        "_redundant_read_warning",
        "_redundant_search_warning",
        "retry_instruction",
        "recovery_hint",
        "repair_recommendation",
        "resource_hint",
        "resume_hint",
        "rewrite_warning",
        "suggested_recovery",
        "suggested_next_actions",
        "_suggested_next_actions",
        "throttle_hint",
        "throttle_warning",
        "truncation_note",
        "usage",
        "_warning",
        "post_helper_usage_hint",
        "next_action_instruction",
        "suggested_next_action",
        "suggested_request",
        "recovery_plan",
    }
    for root in roots:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        key_name = _static_text(key)
                        if key_name not in field_names:
                            continue
                        text = _static_text(value)
                        if text is not None and len(text.strip()) >= 40:
                            yield f"{path}:{getattr(value, 'lineno', getattr(node, 'lineno', 0))}:{key_name}", text
                    continue
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if not isinstance(target, ast.Subscript):
                            continue
                        key_name = _static_text(target.slice)
                        if key_name not in field_names:
                            continue
                        text = _static_text(node.value)
                        if text is not None and len(text.strip()) >= 40:
                            yield f"{path}:{getattr(node.value, 'lineno', getattr(node, 'lineno', 0))}:{key_name}", text


_GUIDANCE_TOOL_RESULT_FIELDS = (
    ":hint",
    ":FIX_HINT",
    ":fix_it_hint",
    ":batch_hint",
    ":delegate_hint",
    ":empty_output_warning",
    ":environment_project_hint",
    ":_next_action_instruction",
    ":_next_step_hint",
    ":_post_helper_hint",
    ":_repeated_command_failure",
    ":_rewrite_suggestion",
    ":next_action",
    ":next_step",
    ":next_step_hint",
    ":normalization_warning",
    ":orphan_image_hint",
    ":raw_text_hint",
    ":recommended_workflow",
    ":_redundant_bash_warning",
    ":_redundant_code_index_warning",
    ":_redundant_read_warning",
    ":_redundant_search_warning",
    ":retry_instruction",
    ":recovery_hint",
    ":repair_recommendation",
    ":resource_hint",
    ":resume_hint",
    ":rewrite_warning",
    ":suggested_recovery",
    ":suggested_next_actions",
    ":_suggested_next_actions",
    ":throttle_hint",
    ":throttle_warning",
    ":truncation_note",
    ":usage",
    ":_warning",
    ":post_helper_usage_hint",
    ":next_action_instruction",
    ":suggested_next_action",
    ":suggested_request",
    ":recovery_plan",
)


def _assert_english_first(name: str, text: str) -> None:
    head = _first_content_line(text) or _first_paragraph(text)
    assert head, f"{name} is empty"
    assert not _contains_cjk(head), f"{name} must start with English model-facing content: {head!r}"
    _assert_no_mojibake_or_inline_cjk(name, text)


def test_runtime_tool_schema_descriptions_are_english_first_with_chinese_summary():
    from app.llm.tools import tool_schemas
    from app.llm.tools.environment import ENVIRONMENT_TOOL_SCHEMAS
    from app.llm.tools.registry import tools_for_runtime_mode

    schemas = [
        (name, value)
        for name, value in vars(tool_schemas).items()
        if name.endswith("_SCHEMA") and isinstance(value, dict)
    ]
    schemas.extend(
        (f"ENVIRONMENT_TOOL_SCHEMAS[{index}]", schema)
        for index, schema in enumerate(ENVIRONMENT_TOOL_SCHEMAS)
    )
    schemas.extend(
        (f"runtime.chat[{index}]", schema)
        for index, schema in enumerate(tools_for_runtime_mode("chat"))
    )
    schemas.extend(
        (f"runtime.environment[{index}]", schema)
        for index, schema in enumerate(tools_for_runtime_mode("environment"))
    )
    assert schemas

    for schema_name, schema in schemas:
        for path, desc in _iter_descriptions(schema, schema_name):
            _assert_english_first(path, desc)
            assert _contains_cjk(desc), f"{path} is missing a concise Chinese summary"


def test_delegate_and_office_guidance_preserves_task_boundaries():
    from app.llm.tools import tool_schemas
    from app.llm.tools.skills import BUNDLED_SKILLS

    delegate_text = str(tool_schemas.DELEGATE_TOOL_SCHEMA)
    office_text = str(tool_schemas.OFFICE_TOOL_SCHEMA)
    skills_text = BUNDLED_SKILLS["office-recipes"]

    assert "same `task_id` with `resume=true`" in delegate_text
    assert "same-deliverable repair" in delegate_text
    assert "Keep the same base kind" in delegate_text
    assert "Create a new `task_id` only when the work boundary is genuinely different" in delegate_text
    assert "task_id_v2" not in delegate_text
    assert "kind=verify" not in delegate_text

    assert "`verify_integrity` is XLSX-only" in office_text
    assert "Valid DOCX block types" in office_text
    assert "plain prose is `paragraph`" in office_text
    assert "bullets are `list`" in office_text
    assert "Table rows must contain at least one non-empty cell" in office_text
    assert "exact non-negative `block_index`" in office_text
    assert "Search inside plain text files" in str(tool_schemas.SEARCH_IN_FILE_SCHEMA)
    assert "office(action='read')" in str(tool_schemas.SEARCH_IN_FILE_SCHEMA)
    assert "binary_or_structured_file_not_readable_as_text" in str(tool_schemas.SEARCH_IN_FILE_SCHEMA)

    assert "Valid block types are" in skills_text
    assert "Use `paragraph` for ordinary prose" in skills_text
    assert "it is XLSX-only" in skills_text


def test_self_check_guidance_and_candidate_boundary(monkeypatch, tmp_path):
    import asyncio
    import json
    from types import SimpleNamespace

    from app.core import orchestrator
    from app.schemas.api import ResponsePlan

    prompt_seen: dict[str, str] = {}

    class _Message:
        content = json.dumps(
            {
                "missing_deliverables": [
                    "create_report_最终报告.docx",
                    "create_report_v2_最终报告.docx",
                    "最终报告.docx",
                ],
                "missing_key_points": [],
            },
            ensure_ascii=False,
        )

    class _Completions:
        async def create(self, **kwargs):
            prompt_seen["system"] = kwargs["messages"][0]["content"]
            prompt_seen["user"] = kwargs["messages"][1]["content"]
            return SimpleNamespace(choices=[SimpleNamespace(message=_Message())])

    class _Client:
        chat = SimpleNamespace(completions=_Completions())

    async def _fake_retry(call, **_kwargs):
        return await call()

    for name in (
        "create_report_最终报告.docx",
        "create_report_v2_最终报告.docx",
        "最终报告.docx",
    ):
        (tmp_path / name).write_text("x", encoding="utf-8")

    spec = SimpleNamespace(model="test-model", provider="test")
    import app.llm.model_pool as model_pool
    import app.llm.client as llm_client

    monkeypatch.setattr(model_pool, "resolve_task", lambda _task: spec)
    monkeypatch.setattr(llm_client, "_client_for_spec", lambda _spec: _Client())
    monkeypatch.setattr(llm_client, "_retry", _fake_retry)

    plan = ResponsePlan(
        intent="deliver report",
        key_points=[],
        tone="plain",
        length_hint="short",
        deliverables=[],
    )
    asyncio.run(orchestrator._self_check_plan(plan, str(tmp_path), trace_id="test"))

    assert plan.deliverables == ["最终报告.docx"]
    prompt = prompt_seen["system"]
    assert "clean final user-facing filename" in prompt
    assert "failed, partial, intermediate, or quality-blocked work" in prompt
    assert "同一产物" in prompt
    assert '"actual_workspace_files":[' in prompt_seen["user"]


def test_delivery_display_name_remap_reads_main_and_temp(tmp_path):
    from app.core.orchestrator_entry import _load_displayed_name_remap_for_delivery
    from app.llm.tools.workspace import update_displayed_name_remap

    main_ws = tmp_path / "main"
    temp_ws = tmp_path / "temp"
    main_ws.mkdir()
    temp_ws.mkdir()

    update_displayed_name_remap(
        str(main_ws),
        {"helper_report_最终报告.docx": "最终报告.docx"},
    )
    update_displayed_name_remap(
        str(temp_ws),
        {"deliverables_trace.zip": "打包文件.zip"},
    )

    remap = _load_displayed_name_remap_for_delivery(str(main_ws), str(temp_ws))
    assert remap["helper_report_最终报告.docx"] == "最终报告.docx"
    assert remap["deliverables_trace.zip"] == "打包文件.zip"


def test_model_visible_prompt_blocks_are_english_first_and_generic():
    from app.core.environment_prompt import ENVIRONMENT_PROMPT_ADDON, environment_round2_system_prompt
    from app.core.orchestrator_prompts import _build_round2_system_prompts
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.memory import cold, kb, warm
    from app.core.dream.tasks.file_searchability import d15_image_index, d16_pdf_deep, d17_office_index
    from app.core.dream.tasks.kb_dag import d21_node_merge, d22_node_split, d23_high_level_abstract, d24_refine, d25_edges
    from app.core.dream.tasks.memory_maintenance import d4_workspace_cleanup
    from app.llm.tools.delegate import _HELPER_CONSISTENCY_CONTRACT, _helper_tool_availability_note, _select_helper_system
    from app.llm.tools.environment import _env_run_fix_hint, _env_run_usage_hint, _list_tree_truncated_note
    from app.llm.tools.helper_kinds import MODEL_VISIBLE_HELPER_KINDS

    texts: list[tuple[str, str]] = [
        ("environment.addon", ENVIRONMENT_PROMPT_ADDON),
        ("memory.warm.user", warm._USER_COMPRESS_SYSTEM),
        ("memory.warm.group", warm._GROUP_COMPRESS_SYSTEM),
        ("memory.cold.compress", cold._COMPRESS_SYSTEM),
        ("memory.cold.avoid", cold._AVOID_MATCH_SYSTEM),
        ("memory.kb.compress", kb._KB_COMPRESS_SYSTEM),
        ("memory.kb.file_index", kb._FILE_INDEX_SYSTEM),
        ("memory.kb.shared_file_index", kb._GROUP_FILE_INDEX_SYSTEM),
        ("dream.d15", d15_image_index._LLM_PROMPT_SYSTEM),
        ("dream.d16", d16_pdf_deep._LLM_PROMPT_SYSTEM),
        ("dream.d17", d17_office_index._LLM_PROMPT),
        ("dream.d21", d21_node_merge._LLM_PROMPT_SYSTEM),
        ("dream.d22", d22_node_split._LLM_PROMPT),
        ("dream.d23", d23_high_level_abstract._LLM_PROMPT_SYSTEM),
        ("dream.d24", d24_refine._D24_REFINE_SYSTEM),
        ("dream.d25", d25_edges._LLM_PROMPT),
        ("dream.d4", d4_workspace_cleanup._LLM_PROMPT),
    ]
    env = EnvironmentContext(
        root_dir="C:/tmp/project",
        archive_id="arch",
        group_id="env_user_u",
        user_id="u",
        project_key="project",
    )
    with runtime_context("environment", env):
        texts.append(("environment.addon.dynamic", ENVIRONMENT_PROMPT_ADDON))
        env_round2 = environment_round2_system_prompt()
        if env_round2 is not None:
            texts.append(("environment.round2", env_round2["content"]))

    for is_coding in (False, True):
        for is_document in (False, True):
            for parallelizable in (False, True):
                for needs_recall in (False, True):
                    for index, message in enumerate(
                        _build_round2_system_prompts(
                            is_coding=is_coding,
                            is_document=is_document,
                            parallelizable=parallelizable,
                            needs_recall=needs_recall,
                        )
                    ):
                        texts.append((f"round2.{is_coding}.{is_document}.{parallelizable}.{needs_recall}.{index}", message["content"]))

    for kind in MODEL_VISIBLE_HELPER_KINDS:
        texts.append((f"helper.availability.{kind}", _helper_tool_availability_note(kind)))
        texts.append((f"helper.{kind}.easy", _select_helper_system(kind, "easy")))
        texts.append((f"helper.{kind}.hard", _select_helper_system(kind, "hard")))
    texts.append(("helper.consistency", _HELPER_CONSISTENCY_CONTRACT))
    texts.append(("env.list_tree.truncated", _list_tree_truncated_note(25)))
    texts.append(("env.run.fix_hint.syntax", _env_run_fix_hint("python -c bad", "", "SyntaxError: bad")))
    texts.append(("env.run.fix_hint.python_code", _env_run_fix_hint("", "", "SyntaxError: bad", python_code_used=True)))
    texts.append(("env.run.usage.both", _env_run_usage_hint("both_command_and_python_code")))
    texts.append(("env.run.usage.missing", _env_run_usage_hint("missing_execution_body")))

    forbidden = (
        "QQ",
        "NapCat",
        "CQ:image",
        "CQ:at",
        "group chat",
        "group members",
        "group activity",
        "\u7fa4\u804a",
        "\u7fa4\u6587\u4ef6",
        "\u7fa4\u91cc",
    )
    for name, text in texts:
        _assert_english_first(name, text)
        for term in forbidden:
            assert term not in text, f"{name} exposes legacy bridge/environment term: {term!r}"


def test_persona_files_are_english_first_with_chinese_summary():
    persona_dir = Path("personas")
    persona_files = sorted(persona_dir.glob("*.md"))
    assert persona_files

    for path in persona_files:
        raw = path.read_text(encoding="utf-8")
        _, _, body = raw.partition("\n---\n")
        body = body or raw
        _assert_english_first(f"persona.{path.name}", body)
        assert "## Chinese Summary" in body or "## 中文概括" in body, path.name


def test_environment_round2_reminder_stays_compact():
    from app.core.environment_prompt import ENVIRONMENT_PROMPT_ADDON, environment_round2_system_prompt
    from app.core.runtime_mode import EnvironmentContext, runtime_context

    env = EnvironmentContext(
        root_dir="C:/tmp/project",
        archive_id="arch",
        group_id="env_user_u",
        user_id="u",
        project_key="project",
    )
    with runtime_context("environment", env):
        message = environment_round2_system_prompt()
    assert message is not None
    content = message["content"]

    assert len(content) < len(ENVIRONMENT_PROMPT_ADDON) * 0.75
    assert content.count("Acceptance Closure") <= 1
    assert content.count("Project file paths are evidence") == 1


def test_dynamic_model_visible_injections_are_english_first(tmp_path):
    from app.core import context
    from app.core.orchestrator_prompts import _inject_dynamic_session_info
    from app.llm.tools.command_risk import analyze_command
    from app.llm.tools.delegate_stuck import StuckDetector
    from app.llm.tools.workspace import _SHARED_READONLY_ERROR_MSG
    from app.llm.tools.workspace_text import _helper_missing_file_fetch_hint, _structured_read_file_rejection
    from app.llm.tools.workspace_utils import _list_helper_workspace_for_prompt

    def _soft_hint(name: str, detector: StuckDetector) -> tuple[str, str]:
        hint = detector.consume_soft_hint()
        assert hint, name
        return name, hint

    edit_verify = StuckDetector("helper_edit")
    for _ in range(3):
        edit_verify.record("edit_file", '{"ok": true}', {"path": "main.c"})

    empty_edit = StuckDetector("helper_empty")
    for _ in range(3):
        empty_edit.record(
            "edit_file",
            '{"ok": true}',
            {"path": "main.py", "old_string": "x = 1", "new_string": "x = 1"},
        )

    no_product = StuckDetector("helper_no_product")
    for _ in range(3):
        no_product.record_batch([("tc", "todo_write", '{"ok": true}', {"todos": []})])

    timeout_hint = StuckDetector("helper_timeout")
    timeout_hint.record("bash", '{"ok": false, "timed_out": true, "error": "timeout"}', {"command": "slow"})

    compile_hint = StuckDetector("helper_compile")
    for _ in range(2):
        compile_hint.record("bash", '{"ok": false, "error": "fatal error: missing.h"}', {"command": "gcc main.c"})

    interface_hint = StuckDetector("helper_interface")
    for _ in range(2):
        interface_hint.record("bash", '{"ok": false, "error": "struct Foo has no member named bar"}', {"command": "gcc main.c"})

    todo_batch = StuckDetector("helper_todo_batch")
    todo_batch.record_batch([
        (f"tc{i}", "todo_write", '{"ok": true}', {"todos": [{"id": str(i), "content": "x"}]})
        for i in range(5)
    ])

    data_schema = StuckDetector("helper_data")
    data_schema.record("read_file", '{"ok": true}', {"path": "data.csv"})
    data_schema.record("python", '{"ok": true}', {"code": "value = row['name']"})

    visual_reuse = StuckDetector("helper_visual")
    visual_reuse.record("fetch_to_temp", '{"ok": true}', {"paths": ["plot_a.png", "plot_b.png"]})
    visual_reuse.record("workspace", '{"ok": true}', {"action": "write", "path": "make_plot.py", "content": "import matplotlib.pyplot as plt"})

    dynamic_texts: list[tuple[str, str]] = [
        (
            "context.warm_user",
            context._format_warm_user_injection(
                [{"id": "w1", "timestamp": "2026-05-30T00:00:00", "headline": "用户提到项目", "tendencies": {"任务": 1.0}}]
            ),
        ),
        (
            "context.recent_shared_messages",
            context._format_recent_group_messages(
                [{"created_at": "2026-05-30T00:00:00", "user_name": "Alice", "content": "hello", "addressed_bot": True}]
            ),
        ),
        (
            "workspace.helper_missing_env",
            _helper_missing_file_fetch_hint(str(tmp_path / "_delegate_read"), "_env/app/main.py"),
        ),
        (
            "workspace.helper_missing_main",
            _helper_missing_file_fetch_hint(str(tmp_path / "_delegate_read"), "input.txt"),
        ),
        (
            "command.blocked_keyword",
            analyze_command("taskkill /pid 1", str(tmp_path), is_main_thread=True).reason,
        ),
        (
            "command.outside_redirect",
            analyze_command("cmd /c echo hi > C:\\outside.txt", str(tmp_path), is_main_thread=True).reason,
        ),
        (
            "workspace.shared_readonly_error",
            _SHARED_READONLY_ERROR_MSG,
        ),
        _soft_hint("stuck.edit_verify", edit_verify),
        _soft_hint("stuck.empty_edit", empty_edit),
        _soft_hint("stuck.no_product", no_product),
        _soft_hint("stuck.timeout", timeout_hint),
        _soft_hint("stuck.compile", compile_hint),
        _soft_hint("stuck.interface", interface_hint),
        _soft_hint("stuck.todo_batch", todo_batch),
        _soft_hint("stuck.data_schema", data_schema),
        _soft_hint("stuck.visual_reuse", visual_reuse),
    ]
    for filename in ("demo.docx", "scan.pdf", "screen.png", "audio.wav", "archive.zip"):
        rejection = _structured_read_file_rejection(filename)
        assert rejection is not None
        dynamic_texts.append((f"workspace.structured_rejection.{filename}", rejection["message"]))

    helper_ws = tmp_path / "_delegate_read"
    helper_ws.mkdir()
    (helper_ws / "input.txt").write_text("hello", encoding="utf-8")
    dynamic_texts.append(("workspace.helper_snapshot", _list_helper_workspace_for_prompt(str(helper_ws))))

    injected_messages = [{"role": "user", "content": "current request"}]
    _inject_dynamic_session_info(
        injected_messages,
        pause_text="Checkpoint: investigated files.",
        profile_text="Preference: concise engineering reports.",
        feedback_text="Previous reply missed file evidence.",
        lang_directive="Reply in Chinese.",
        mode_text="Environment mode with project root.",
    )
    dynamic_texts.append(("orchestrator.dynamic_session", injected_messages[0]["content"]))

    for name, text in dynamic_texts:
        assert text, name
        _assert_english_first(name, text)


def test_static_system_role_injections_are_english_first_and_generic():
    forbidden = (
        "QQ",
        "NapCat",
        "CQ:image",
        "CQ:at",
        "group chat",
        "group members",
        "group activity",
        "\u7fa4\u804a",
        "\u7fa4\u6587\u4ef6",
        "\u7fa4\u91cc",
    )
    injections = list(_iter_static_system_role_injections())
    assert injections
    for name, text in injections:
        _assert_english_first(name, text)
        for term in forbidden:
            assert term not in text, f"{name} exposes legacy bridge/environment term: {term!r}"


def test_promptish_constants_are_english_first():
    constants = list(_iter_promptish_constants())
    assert constants
    for name, text in constants:
        _assert_english_first(name, text)


def test_model_visible_hint_literals_are_english_first():
    hints = list(_iter_model_visible_hint_literals())
    assert hints
    for name, text in hints:
        _assert_english_first(name, text)


def test_static_model_visible_tool_result_fields_are_english_first():
    fields = list(_iter_static_model_visible_dict_fields())
    assert fields
    forbidden = (
        "QQ",
        "NapCat",
        "CQ:image",
        "CQ:at",
        "group chat",
        "group members",
        "group activity",
        "\u7fa4\u804a",
        "\u7fa4\u6587\u4ef6",
        "\u7fa4\u91cc",
    )
    for name, text in fields:
        _assert_english_first(name, text)
        if name.endswith(_GUIDANCE_TOOL_RESULT_FIELDS):
            assert _contains_cjk(text), f"{name} is missing a concise Chinese summary"
        for term in forbidden:
            assert term not in text, f"{name} exposes legacy bridge/environment term: {term!r}"


def test_helper_convergence_hints_are_english_first_and_actionable():
    from app.llm.tools.runtime_hints import (
        helper_completed_todos_handoff,
        helper_finalize_window,
        helper_iter_checkpoint,
        helper_read_to_write_checkpoint,
        helper_repeated_tool_call_bloat_checkpoint,
        helper_tool_call_bloat_checkpoint,
    )

    hints = [
        ("helper.iter", helper_iter_checkpoint(50, 140, "read")),
        ("helper.finalize", helper_finalize_window(90)),
        ("helper.completed_todos", helper_completed_todos_handoff(iteration=12, helper_kind="code", completed=6)),
        (
            "helper.read_to_write",
            helper_read_to_write_checkpoint(iteration=8, helper_kind="edit", recent_reads=6),
        ),
        (
            "helper.bloat",
            helper_tool_call_bloat_checkpoint(
                iteration=12,
                helper_kind="read",
                tool_name="workspace",
                arg_chars=32000,
            ),
        ),
        (
            "helper.repeated_bloat",
            helper_repeated_tool_call_bloat_checkpoint(
                iteration=64,
                helper_kind="code",
                tool_name="workspace",
                arg_chars=36000,
                count=3,
            ),
        ),
    ]
    for name, text in hints:
        _assert_english_first(name, text)
        assert "PASS" in text
        assert "PARTIAL" in text
        assert "evidence" in text or "handoff" in text
    bloat = dict(hints)["helper.bloat"]
    assert "2,000-4,000 characters" in bloat
    assert "section files" in bloat
    repeated_bloat = dict(hints)["helper.repeated_bloat"]
    assert "convergence checkpoint" in repeated_bloat
    assert "Output files JSON" in repeated_bloat
    read_to_write = dict(hints)["helper.read_to_write"]
    assert "without writing the expected artifact" in read_to_write
    assert "merge or synthesis tasks" in read_to_write


def test_intermediate_feedback_marks_kill_progress_as_stuck():
    from app.core.intermediate_feedback import (
        classify_workflow_feedback_event,
        summarize_workflow_feedback_event,
        validate_intermediate_feedback_message,
    )

    payload = {
        "kind": "helper_progress",
        "task_id": "read-pdf-vocab",
        "helper_kind": "read",
        "heartbeat_status": "fresh",
        "wait_or_continue": "kill",
        "_runaway": True,
        "_runaway_reason": "iter 133 > 100",
        "what_doing": "reading file",
    }
    assert classify_workflow_feedback_event(payload) == "stuck"
    facts = summarize_workflow_feedback_event(payload)
    assert "state=kill" in facts
    assert "event_fact=helper_runaway_or_stuck" in facts
    ok, reason = validate_intermediate_feedback_message(
        message="正在读取词汇材料，马上整理输出。",
        recent_work=facts,
        event="stuck",
    )
    assert not ok
    assert reason == "runaway_helper_described_as_normal_progress"
