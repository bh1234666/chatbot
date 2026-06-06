import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class DebugRecorder:
    def __init__(self):
        self.events = []

    def log(self, category, message, payload=None):
        self.events.append((category, message, payload))


def test_recall_audit_detects_plan_entity_use():
    from app.core.recall_audit import recall_audit_recall_used
    from app.schemas.api import ResponsePlan

    debug = DebugRecorder()
    plan = ResponsePlan(intent="总结 AlphaProject", key_points=["AlphaProject 有文件记录"], tone="自然", length_hint="短")
    base_msgs = [{"role": "system", "content": "## 群组知识库\nAlphaProject 设计文档"}]

    recall_audit_recall_used(plan, base_msgs, debug=debug)

    assert debug.events
    assert debug.events[-1][0] == "recall_audit.used"


def test_recall_audit_reports_unused_memory():
    from app.core.recall_audit import recall_audit_recall_used
    from app.schemas.api import ResponsePlan

    debug = DebugRecorder()
    plan = ResponsePlan(intent="普通回答", key_points=["没有引用实体"], tone="自然", length_hint="短")
    base_msgs = [{"role": "system", "content": "## 群组知识库\nAlphaProject 设计文档"}]

    recall_audit_recall_used(plan, base_msgs, debug=debug)

    assert debug.events
    assert debug.events[-1][0] == "recall_audit.unused"


def test_build_bot_log_records_execution_facts():
    from app.core.bot_log import build_bot_log
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="生成报告",
        key_points=["分析数据", "输出文件"],
        tone="自然",
        length_hint="中",
        delivery_partial=["chart.png"],
    )

    text = build_bot_log(
        plan,
        [("report.md", "/download/report.md", "f:/tmp/report.md")],
        "hard",
        True,
        promoted_to_main=["report.md"],
        helper_status={"h1": {"status": "done"}, "h2": {"status": "failed"}},
        internal_note="部分图表失败",
    )

    assert "complexity=hard" in text
    assert "aborted=true" in text
    assert "deliverables=[report.md]" in text
    assert "delivery_partial=[chart.png]" in text
    assert "helpers={done:[h1],failed:[h2]}" in text


def test_build_bot_log_keeps_easy_mode_when_only_main_delivery_or_partial_exists():
    from app.core.bot_log import build_bot_log
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="",
        key_points=[],
        tone="自然",
        length_hint="短",
        delivery_partial=["draft.docx"],
    )

    text = build_bot_log(
        plan,
        [],
        "easy",
        False,
        promoted_to_main=["final.docx"],
        helper_status={},
        internal_note="主区已有最终件",
    )

    assert text
    assert "complexity=easy" in text
    assert "delivery_partial=[draft.docx]" in text
    assert "in_main=[final.docx]" in text
    assert "note=主区已有最终件" in text


def test_partial_delivery_merge_keeps_existing_entries_when_promote_skips_more():
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="交付文件",
        key_points=[],
        tone="自然",
        length_hint="短",
        delivery_partial=["already-missing.docx"],
    )
    skipped = ["new-skip.docx"]

    existing = set(plan.delivery_partial or [])
    plan.delivery_partial = sorted(existing | set(skipped))

    assert plan.delivery_partial == ["already-missing.docx", "new-skip.docx"]


def test_visible_assistant_text_strips_internal_bot_log():
    from app.core.orchestrator import _visible_assistant_text

    text = (
        "正常好\n\n"
        "<bot_log>complexity=medium; key_points=[_session_manifest.json]; note=internal</bot_log>"
    )

    assert _visible_assistant_text(text) == "正常好"


def test_read_helper_prompt_mentions_tiered_cache_reuse():
    from app.llm.tools.delegate import _HELPER_SYSTEM_READ

    assert "Reuse OCR cache" in _HELPER_SYSTEM_READ
    assert "quality satisfy the purpose" in _HELPER_SYSTEM_READ
    assert "allow_upgrade" in _HELPER_SYSTEM_READ


