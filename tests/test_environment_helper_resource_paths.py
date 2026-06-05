import asyncio
from pathlib import Path

from app.core.runtime_mode import EnvironmentContext, runtime_context
from app.core.orchestrator_entry import _environment_project_tool_route
from app.llm.tools.delegate_actions import (
    _annotate_source_count_hints_from_manifest,
    _auto_fetch_environment_workspace_refs,
    _normalize_environment_output_paths_from_manifest,
)
from app.llm.tools.registry import _handle_fetch_to_temp


def test_environment_project_route_detects_project_paths():
    needs_tools, is_coding, reason = _environment_project_tool_route(
        "Inspect llm/tools/delegate_actions.py and summarize responsibilities."
    )

    assert needs_tools is True
    assert is_coding is True
    assert reason


def test_environment_project_route_detects_directory_visibility():
    needs_tools, is_coding, reason = _environment_project_tool_route("能看到当前目录吗")

    assert needs_tools is True
    assert is_coding is False
    assert reason


def test_environment_project_route_ignores_tool_concept_chat():
    needs_tools, is_coding, reason = _environment_project_tool_route("OCR 是什么技术")

    assert needs_tools is False
    assert is_coding is False
    assert reason == ""


def test_environment_delegate_auto_fetches_project_paths(tmp_path):
    root = Path("app").resolve()
    env = EnvironmentContext(
        root_dir=str(root),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )
    tasks = [{
        "prompt": (
            "Refactor llm/tools/delegate_actions.py and inspect "
            "`llm/tools/delegate.py` before editing."
        ),
        "expected_outputs": ["delegate/actions.py"],
    }]

    with runtime_context("environment", env):
        result = _auto_fetch_environment_workspace_refs(str(tmp_path), tasks)

    assert "llm/tools/delegate_actions.py" in result["fetched"]
    assert "llm/tools/delegate.py" in result["fetched"]
    assert (tmp_path / "_env/llm/tools/delegate_actions.py").is_file()
    assert (tmp_path / "_env/llm/tools/delegate.py").is_file()


def test_environment_summarize_helper_receives_project_inventory(tmp_path):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "docs").mkdir()
    (project / "README.md").write_text("# Demo\n", encoding="utf-8")
    (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (project / "src" / "main.py").write_text("print('demo')\n", encoding="utf-8")
    (project / "docs" / "口语素材.txt").write_text("part 2 topic\n", encoding="utf-8")
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )
    tasks = [{
        "kind": "summarize",
        "prompt": "Build an inventory of the current project and identify important IELTS files.",
        "expected_outputs": [],
    }]

    with runtime_context("environment", env):
        result = _auto_fetch_environment_workspace_refs(str(tmp_path / "workspace"), tasks)

    assert "project_inventory.md" in result["fetched"]
    inventory = tmp_path / "workspace" / "_env" / "project_inventory.md"
    text = inventory.read_text(encoding="utf-8")
    assert "src/main.py" in text
    assert "docs/口语素材.txt" in text
    assert "Suffix Counts" in text
    assert "Key Candidate Paths" in text


def test_environment_read_helper_receives_project_resource_manifest_for_broad_materials(tmp_path):
    project = tmp_path / "project"
    target = project / "代文静组" / "电子信息01班代文静组" / "202305050127代文静.docx"
    sheet = project / "黄超龙组" / "项目管理-黄超龙组" / "财务评价报表.xlsx"
    target.parent.mkdir(parents=True)
    sheet.parent.mkdir(parents=True)
    target.write_bytes(b"fake-docx")
    sheet.write_bytes(b"fake-xlsx")
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )
    tasks = [{
        "kind": "read",
        "task_id": "read_groups",
        "prompt": "Read all engineering-management group materials and build coverage evidence.",
        "expected_outputs": ["group_coverage_evidence.txt"],
    }]

    with runtime_context("environment", env):
        result = _auto_fetch_environment_workspace_refs(str(tmp_path / "workspace"), tasks)

    assert "project_inventory.md" in result["fetched"]
    assert ".resource_manifest.json" in result["fetched"]
    inventory = tmp_path / "workspace" / "_env" / "project_inventory.md"
    text = inventory.read_text(encoding="utf-8")
    assert "代文静组/电子信息01班代文静组/202305050127代文静.docx" in text
    assert "黄超龙组/项目管理-黄超龙组/财务评价报表.xlsx" in text
    manifest = tmp_path / "workspace" / "_env" / ".resource_manifest.json"
    data = manifest.read_text(encoding="utf-8")
    assert '"project_path": "代文静组/电子信息01班代文静组/202305050127代文静.docx"' in data
    assert '"staged_path": "_env/代文静组/电子信息01班代文静组/202305050127代文静.docx"' in data