def test_read_helper_prompt_keeps_internal_material_separate_from_user_copy():
    from app.llm.tools.delegate import _HELPER_SYSTEM_READ, _HELPER_SYSTEM_EDIT

    assert "source-material reading and evidence extraction" in _HELPER_SYSTEM_READ
    assert "one final `.txt`" in _HELPER_SYSTEM_READ
    assert "segment-readable text file" in _HELPER_SYSTEM_READ
    assert "Coverage contract" in _HELPER_SYSTEM_READ
    assert "coverage_summary" in _HELPER_SYSTEM_READ
    assert "item_counts" in _HELPER_SYSTEM_READ
    assert "confirmed content" in _HELPER_SYSTEM_READ
    assert "uncertain content" in _HELPER_SYSTEM_READ
    assert "methods_used" in _HELPER_SYSTEM_READ
    assert "cache_status" in _HELPER_SYSTEM_READ

    assert "preserve the acceptance contract" in _HELPER_SYSTEM_EDIT
    assert "coverage summaries, item counts, section maps, and line ranges" in _HELPER_SYSTEM_EDIT
    assert "Visual text evidence is a source, not a template" in _HELPER_SYSTEM_EDIT
    assert "User-facing documents should say what is visible or stated" in _HELPER_SYSTEM_EDIT
    assert "confirmed source facts from uncertain text" in _HELPER_SYSTEM_EDIT


def test_resource_helper_prompt_says_main_process_dispatched_it():
    from app.llm.tools.delegate_resources import _resource_task_prompt

    prompt = _resource_task_prompt(
        blocked_task_id="edit_doc",
        blocked_kind="edit",
        resource_request={
            "suggested_helper_kind": "draw",
            "blocked_reason": "缺少图表",
            "needed_outputs": ["chart.png"],
        },
    )
    assert "主进程为解冻依赖而派发" in prompt
    assert "自动派发" not in prompt
    assert "request_resource 冻结" in prompt


def test_round3_auto_voice_tts_uses_workspace_cwd():
    import inspect
    from app.core import orchestrator_entry

    src = inspect.getsource(orchestrator_entry.orchestrate)
    assert '"output": voice_path' in src
    assert '"cwd": workspace_dir' in src
    assert "_tts_func," in src
    assert 'voice_instruct or "female, moderate pitch"' not in src
    assert "voice.profile_missing" in src


def test_progress_tool_summary_hides_internal_resource_names():
    from app.core.orchestrator_utils import _brief_tool_desc, _summarize_delegate_result

    brief = _brief_tool_desc("delegate", {
        "tasks": [{"task_id": "ocr_card", "kind": "ocr"}],
    })
    assert brief == "处理 1 个子任务"
    assert "ocr" not in brief.lower()
    assert "helper" not in brief.lower()

    summary = _summarize_delegate_result({
        "results": [{"task_id": "ocr_card", "ok": True, "elapsed_sec": 12.3}],
        "total_elapsed_seconds": 12.3,
    }, "分派 1 个并行任务:['ocr_card']")
    assert "子任务" in summary
    assert "ocr" not in summary.lower()
    assert "helper" not in summary.lower()


def test_document_source_grounding_warnings_flag_internal_and_overclaim_text():
    from app.llm.tools.delegate_quality import document_source_grounding_warnings

    text = (
        "本报告基于 OCR 识别结果撰写。\n"
        "卡片检测结果为 PASS，表示三个参数均满足既定标准。"
    )
    issues = {w["issue"] for w in document_source_grounding_warnings(text)}

    assert "document_internal_source_label" in issues
    assert "document_unbacked_pass_standard" in issues


def test_ensure_temp_workspace_rotates_per_main_workspace(tmp_path, monkeypatch):
    from app.llm.tools import workspace as ws_mod

    monkeypatch.setattr(ws_mod, "_rotation_done", False)
    ws_mod._rotated_main_workspaces.clear()

    main_a = tmp_path / "a"
    main_b = tmp_path / "b"
    main_a.mkdir()
    main_b.mkdir()

    temp_a = ws_mod.ensure_temp_workspace(str(main_a), session_tag="a")
    temp_b = ws_mod.ensure_temp_workspace(str(main_b), session_tag="b")

    assert (main_a / ".temp").is_dir()
    assert (main_b / ".temp").is_dir()
    assert temp_a != temp_b


async def test_workspace_run_creates_missing_cwd(tmp_path):
    from app.llm.tools.workspace import handle_run

    missing = tmp_path / "missing-temp"
    result = await handle_run(str(missing), "dir", timeout_sec=5)

    assert missing.is_dir()
    assert result["ok"] is True