def test_environment_manifest_adds_source_count_hint_for_broad_synthesis(tmp_path):
    project = tmp_path / "project"
    for idx in range(8):
        target = project / "materials" / f"ielts_{idx}.docx"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-docx")
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )
    tasks = [{
        "kind": "code",
        "task_id": "ielts_synthesis",
        "prompt": "Read all files in the current directory and organize all source material by IELTS section.",
        "expected_outputs": ["_env/ielts_report.md"],
    }]

    with runtime_context("environment", env):
        _auto_fetch_environment_workspace_refs(str(tmp_path / "workspace"), tasks)
        _annotate_source_count_hints_from_manifest(str(tmp_path / "workspace"), tasks)

    assert tasks[0]["_source_count_hint"] == 8


def test_environment_manifest_source_hint_drives_parallel_read_split():
    from app.llm.tools.delegate import _deterministic_source_read_split_recommendations

    tasks = [{
        "kind": "code",
        "task_id": "ielts_synthesis",
        "prompt": (
            "Read all files in the current directory and organize all source material by IELTS section. "
            "Downstream writing can consume evidence files after coverage is complete."
        ),
        "expected_outputs": ["_env/analysis_outputs/ielts_report.md"],
        "_source_count_hint": 18,
    }]

    recommendations = _deterministic_source_read_split_recommendations(tasks)

    assert recommendations
    assert recommendations[0]["task_id"] == "ielts_synthesis"
    assert recommendations[0]["should_split"] is True
    assert len(recommendations[0]["split_into"]) >= 3
    assert "parallel read helpers" in recommendations[0]["reason"]


def test_environment_output_basename_normalizes_to_unique_manifest_path(tmp_path):
    project = tmp_path / "project"
    target = project / "src" / "pkg" / "models.py"
    target.parent.mkdir(parents=True)
    target.write_text("class Model: pass\n", encoding="utf-8")
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )
    workspace = tmp_path / "workspace"
    tasks = [{
        "kind": "code",
        "task_id": "edit_models",
        "prompt": "Modify _env/src/pkg/models.py and keep the same project path.",
        "expected_outputs": ["models.py"],
    }]

    with runtime_context("environment", env):
        _auto_fetch_environment_workspace_refs(str(workspace), tasks)
        _normalize_environment_output_paths_from_manifest(str(workspace), tasks)

    assert tasks[0]["expected_outputs"] == ["_env/src/pkg/models.py"]


async def test_environment_output_basename_normalizes_during_delegate_sanitize(tmp_path):
    from app.llm.tools.delegate import _sanitize_and_validate_tasks

    project = tmp_path / "project"
    target = project / "src" / "pkg" / "models.py"
    target.parent.mkdir(parents=True)
    target.write_text("class Model: pass\n", encoding="utf-8")
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )
    workspace = tmp_path / "workspace"

    with runtime_context("environment", env):
        cleaned = await _sanitize_and_validate_tasks(
            {
                "tasks": [{
                    "kind": "code",
                    "task_id": "edit_models",
                    "prompt": "Modify _env/src/pkg/models.py and keep the same project path.",
                    "expected_outputs": ["models.py"],
                }]
            },
            main_workspace=str(workspace),
            archive_id="arch",
            group_id="group",
            user_id="user",
        )

    assert not isinstance(cleaned, str)
    assert cleaned[0]["expected_outputs"] == ["_env/src/pkg/models.py"]


async def test_inventory_helper_cannot_read_source_material_body(tmp_path):
    from app.core.core_processes import (
        HELPER_OWNER_PREFIX,
        reset_current_helper_kind,
        reset_current_owner,
        set_current_helper_kind,
        set_current_owner,
    )
    from app.llm.tools.registry import dispatch

    (tmp_path / "_env").mkdir()
    (tmp_path / "_env" / "project_inventory.md").write_text("# Inventory\n", encoding="utf-8")
    (tmp_path / "_env" / "作业.txt").write_text("homework body\n", encoding="utf-8")

    owner_token = set_current_owner(f"{HELPER_OWNER_PREFIX}:trace:inventory_materials")
    kind_token = set_current_helper_kind("inventory")
    try:
        allowed = await dispatch(
            "read_file",
            {"path": "_env/project_inventory.md"},
            archive_id="arch",
            group_id="group",
            user_id="user",
            workspace_dir=str(tmp_path),
            caller="helper",
        )
        blocked = await dispatch(
            "read_file",
            {"path": "_env/作业.txt"},
            archive_id="arch",
            group_id="group",
            user_id="user",
            workspace_dir=str(tmp_path),
            caller="helper",
        )
    finally:
        reset_current_helper_kind(kind_token)
        reset_current_owner(owner_token)

    assert '"ok": true' in allowed
    assert "inventory_helper_source_material_read_forbidden" in blocked
    assert '"suggested_helper_kind": "read"' in blocked