async def test_workspace_run_does_not_inherit_parent_pythonpath(tmp_path, monkeypatch):
    from app.llm.tools.workspace import handle_run

    bad_root = tmp_path / "bad_root"
    good_root = tmp_path / "good_root"
    (bad_root / "pkg").mkdir(parents=True)
    (good_root / "pkg").mkdir(parents=True)
    (bad_root / "pkg" / "__init__.py").write_text("VALUE='bad'\n", encoding="utf-8")
    (good_root / "pkg" / "__init__.py").write_text("VALUE='good'\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(bad_root))

    no_path = await handle_run(str(tmp_path), "python -c \"import pkg; print(pkg.VALUE)\"", timeout_sec=5)
    explicit = await handle_run(
        str(tmp_path),
        f"set PYTHONPATH={good_root} && python -c \"import pkg; print(pkg.VALUE)\"",
        timeout_sec=5,
    )

    assert no_path["ok"] is False
    error_text = no_path.get("stderr") or no_path.get("test_summary") or ""
    assert "ModuleNotFoundError" in error_text or "ImportError: No module named pkg" in error_text
    assert explicit["ok"] is True
    assert explicit["stdout"].strip() == "good"


async def test_workspace_run_adds_workspace_root_for_helpers_shared_package(tmp_path):
    from app.llm.tools.workspace import handle_run

    shared = tmp_path / "_helpers_shared"
    shared.mkdir()
    (shared / "__init__.py").write_text("", encoding="utf-8")
    (shared / "compression_framework.py").write_text("VALUE = 'shared-ok'\n", encoding="utf-8")
    (shared / "huffman.py").write_text(
        "from _helpers_shared.compression_framework import VALUE\nprint(VALUE)\n",
        encoding="utf-8",
    )

    result = await handle_run(str(tmp_path), "python _helpers_shared/huffman.py", timeout_sec=5)

    assert result["ok"] is True
    assert result["stdout"].strip() == "shared-ok"


async def test_workspace_run_pytest_forces_workspace_rootdir(tmp_path, monkeypatch):
    from app.llm.tools import workspace as ws_tool

    monkeypatch.setattr(ws_tool, "_check_bash_rate", lambda owner: asyncio.sleep(0, result=True))
    monkeypatch.setattr(ws_tool, "_is_main_thread", lambda: False)
    monkeypatch.setattr(ws_tool, "analyze_command", lambda *a, **k: type("D", (), {"allowed": True, "reason": "", "category": "allow"})())
    monkeypatch.setattr(ws_tool, "_ensure_matplotlibrc", lambda path: None)

    captured = {}

    class FakeProc:
        pid = 34567
        returncode = 0

        async def communicate(self):
            return b"ok", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        return FakeProc()

    monkeypatch.setattr(ws_tool.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await ws_tool.handle_run(str(tmp_path), "python -m pytest tests -q", timeout_sec=5)

    assert result["ok"] is True
    assert "--rootdir=." in captured["env"]["PYTEST_ADDOPTS"]


async def test_workspace_run_pytest_injects_rootdir_into_prefixed_command(tmp_path, monkeypatch):
    from app.llm.tools import workspace as ws_tool

    monkeypatch.setattr(ws_tool, "_check_bash_rate", lambda owner: asyncio.sleep(0, result=True))
    monkeypatch.setattr(ws_tool, "_is_main_thread", lambda: False)
    monkeypatch.setattr(ws_tool, "analyze_command", lambda *a, **k: type("D", (), {"allowed": True, "reason": "", "category": "allow"})())
    monkeypatch.setattr(ws_tool, "_ensure_matplotlibrc", lambda path: None)

    captured = {}

    class FakeProc:
        pid = 34568
        returncode = 0

        async def communicate(self):
            return b"ok", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        return FakeProc()

    monkeypatch.setattr(ws_tool.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await ws_tool.handle_run(
        str(tmp_path),
        "set PYTHONPATH=src && python -m pytest tests -q",
        timeout_sec=5,
    )

    assert result["ok"] is True
    assert "--rootdir=." in captured["args"][2]
    assert "-c " in captured["args"][2]
    assert ".workspace_pytest_empty.ini" in captured["args"][2]
    assert str(tmp_path) in captured["args"][2]


def test_workspace_run_pytest_empty_config_is_absolute_for_subdir_commands(tmp_path):
    from app.llm.tools.workspace_run import _augment_pytest_command

    (tmp_path / "_env").mkdir()

    command = _augment_pytest_command(
        "cd _env && python -m pytest tests -q",
        str(tmp_path),
    )

    assert "--rootdir=." in command
    assert ".workspace_pytest_empty.ini" in command
    assert str(tmp_path) in command
    assert (tmp_path / "_env" / ".workspace_pytest_empty.ini").is_file()
    assert "-c " in command


def test_workspace_run_pytest_uses_project_config_for_cd_subdir(tmp_path):
    from app.llm.tools.workspace_run import _augment_pytest_command

    (tmp_path / "_env").mkdir()
    (tmp_path / "_env" / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\npythonpath = ['src']\n",
        encoding="utf-8",
    )

    command = _augment_pytest_command(
        "cd _env && python -m pytest tests -q",
        str(tmp_path),
    )

    assert "-c pyproject.toml" in command
    assert ".workspace_pytest_empty.ini" not in command