def test_environment_output_basename_keeps_ambiguous_manifest_path(tmp_path):
    project = tmp_path / "project"
    first = project / "src" / "models.py"
    second = project / "tests" / "models.py"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("A = 1\n", encoding="utf-8")
    second.write_text("B = 2\n", encoding="utf-8")
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )
    workspace = tmp_path / "workspace"
    tasks = [{
        "kind": "code",
        "task_id": "edit_models",
        "prompt": "Modify one models.py after deciding the correct owner.",
        "expected_outputs": ["models.py"],
    }]

    with runtime_context("environment", env):
        _auto_fetch_environment_workspace_refs(str(workspace), tasks)
        _normalize_environment_output_paths_from_manifest(str(workspace), tasks)

    assert tasks[0]["expected_outputs"] == ["models.py"]


def test_environment_delegate_auto_fetches_visual_project_paths_with_unicode(tmp_path):
    project = tmp_path / "project"
    (project / "image" / "2026-5").mkdir(parents=True)
    (project / "image" / "2026-5" / "屏幕截图 2026-05-11 205512.jpg").write_bytes(b"fake-jpg")
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )
    tasks = [{
        "kind": "read",
        "prompt": (
            "OCR these project images for the report: "
            "`image/2026-5/屏幕截图 2026-05-11 205512.jpg`"
        ),
        "expected_outputs": [],
    }]

    with runtime_context("environment", env):
        result = _auto_fetch_environment_workspace_refs(str(tmp_path / "workspace"), tasks)

    assert "image/2026-5/屏幕截图 2026-05-11 205512.jpg" in result["fetched"]
    assert (
        tmp_path
        / "workspace"
        / "_env"
        / "image"
        / "2026-5"
        / "屏幕截图 2026-05-11 205512.jpg"
    ).read_bytes() == b"fake-jpg"


def test_environment_delegate_auto_fetches_absolute_visual_project_paths(tmp_path):
    project = tmp_path / "project"
    (project / "image").mkdir(parents=True)
    image = project / "image" / "屏幕截图 2026-05-11 205512.jpg"
    image.write_bytes(b"absolute-fake-jpg")
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )
    tasks = [{
        "kind": "read",
        "prompt": f"OCR project image {image} for exact visible text.",
        "expected_outputs": [],
    }]

    with runtime_context("environment", env):
        result = _auto_fetch_environment_workspace_refs(str(tmp_path / "workspace"), tasks)

    assert "image/屏幕截图 2026-05-11 205512.jpg" in result["fetched"]
    assert (
        tmp_path
        / "workspace"
        / "_env"
        / "image"
        / "屏幕截图 2026-05-11 205512.jpg"
    ).read_bytes() == b"absolute-fake-jpg"


def test_environment_delegate_auto_fetches_unquoted_office_paths_with_spaces(tmp_path):
    project = tmp_path / "project"
    target = project / "wx2" / "包涵 - 2026.5-8月 口语话题更新 (2026.5.15修改).docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"fake-docx")
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )
    tasks = [{
        "kind": "read",
        "prompt": (
            "Read _env/wx2/包涵 - 2026.5-8月 口语话题更新 (2026.5.15修改).docx "
            "and summarize speaking topics."
        ),
        "expected_outputs": [],
    }]

    with runtime_context("environment", env):
        result = _auto_fetch_environment_workspace_refs(str(tmp_path / "workspace"), tasks)

    rel = "wx2/包涵 - 2026.5-8月 口语话题更新 (2026.5.15修改).docx"
    assert rel in result["fetched"]
    assert (tmp_path / "workspace" / "_env" / rel).read_bytes() == b"fake-docx"


def test_fetch_to_temp_normalizes_env_paths_in_environment_mode(tmp_path):
    root = Path("app").resolve()
    env = EnvironmentContext(
        root_dir=str(root),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )
    helper_ws = tmp_path / ".temp" / "_delegate_test"
    helper_ws.mkdir(parents=True)

    async def run():
        with runtime_context("environment", env):
            return await _handle_fetch_to_temp(str(helper_ws), {
                "source": "main",
                "paths": ["_env/llm/tools/delegate.py"],
            })

    raw = asyncio.run(run())

    assert '"ok": true' in raw
    assert '"skipped": []' in raw
    assert '"normalized_paths": ["llm/tools/delegate.py"]' in raw
    assert (helper_ws / "_env/llm/tools/delegate.py").is_file()


def test_fetch_to_temp_fetches_project_relative_visual_files_in_environment_mode(tmp_path):
    project = tmp_path / "project"
    (project / "image" / "2026-5").mkdir(parents=True)
    (project / "image" / "2026-5" / "sample.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )
    main_ws = tmp_path / "workspace"
    helper_ws = main_ws / ".temp" / "_delegate_ocr"
    helper_ws.mkdir(parents=True)

    async def run():
        with runtime_context("environment", env):
            return await _handle_fetch_to_temp(str(helper_ws), {
                "source": "main",
                "paths": ["image/2026-5/sample.png"],
            })

    raw = asyncio.run(run())

    assert '"ok": true' in raw
    assert '"skipped": []' in raw
    assert '"env_copied": ["_env/image/2026-5/sample.png"]' in raw
    assert (helper_ws / "_env" / "image" / "2026-5" / "sample.png").read_bytes() == b"\x89PNG\r\n\x1a\n"
