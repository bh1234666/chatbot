import json

import sys

from pathlib import Path



import pytest



sys.path.insert(0, str(Path(__file__).parent.parent))





def _tool_names(tools):

    return [t["function"]["name"] for t in tools]





def test_chat_mode_tool_list_is_original_object_and_no_env_tools():

    from app.llm.tools import registry

    from app.core.runtime_mode import EnvironmentContext, runtime_context



    baseline = registry.ROUND2_TOOLS

    assert registry.tools_for_runtime_mode("chat") is baseline

    assert not any(name.startswith("env_") for name in _tool_names(registry.tools_for_runtime_mode("chat")))



    env = EnvironmentContext(

        root_dir=str(Path.cwd()),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        assert registry.tools_for_runtime_mode("chat") is baseline

        assert not any(name.startswith("env_") for name in _tool_names(registry.tools_for_runtime_mode("chat")))





def test_environment_mode_tools_are_additive_only():

    from app.llm.tools import registry

    from app.core.runtime_mode import EnvironmentContext, runtime_context



    baseline_names = _tool_names(registry.ROUND2_TOOLS)

    env = EnvironmentContext(

        root_dir=str(Path.cwd()),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        names = _tool_names(registry.tools_for_current_runtime())

    assert names[: len(baseline_names)] == baseline_names

    assert "env_inventory" in names

    assert "env_list_tree" in names

    assert "env_run" in names





async def test_env_inventory_returns_exact_paths_and_writes_manifests(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    (project / "wx2").mkdir(parents=True)

    (project / "image").mkdir()

    (project / "wx2" / "包涵 - 2026.5-8月 口语话题更新.docx").write_bytes(b"docx")

    (project / "file" / "2026-5").mkdir(parents=True)

    (project / "file" / "2026-5" / "P1-P3场景词汇.pdf").write_bytes(b"pdf")

    (project / "image" / "微信图片_20260508193433.jpg").write_bytes(b"jpg")

    (project / "阅读.txt").write_text("vocab\n", encoding="utf-8")



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    workspace = tmp_path / "workspace"

    with runtime_context("environment", env):

        result = json.loads(await handle_environment_tool(

            "env_inventory",

            str(workspace),

            {"categories": ["office_pdf", "image"], "limit": 10},

        ))



    assert result["ok"] is True

    paths = {item["project_path"] for item in result["resources"]}

    assert "wx2/包涵 - 2026.5-8月 口语话题更新.docx" in paths

    assert "file/2026-5/P1-P3场景词汇.pdf" in paths

    assert "image/微信图片_20260508193433.jpg" in paths

    assert "阅读.txt" not in paths

    assert (workspace / "_env" / "project_inventory.md").is_file()

    assert (workspace / "_env" / ".resource_manifest.json").is_file()





@pytest.mark.asyncio

async def test_env_inventory_reports_effective_material_count_and_skips_lock_files(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    project.mkdir()

    (project / "lesson.docx").write_bytes(b"docx")

    (project / "~$lesson.docx").write_bytes(b"lock")

    workspace = tmp_path / "workspace"

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        result = json.loads(await handle_environment_tool("env_inventory", str(workspace), {}))



    assert result["ok"] is True

    assert result["summary"]["total_project_files"] == 2

    assert result["summary"]["listed_files"] == 1

    assert result["summary"]["effective_material_files"] == 1

    assert result["summary"]["skipped_low_value_files"] == 1

    inventory_text = (workspace / "_env" / "project_inventory.md").read_text(encoding="utf-8")

    assert "`total_project_files` counts files in the indexed project surface" in inventory_text

    assert "`~$lesson.docx` (office_lock_file)" in inventory_text





@pytest.mark.asyncio

async def test_env_inventory_skips_internal_generated_directories(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    project.mkdir()

    (project / "README.md").write_text("# Demo\n", encoding="utf-8")

    for dirname in ("backups", "logs", ".temp", "stress_tools"):

        nested = project / dirname / "old"

        nested.mkdir(parents=True)

        for idx in range(5):

            (nested / f"generated_{idx}.py").write_text("print('skip')\n", encoding="utf-8")

    workspace = tmp_path / "workspace"

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        result = json.loads(await handle_environment_tool("env_inventory", str(workspace), {}))



    assert result["ok"] is True

    assert result["summary"]["listed_files"] == 1

    assert result["summary"]["total_project_files"] == 1

    paths = {item["project_path"] for item in result["resources"]}

    assert paths == {"README.md"}



@pytest.mark.asyncio

async def test_env_inventory_skips_runtime_state_paths(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    project.mkdir()

    (project / "README.md").write_text("# Demo\n", encoding="utf-8")

    (project / "chatbot.db").write_bytes(b"sqlite")

    (project / "chatbot.db-wal").write_bytes(b"wal")

    (project / "data" / "workspaces" / "arch" / "group").mkdir(parents=True)

    (project / "data" / "workspaces" / "arch" / "group" / "helper_output.txt").write_text("skip\n", encoding="utf-8")

    (project / "data" / "environment_projects.json").write_text("{}", encoding="utf-8")

    (project / "data" / "materials" / "lesson.txt").parent.mkdir(parents=True)

    (project / "data" / "materials" / "lesson.txt").write_text("keep\n", encoding="utf-8")

    workspace = tmp_path / "workspace"

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        result = json.loads(await handle_environment_tool("env_inventory", str(workspace), {}))



    assert result["ok"] is True

    paths = {item["project_path"] for item in result["resources"]}

    assert paths == {"README.md", "data/materials/lesson.txt"}

    assert result["summary"]["listed_files"] == 2

    assert result["summary"]["total_project_files"] == 2



async def test_env_inventory_can_stage_filtered_batch(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    (project / "docs").mkdir(parents=True)

    (project / "docs" / "a.docx").write_bytes(b"docx-a")

    (project / "docs" / "b.docx").write_bytes(b"docx-b")

    (project / "docs" / "large.pdf").write_bytes(b"x" * 32)

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    workspace = tmp_path / "workspace"

    with runtime_context("environment", env):

        result = json.loads(await handle_environment_tool(

            "env_inventory",

            str(workspace),

            {"suffixes": [".docx"], "stage": True, "stage_limit": 1, "limit": 5},

        ))



    assert result["ok"] is True

    assert len(result["staged_now"]) == 1

    assert result["stage_skipped"][0]["reason"] == "stage_limit"

    staged = result["staged_now"][0]

    assert (workspace / "_env" / staged).is_file()

    resource_by_path = {item["project_path"]: item for item in result["resources"]}

    assert resource_by_path[staged]["staged"] is True





async def test_env_read_can_read_generated_inventory_manifest(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    project.mkdir()

    (project / "README.md").write_text("# Demo\n", encoding="utf-8")

    workspace = tmp_path / "workspace"

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        inventory = json.loads(await handle_environment_tool("env_inventory", str(workspace), {}))

        read = json.loads(await handle_environment_tool(

            "env_read",

            str(workspace),

            {"path": "_env/project_inventory.md", "max_chars": 4000},

        ))



    assert inventory["ok"] is True

    assert read["ok"] is True

    assert read["path"] == "_env/project_inventory.md"

    assert read["source_zone"] == "workspace_staged"

    assert "Project Resource Manifest" in read["content"]

    assert "README.md" in read["content"]




def test_environment_mode_appends_inventory_tool_without_replacing_chat_delegate():

    from app.llm.tools import registry

    from app.core.runtime_mode import EnvironmentContext, runtime_context



    chat_tools = registry.tools_for_runtime_mode("chat")

    chat_delegate = next(t for t in chat_tools if t["function"]["name"] == "delegate")

    chat_kind = chat_delegate["function"]["parameters"]["properties"]["tasks"]["items"]["properties"]["kind"]

    assert "inventory" in chat_kind["enum"]

    assert "summarize" not in chat_kind["enum"]



    env = EnvironmentContext(

        root_dir=str(Path.cwd()),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        env_tools = registry.tools_for_current_runtime()



    assert env_tools[:len(chat_tools)] == chat_tools

    assert env_tools[len(chat_tools) - 1] is chat_tools[-1]

    env_names = _tool_names(env_tools)

    assert "delegate_inventory" in env_names

    assert env_names.index("delegate_inventory") > env_names.index("delegate")

    assert next(t for t in env_tools if t["function"]["name"] == "delegate") is chat_delegate





def test_environment_task_quality_guard_knows_inventory_only_in_project_mode():

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.delegate import _task_quality_guard_environment_helper_text



    env = EnvironmentContext(

        root_dir=str(Path.cwd()),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        helper_line, suggested_line, principle = _task_quality_guard_environment_helper_text()



    assert "inventory" in helper_line

    assert "environment-only first-pass project inventory" in helper_line

    assert "environment-only inventory" in suggested_line

    assert "First-pass unfamiliar project inventory -> inventory" in principle





def test_environment_prompt_addon_chat_mode_empty():

    from app.core.environment_prompt import environment_project_context, environment_prompt_addon

    from app.core.runtime_mode import EnvironmentContext, runtime_context



    assert environment_prompt_addon() == ""

    env = EnvironmentContext(

        root_dir=str(Path.cwd()),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

        project_name="demo",

    )

    with runtime_context("environment", env):

        text = environment_prompt_addon()
        project_context = environment_project_context()

    assert "Environment Project Mode" in text

    assert "env_fetch" in text

    assert str(Path.cwd()) not in text
    assert str(Path.cwd()) in project_context
    assert "Current Environment Project" in project_context
    assert "demo" in project_context

    assert "project maintenance" in text

    assert "chat workspace is not the project directory" in text

    assert "project_map" in text

    assert "file_summary" in text

    assert "impact_review" in text

    assert "starting with inventory" in text

    assert "whole-directory questions" in text

    assert "delegate_inventory" in text
    assert "inventory, summarize" not in text

    assert "first-pass inventory" in text

    assert "run it or report the blocker instead of asking whether to run it" in text

    assert "Acceptance Closure" in text

    assert "documentation/implementation contradiction" in text

    assert "env_apply_create is only for targets confirmed absent" in text

    assert "prefer env_run over workspace/bash" in text

    assert "real temporary directories on the current platform" in text

    assert "observed behavior" in text

    assert "mocked async processes must match the interface" in text

    assert "syntax/collection check" in text

    assert "print exact results with project-relative paths" in text

    assert "compact structural map" in text

    assert "top-level symbols" in text

    assert "read_file is only for chat-workspace files" in text

    assert "not in the project directory" in text

    assert "project-local backup before replacing" not in text





def test_environment_prompts_keep_inspection_scripts_outside_project():

    from app.core.environment_prompt import environment_prompt_addon, environment_round2_system_prompt

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import ENV_RUN_SCHEMA



    env = EnvironmentContext(

        root_dir=str(Path.cwd()),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        addon = environment_prompt_addon()

        round2 = environment_round2_system_prompt()["content"]

    schema_desc = ENV_RUN_SCHEMA["function"]["description"]



    for text in (addon, round2, schema_desc):

        assert "python_code" in text

        assert "outside the project tree" in text

        assert ("Inspection scripts are not project" in text) or ("检查脚本不属于项目文件" in text)

        assert "Characters, bytes, file size, line count, and file count are" in text

        assert "transient inspection scripts" in text

    for text in (addon, round2):

        assert "env_apply_create" in text





def test_environment_prompts_keep_bulk_source_body_extraction_in_read_helpers():

    from app.core.environment_prompt import environment_prompt_addon, environment_round2_system_prompt

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import ENV_RUN_SCHEMA



    env = EnvironmentContext(

        root_dir=str(Path.cwd()),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        addon = environment_prompt_addon()

        round2 = environment_round2_system_prompt()["content"]

    schema_desc = ENV_RUN_SCHEMA["function"]["description"]



    for text in (addon, round2, schema_desc):

        assert "bulk" in text

        assert "Office/PDF/image" in text

        assert "read helpers" in text

    assert "use env_run for inventory, counts, locating, and spot-checks" in addon

    assert "bulk Office/PDF/image body extraction belongs to split read helpers" in round2





def test_environment_prompts_distinguish_project_paths_from_env_staging():

    from app.core.environment_prompt import environment_prompt_addon, environment_round2_system_prompt

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import ENV_RUN_SCHEMA

    from app.llm.tools.registry import tools_for_runtime_mode



    env = EnvironmentContext(

        root_dir=str(Path.cwd()),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        addon = environment_prompt_addon()

        round2 = environment_round2_system_prompt()["content"]

        runtime_tools = tools_for_runtime_mode("environment")

    schema_desc = ENV_RUN_SCHEMA["function"]["description"]

    workspace_schema = next(t for t in runtime_tools if t["function"]["name"] == "workspace")

    workspace_text = str(workspace_schema)



    for text in (addon, round2, schema_desc):

        assert "env_run" in text
        assert (
            "executes in the real project directory" in text
            or "uses the real project tree" in text
            or "real project evidence comes from" in text
        )

        assert "project-relative paths" in text

        assert "`_env/...`" in text

    for text in (addon, round2):

        assert "read_file is only for chat-workspace files" in text

        assert "edit_file, multi_edit" in text

    assert "this tool works in the chat workspace, not the real project directory" in workspace_text

    assert "`_env/...` paths are staged copies" in workspace_text

    assert "not a project creation namespace" in workspace_text

    assert "workspace.mkdir creates chat-workspace folders only" in workspace_text

    assert "env_apply_create" in workspace_text

    assert "env_apply_replace" in workspace_text





def test_environment_tool_schema_contracts_keep_apply_flow_visible():

    from app.llm.tools.environment import (

        ENV_APPLY_CREATE_SCHEMA,

        ENV_APPLY_REPLACE_SCHEMA,

        ENV_DIFF_SCHEMA,

        ENV_FETCH_SCHEMA,

    )



    fetch_text = ENV_FETCH_SCHEMA["function"]["description"]

    diff_text = ENV_DIFF_SCHEMA["function"]["description"]

    replace_text = ENV_APPLY_REPLACE_SCHEMA["function"]["description"]

    create_text = ENV_APPLY_CREATE_SCHEMA["function"]["description"]



    assert "staged workspace copy" in fetch_text

    assert "env_run uses real project-relative paths instead" in fetch_text

    assert "edited `_env/...` workspace copy" in diff_text

    assert "apply step for existing project files" in replace_text

    assert "Create new project files through env_apply_create or accepted helper outputs" in create_text

    assert "for existing files use env_fetch, edit, env_diff, then env_apply_replace" in create_text





def test_environment_prompts_do_not_treat_workspace_mkdir_as_project_creation():

    from app.core.environment_prompt import environment_prompt_addon, environment_round2_system_prompt

    from app.core.runtime_mode import EnvironmentContext, runtime_context



    env = EnvironmentContext(

        root_dir=str(Path.cwd()),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        addon = environment_prompt_addon()

        round2 = environment_round2_system_prompt()["content"]



    for text in (addon, round2):

        assert "workspace.mkdir" in text

        assert ("chat-workspace folders" in text) or ("chat-workspace creation" in text)

        assert "env_apply_create" in text

        assert "creates parent directories" in text





def test_environment_prompts_call_out_platform_sensitive_tests():

    from app.core.environment_prompt import environment_prompt_addon, environment_round2_system_prompt

    from app.core.runtime_mode import EnvironmentContext, runtime_context



    env = EnvironmentContext(

        root_dir=str(Path.cwd()),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        addon = environment_prompt_addon()

        round2 = environment_round2_system_prompt()["content"]



    for text in (addon, round2):

        assert "path, command, and OS-sensitive tests" in text

        assert "real temporary directories on the current platform" in text

        assert ("observed behavior" in text) or ("observed shell behavior" in text)

        assert "project-relative paths" in text

        assert "compact structural map" in text

    assert "syntax/collection check" in addon

    assert "pytest collection checks" in round2





def test_environment_prompts_keep_env_staging_paths_out_of_final_deliverables():

    from app.core.environment_prompt import environment_prompt_addon, environment_round2_system_prompt

    from app.core.runtime_mode import EnvironmentContext, runtime_context



    env = EnvironmentContext(

        root_dir=str(Path.cwd()),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        addon = environment_prompt_addon()

        round2 = environment_round2_system_prompt()["content"]



    for text in (addon, round2):

        assert "project-relative paths without `_env/`" in text

        assert "internal staging path" in text





def test_environment_prompts_delegate_long_file_authoring_from_main_thread():

    from app.core.environment_prompt import environment_prompt_addon, environment_round2_system_prompt

    from app.core.runtime_mode import EnvironmentContext, runtime_context



    env = EnvironmentContext(

        root_dir=str(Path.cwd()),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        addon = environment_prompt_addon()

        round2 = environment_round2_system_prompt()["content"]



    for text in (addon, round2):

        lower = text.lower()

        assert "substantially rewritten source, test, script" in text

        assert "delegate" in text

        assert "keep long file bodies out of main-thread tool calls" in lower





def test_environment_prompts_delegate_project_contract_authoring():

    from app.core.environment_prompt import environment_prompt_addon, environment_round2_system_prompt

    from app.core.runtime_mode import EnvironmentContext, runtime_context



    env = EnvironmentContext(

        root_dir=str(Path.cwd()),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        addon = environment_prompt_addon()

        round2 = environment_round2_system_prompt()["content"]



    assert "delegate file authoring to the matching helper" in addon

    assert "Keep long file bodies out of main-thread tool calls" in addon

    assert "substantially rewritten source" in round2 or "project contracts" in round2





def test_environment_maintenance_quality_flags_internal_env_staging_path():

    from stress_tools.run_environment_maintenance import environment_quality_fail_reasons



    reasons = environment_quality_fail_reasons("修改了 `_env/src/taskboard/cli.py`，验证通过。")

    assert "internal_env_staging_path_in_final_reply" in reasons







@pytest.mark.asyncio

async def test_environment_run_python_code_uses_system_temp_not_project(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    (project / "a.py").write_text("print('a')\n", encoding="utf-8")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_pycode",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    code = (

        "from pathlib import Path\n"

        "root=Path.cwd()\n"

        "print('cwd=' + root.name)\n"

        "print('py_files=' + str(len(list(root.rglob('*.py')))))\n"

    )

    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_run",

            str(workspace),

            {"python_code": code, "timeout_sec": 5},

        )

    result = json.loads(raw)



    assert result["ok"] is True

    assert result["python_code"] is True

    assert result["script_location"] == "system_temp"

    assert result["script_deleted"] is True

    assert "cwd=project" in result["stdout"]

    assert "py_files=1" in result["stdout"]

    assert not list(project.glob("env_run_*.py"))

    assert not list(project.glob("*stats*.py"))





@pytest.mark.asyncio

async def test_environment_run_python_code_preserves_unicode_stdout(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    (project / "P1-P3场景词汇.pdf").write_bytes(b"pdf")

    (project / "微信图片_20260508193433.jpg").write_bytes(b"jpg")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_pycode_utf8",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    code = (

        "from pathlib import Path\n"

        "for p in sorted(Path.cwd().iterdir()):\n"

        "    print(p.name)\n"

    )

    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_run",

            str(workspace),

            {"python_code": code, "timeout_sec": 5},

        )

    result = json.loads(raw)



    assert result["ok"] is True

    assert " -X utf8 " in result["command"]

    assert "P1-P3场景词汇.pdf" in result["stdout"]

    assert "微信图片_20260508193433.jpg" in result["stdout"]

    assert "鍦烘櫙" not in result["stdout"]

    assert "\u5bf0\ue1bb\u4fca" not in result["stdout"]





@pytest.mark.asyncio

async def test_environment_read_file_miss_redirects_to_env_read(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools import registry



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    (project / "main.py").write_text("print('project')\n", encoding="utf-8")



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        raw = await registry._handle_read_file(str(workspace), {"path": "main.py"})

    result = json.loads(raw)



    assert result["ok"] is True

    assert result["path"] == "main.py"

    assert result["_redirected_from"] == "read_file"

    assert "print('project')" in result["content"]

    assert "env_read/env_search/env_list_tree/env_run" in result["_next_action_instruction"]





@pytest.mark.asyncio

async def test_environment_read_file_missing_env_copy_stays_staged_scoped(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools import registry



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    (project / "main.py").write_text("print('project')\n", encoding="utf-8")



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        raw = await registry._handle_read_file(str(workspace), {"path": "_env/main.py"})

    result = json.loads(raw)



    assert result["ok"] is False

    assert "does not exist" in result["error"] or "file not found" in result["error"]

    assert "_redirected_from" not in result





@pytest.mark.asyncio

async def test_environment_read_file_existing_env_copy_reads_workspace_copy(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools import registry



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    (project / "main.py").write_text("print('project')\n", encoding="utf-8")

    staged = workspace / "_env" / "main.py"

    staged.parent.mkdir(parents=True)

    staged.write_text("print('staged')\n", encoding="utf-8")



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        raw = await registry._handle_read_file(str(workspace), {"path": "_env/main.py"})

    result = json.loads(raw)



    assert result["ok"] is True

    assert result["path"] == "_env/main.py"

    assert "_redirected_from" not in result

    assert "print('staged')" in result["content"]





@pytest.mark.asyncio

async def test_environment_read_file_staged_root_is_directory_error(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools import registry



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        raw = await registry._handle_read_file(str(workspace), {"path": "_env/"})

    result = json.loads(raw)



    assert result["ok"] is False

    assert result["error"] == "path_is_directory_or_missing_staged_root"

    assert result["path_zone"] == "staged_root"

    assert "env_list_tree" in result["suggested_tools"]





@pytest.mark.asyncio

async def test_environment_code_index_miss_redirects_project_path_to_env_read(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools import registry



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    target = project / "app" / "main.py"

    target.parent.mkdir()

    target.write_text("def main():\n    return 'ok'\n", encoding="utf-8")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )



    with runtime_context("environment", env):

        raw = await registry._handle_code_index(str(workspace), {"path": "app/main.py"})

    result = json.loads(raw)



    assert result["ok"] is True

    assert result["_redirected_from"] == "code_index"

    assert result["path"] == "app/main.py"

    assert "def main" in result["content"]

    assert "env_read/env_search/env_list_tree/env_run" in result["_next_action_instruction"]





@pytest.mark.asyncio

async def test_environment_code_index_unfetched_env_path_stays_staged_scoped(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools import registry



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    target = project / "app" / "main.py"

    target.parent.mkdir()

    target.write_text("def main():\n    return 'ok'\n", encoding="utf-8")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )



    with runtime_context("environment", env):

        raw = await registry._handle_code_index(str(workspace), {"path": "_env/app/main.py"})

    result = json.loads(raw)



    assert result["ok"] is False

    assert "does not exist" in result["error"] or "file not found" in result["error"]

    assert "_redirected_from" not in result





@pytest.mark.asyncio

async def test_environment_code_index_absolute_project_path_redirects_to_env_read(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools import registry



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    target = project / "app" / "main.py"

    target.parent.mkdir()

    target.write_text("def main():\n    return 'ok'\n", encoding="utf-8")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )



    with runtime_context("environment", env):

        raw = await registry._handle_code_index(str(workspace), {"path": str(target)})

    result = json.loads(raw)



    assert result["ok"] is True

    assert result["_redirected_from"] == "code_index"

    assert result["_original_workspace_path"] == str(target)

    assert result["path"] == "app/main.py"

    assert "def main" in result["content"]

    assert "absolute path must be inside this sandbox" not in result.get("error", "")





@pytest.mark.asyncio

async def test_environment_search_in_file_miss_redirects_project_path_to_env_search(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools import registry



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    target = project / "src" / "feature.py"

    target.parent.mkdir()

    target.write_text("class Feature:\n    pass\n", encoding="utf-8")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )



    with runtime_context("environment", env):

        raw = await registry._handle_search_in_file(

            str(workspace),

            {"path": "src/feature.py", "pattern": "Feature"},

        )

    result = json.loads(raw)



    assert result["ok"] is True

    assert result["_redirected_from"] == "search_in_file"

    assert result["matches"][0]["path"] == "src/feature.py"

    assert "env_read/env_search/env_list_tree/env_run" in result["_next_action_instruction"]





@pytest.mark.asyncio

async def test_environment_search_unfetched_env_path_stays_staged_scoped(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools import registry



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    target = project / "src" / "feature.py"

    target.parent.mkdir()

    target.write_text("class Feature:\n    pass\n", encoding="utf-8")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )



    with runtime_context("environment", env):

        raw = await registry._handle_search_in_file(

            str(workspace),

            {"path": "_env/src/feature.py", "pattern": "Feature"},

        )

    result = json.loads(raw)



    assert result["ok"] is False

    assert "file not found" in result["error"]

    assert "_redirected_from" not in result





@pytest.mark.asyncio

async def test_environment_inspect_file_project_path_redirects_to_env_read(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools import registry



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    target = project / "app" / "main.py"

    target.parent.mkdir()

    target.write_text("def main():\n    return 'ok'\n", encoding="utf-8")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )



    with runtime_context("environment", env):

        raw = await registry._handle_inspect_file(str(workspace), {"path": "app/main.py"})

    result = json.loads(raw)



    assert result["ok"] is True

    assert result["_redirected_from"] == "inspect_file"

    assert result["path"] == "app/main.py"

    assert "def main" in result["content"]





@pytest.mark.asyncio

async def test_environment_inspect_file_unfetched_env_path_auto_stages_for_readonly(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools import registry



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    target = project / "app" / "main.py"

    target.parent.mkdir()

    target.write_text("def main():\n    return 'ok'\n", encoding="utf-8")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )



    with runtime_context("environment", env):

        raw = await registry._handle_inspect_file(str(workspace), {"path": "_env/app/main.py"})

    result = json.loads(raw)



    assert result["ok"] is True

    assert result["_redirected_from"] == "inspect_file_auto_env_fetch"

    assert result["_env_fetch"]["path"] == "app/main.py"

    assert (workspace / "_env" / "app" / "main.py").is_file()





@pytest.mark.asyncio

async def test_environment_list_tree_truncated_warns_not_for_rankings(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    for idx in range(5):

        (project / f"file_{idx}.py").write_text("print('x')\n", encoding="utf-8")



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_tree_truncated",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_list_tree",

            str(workspace),

            {"path": ".", "max_depth": 1, "limit": 2},

        )

    result = json.loads(raw)



    assert result["ok"] is True

    assert result["truncated"] is True

    assert result["incomplete"] is True

    assert "partial project listing" in result["next_action_instruction"]

    assert "largest/smallest/count/ranking/statistics" in result["next_action_instruction"]

    assert "env_run" in result["next_action_instruction"]





@pytest.mark.asyncio

async def test_environment_list_tree_slash_means_project_root(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    (project / "README.md").write_text("# Project\n", encoding="utf-8")



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_tree_root",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_list_tree",

            str(workspace),

            {"path": "/", "max_depth": 1, "limit": 20},

        )

    result = json.loads(raw)



    assert result["ok"] is True

    assert result["root"] == "."

    assert any(item["path"] == "README.md" for item in result["items"])





@pytest.mark.asyncio

async def test_environment_list_tree_many_mixed_materials_suggests_inventory(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    for name in (

        "writing.txt",

        "speaking.html",

        "vocab.docx",

        "listening.pdf",

        "reading.md",

        "chart.png",

        "audio.mp3",

        "archive.zip",

    ):

        (project / name).write_text("sample\n", encoding="utf-8")



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_tree_hint",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_list_tree",

            str(workspace),

            {"path": ".", "max_depth": 1, "limit": 50},

        )

    result = json.loads(raw)



    assert result["ok"] is True

    assert result["truncated"] is False

    assert "next_action_instruction" in result

    assert "kind='inventory'" in result["next_action_instruction"]

    assert "first-pass inventory" in result["next_action_instruction"]





@pytest.mark.asyncio

async def test_environment_list_tree_many_plain_files_suggests_inventory(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    for idx in range(30):

        (project / f"note_{idx:02d}.txt").write_text("sample\n", encoding="utf-8")



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_tree_many_files",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_list_tree",

            str(workspace),

            {"path": ".", "max_depth": 1, "limit": 80},

        )

    result = json.loads(raw)



    assert result["ok"] is True

    assert result["truncated"] is False

    assert "kind='inventory'" in result["next_action_instruction"]

    assert "advanced directory tree/project inventory" in result["next_action_instruction"]





@pytest.mark.asyncio

async def test_environment_list_tree_complex_project_suggests_inventory(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    (project / "src" / "pkg").mkdir(parents=True)

    (project / "tests").mkdir(parents=True)

    workspace.mkdir()

    for name in ("README.md", "pyproject.toml", "src/pkg/__init__.py", "src/pkg/app.py", "src/pkg/api.py", "src/pkg/models.py", "src/pkg/utils.py", "tests/test_app.py"):

        target = project / name

        target.write_text("sample\n", encoding="utf-8")



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_tree_project",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_list_tree",

            str(workspace),

            {"path": ".", "max_depth": 3, "limit": 80},

        )

    result = json.loads(raw)



    assert result["ok"] is True

    assert "kind='inventory'" in result["next_action_instruction"]

    assert "README/docs" in result["next_action_instruction"]

    assert "config/build/test hints" in result["next_action_instruction"]





@pytest.mark.asyncio

async def test_environment_list_tree_small_simple_directory_does_not_suggest_inventory(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    (project / "one.txt").write_text("sample\n", encoding="utf-8")

    (project / "two.txt").write_text("sample\n", encoding="utf-8")



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_tree_small",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_list_tree",

            str(workspace),

            {"path": ".", "max_depth": 1, "limit": 20},

        )

    result = json.loads(raw)



    assert result["ok"] is True

    assert "next_action_instruction" not in result





def test_environment_persona_declares_identity_and_directory_workflow():

    text = (Path(__file__).parent.parent / "personas" / "environment.md").read_text(encoding="utf-8")



    assert "name: bot" in text

    assert '你是名为"bot"' in text

    assert "You are bot" in text

    assert "local project maintenance and engineering agent" in text

    assert "current project directory" in text

    assert "chat workspace is separate" in text

    assert "what was completed" in text

    assert "Identity, persona, permission boundaries" in text
    assert "those settings stay fixed here" in text





def test_environment_round2_prompt_mentions_light_project_helpers():

    from app.core.environment_prompt import environment_round2_system_prompt

    from app.core.runtime_mode import EnvironmentContext, runtime_context



    env = EnvironmentContext(

        root_dir=str(Path.cwd()),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        prompt = environment_round2_system_prompt()

    assert prompt is not None

    text = prompt["content"]

    assert "project_map" in text

    assert "delegate_inventory" in text
    assert "inventory, summarize" not in text

    assert "starting with inventory" in text

    assert "whole-directory questions" in text

    assert "inventory as first-pass directory evidence" in text

    assert "main thread light" in text

    assert "exact missing target" in text

    assert "acceptance checklist" in text

    assert "Project file paths are evidence, not guesses" in text

    assert "top-N questions" in text

    assert "docs/implementation consistency" in text

    assert "confirmed absent" in text

    assert "run project pytest from the project's own root" in text

    assert "Apply completed verified slices to project paths before waiting for every helper" in text

    assert "coherent partial milestones" in text

    assert "已验证切片先应用到项目路径" in text

    assert "Validation claims must name the exact command and environment used" in text

    assert "requirements dependency is not installed" in text

    assert "Rewrite `_env/`, helper sandboxes, trace IDs" in text





def test_environment_prompt_requires_incremental_greenfield_milestones():

    from app.core.environment_prompt import environment_prompt_addon

    from app.core.runtime_mode import EnvironmentContext, runtime_context



    env = EnvironmentContext(

        root_dir=str(Path.cwd()),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        text = environment_prompt_addon()



    assert "Apply completed and verified slices to project paths at milestone boundaries" in text

    assert "coherent partial milestone" in text

    assert "scaffold/contract, core module, UI or CLI surface" in text

    assert "vertical slices" in text

    assert "runnable core, one UI/CLI path" in text

    assert "one code helper" in text

    assert "Keep the first runnable slice cohesive enough" in text

    assert "resume the owning helper with the concrete failing command/output" in text

    assert "已验证切片先应用到真实项目目录" in text

    assert "Validation claims must name the exact command and environment used" in text

    assert "User-facing replies should describe project paths and commands" in text

    assert "附件、_env 或 helper 笔记属于证据或暂存" in text





def test_environment_file_not_found_response_redirects_project_paths_to_env_tools(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.workspace import _file_not_found_response



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )



    with runtime_context("environment", env):

        result = _file_not_found_response(str(workspace), "app/backend/system_state_v2.py")



    assert result["ok"] is False

    assert "project_path" in result["error"]

    assert "fetch_to_temp(source='main'" in result["error"]

    assert "request_resource" in result["error"]

    assert "_env/.resource_manifest.json" in result["error"]





def test_app_clone_quality_flags_preparation_only_implementation_reply():

    from stress_tools.run_app_clone_maintenance import evaluate_response_quality



    quality = evaluate_response_quality(

        2,

        "好的，我先确认当前项目结构和现有测试情况，再动手。跑一下目录结构和找到相关文件。",

        {},

    )



    assert quality["ok"] is False

    assert "implementation_reply_only_preparation" in quality["issues"]





def test_round2_upgrade_regression_guard_keeps_better_prior_plan():

    from app.core.orchestrator_entry import _choose_non_regressed_plan

    from app.schemas.api import ResponsePlan



    prior = ResponsePlan(

        intent="对 app 备份工程做只读架构审查",

        key_points=[

            "候选 1: app/llm/tools/delegate.py (193,686 B) — delegate 动作 handler 多且拆分风险高",

            "候选 2: app/llm/client_tools_loop.py (166,927 B) — 主循环驱动层，工具调用调度复杂",

            "候选 3: app/core/orchestrator.py (146,274 B) — 核心编排器，跨模块依赖多",

            "拆分风险：循环引用、导出兼容和共享数据结构提炼",

        ],

        tone="严谨克制",

        length_hint="长",

        upgrade_to_hard=True,

    )

    regressed = ResponsePlan(

        intent="read_multiple_files",

        key_points=["env_read(path='app/llm/tools/delegate.py', lines=[1,80])"],

        tone="自然平和",

        length_hint="中",

    )



    selected = _choose_non_regressed_plan(prior, regressed)



    assert selected is prior

    assert selected.upgrade_to_hard is False





def test_preparatory_plan_after_substantial_work_requests_upgrade():

    from app.core.orchestrator import _should_upgrade_preparatory_after_work

    from app.schemas.api import ResponsePlan



    plan = ResponsePlan(

        intent="我先检查当前测试结构，然后添加测试并运行 pytest",

        key_points=["先查看 tests 目录", "下一步补充测试"],

        tone="自然",

        length_hint="短",

    )



    should_upgrade, reason = _should_upgrade_preparatory_after_work(

        plan,

        {"iter_count": 34},

        user_message="增加测试，运行 pytest 并修复问题",

        helper_excerpts={"env_tool_tests": "added tests and ran pytest"},

        main_tool_results={},

        final_msgs=[{"role": "tool", "content": "{}"} for _ in range(12)],

    )



    assert should_upgrade is True

    assert "preparatory plan after substantial work" in reason





def test_preparatory_plan_without_work_does_not_request_upgrade():

    from app.core.orchestrator import _should_upgrade_preparatory_after_work

    from app.schemas.api import ResponsePlan



    plan = ResponsePlan(

        intent="我先检查当前测试结构",

        key_points=["下一步补充测试"],

        tone="自然",

        length_hint="短",

    )



    should_upgrade, _reason = _should_upgrade_preparatory_after_work(

        plan,

        {"iter_count": 1},

        user_message="增加测试",

        helper_excerpts={},

        main_tool_results={},

        final_msgs=[],

    )



    assert should_upgrade is False





def test_complex_plan_orientation_only_requests_continuation_for_greenfield():

    from app.core.orchestrator import _should_continue_incomplete_complex_plan

    from app.schemas.api import ResponsePlan



    plan = ResponsePlan(

        intent="创建一个多语言 agent 工程的架构蓝图",

        key_points=["已输出 ARCHITECTURE.md，下一步实现 CLI、服务端和测试"],

        tone="自然",

        length_hint="中",

        deliverables=["ARCHITECTURE.md"],

    )



    should_continue, reason = _should_continue_incomplete_complex_plan(

        plan,

        user_message="从空目录构建一个完整复杂工程，实现一个简单 agent，包含多语言混合和自测",

        helper_excerpts={"arch": "partial scaffold only; implementation not yet written"},

        main_tool_results={},

        final_msgs=[{"role": "tool", "content": "{}"} for _ in range(6)],

    )



    assert should_continue is True

    assert "implementation" in reason or "partial" in reason





def test_complex_plan_orientation_only_requests_continuation_for_long_report():

    from app.core.orchestrator import _should_continue_incomplete_complex_plan

    from app.schemas.api import ResponsePlan



    plan = ResponsePlan(

        intent="整理五月雅思材料索引",

        key_points=["已生成材料索引，部分 PDF 和图片尚未完整读取"],

        tone="自然",

        length_hint="中",

        deliverables=["ielts_material_index.md"],

    )



    should_continue, reason = _should_continue_incomplete_complex_plan(

        plan,

        user_message="分析五月雅思全部材料，生成学习报告和四周计划",

        helper_excerpts={"read": "PARTIAL: large PDFs and images remain unread"},

        main_tool_results={},

        final_msgs=[{"role": "tool", "content": "{}"} for _ in range(5)],

    )



    assert should_continue is True

    assert "report" in reason or "partial" in reason





def test_complex_plan_orientation_detector_does_not_affect_concept_answer():

    from app.core.orchestrator import _should_continue_incomplete_complex_plan

    from app.schemas.api import ResponsePlan



    plan = ResponsePlan(

        intent="解释架构图的作用",

        key_points=["架构图用于表达模块和依赖关系"],

        tone="自然",

        length_hint="短",

    )



    should_continue, _reason = _should_continue_incomplete_complex_plan(

        plan,

        user_message="架构图是什么",

        helper_excerpts={},

        main_tool_results={},

        final_msgs=[],

    )



    assert should_continue is False





def test_complex_plan_verified_inventory_does_not_upgrade_from_harness_wording():

    from app.core.orchestrator import _should_continue_incomplete_complex_plan

    from app.schemas.api import ResponsePlan



    plan = ResponsePlan(

        intent="Full recursive traversal of app/ using os.walk, count Python files, and list largest files.",

        key_points=[

            "Total Python files under app/: 186",

            "Used os.walk for a full traversal and avoided relying on a truncated directory tree.",

        ],

        tone="rigorous",

        length_hint="short",

    )



    should_continue, _reason = _should_continue_incomplete_complex_plan(

        plan,

        user_message=(

            "Read the isolated app clone's app/ directory with a full recursive traversal. "

            "Count Python files under app/ only, list the 12 largest files, and report only verified facts. "

            "Use helpers for broad reading or implementation and verify concrete command output."

        ),

        helper_excerpts={},

        main_tool_results={"env_run": "Total Python files under app/: 186"},

        final_msgs=[{"role": "tool", "content": "{}"} for _ in range(4)],

    )



    assert should_continue is False





def test_app_clone_quality_flags_preparation_only_analysis_reply():

    from stress_tools.run_app_clone_maintenance import evaluate_response_quality



    quality = evaluate_response_quality(

        1,

        "我来做一次只读架构审查。先读取这些大文件的头部和结构，看看真实的代码组织情况。",

        {},

    )



    assert quality["ok"] is False

    assert "analysis_reply_only_preparation" in quality["issues"]





def test_app_clone_quality_flags_failed_validation_that_needs_continue():

    from stress_tools.run_app_clone_maintenance import evaluate_response_quality



    quality = evaluate_response_quality(

        2,

        "pytest 跑起来了，但 5 个测试全部 FAILED，验证没通过。需要先修这些 KeyError，要我接着修吗？",

        {},

    )



    assert quality["ok"] is False

    assert "implementation_validation_failed_needs_continue" in quality["issues"]





def test_app_clone_quality_flags_false_missing_environment_code_blocker():

    from stress_tools.run_app_clone_maintenance import evaluate_response_quality



    quality = evaluate_response_quality(

        2,

        "这个备份工程里没有 environment 相关代码，也没有对应的测试目录。",

        {"has_environment": True},

    )



    assert quality["ok"] is False

    assert "false_missing_environment_code_blocker" in quality["issues"]





def test_app_clone_quality_flags_nonexistent_architecture_directories():

    from stress_tools.run_app_clone_maintenance import evaluate_response_quality



    quality = evaluate_response_quality(

        1,

        "整体职责分为：`core/`、`llm/`、`web/`、`agent/`，其中 `app/api/` 提供接口。",

        {"app_top_dirs": ["api", "core", "db", "llm", "memory", "schemas", "tests"]},

    )



    assert quality["ok"] is False

    assert any(issue == "nonexistent_top_level_directory_claim:agent,web" for issue in quality["issues"])





def test_app_clone_quality_accepts_observed_architecture_directories():

    from stress_tools.run_app_clone_maintenance import evaluate_response_quality



    quality = evaluate_response_quality(

        1,

        "观察到的顶层结构是：`api/`、`core/`、`llm/`、`memory/`、`app/tests/`；函数包括 `_detect_*`。",

        {"app_top_dirs": ["api", "core", "db", "llm", "memory", "schemas", "tests"]},

    )



    assert not any(issue.startswith("nonexistent_top_level_directory_claim") for issue in quality["issues"])





def test_environment_maintenance_review_prompt_is_actionable(tmp_path):

    from stress_tools.run_environment_maintenance import ProjectState, next_message



    project = tmp_path / "project_01_snake_arcade"

    project.mkdir()

    state = ProjectState(project_id=1, path=project, stage=1, turn=3)

    state.last_validation = {

        "ok": False,

        "checks": {"required_hits": {"load_scores": True, "save_scores": False}},

    }



    message = next_message(state)



    assert "actionable patch request" in message

    assert "save_scores" in message

    assert "exact missing target names" in message

    assert "grep/search" in message





def test_environment_group_id_is_filesystem_safe():

    from app.core.environment_projects import safe_group_id



    group_id = safe_group_id("user:with/bad\\chars")

    assert group_id.startswith("env_user_")

    assert ":" not in group_id

    assert "/" not in group_id

    assert "\\" not in group_id





@pytest.mark.asyncio

async def test_environment_tools_fetch_diff_apply_and_hash_conflict(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    source = project / "hello.txt"

    source.write_text("one\n", encoding="utf-8")



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        fetched = json.loads(await handle_environment_tool("env_fetch", str(workspace), {"path": "hello.txt"}))

        copy_path = workspace / fetched["workspace_path"]

        copy_path.write_text("two\n", encoding="utf-8")

        diff = json.loads(await handle_environment_tool("env_diff", str(workspace), {"path": "hello.txt"}))

        assert diff["ok"] is True

        assert "-one" in diff["diff"]

        assert "+two" in diff["diff"]



        source.write_text("external\n", encoding="utf-8")

        conflict = json.loads(await handle_environment_tool(

            "env_apply_replace",

            str(workspace),

            {"path": "hello.txt", "expected_hash": fetched["sha256"]},

        ))

        assert conflict["ok"] is False

        assert "changed since env_fetch" in conflict["error"]



        fetched2 = json.loads(await handle_environment_tool("env_fetch", str(workspace), {"path": "hello.txt"}))

        copy_path = workspace / fetched2["workspace_path"]

        copy_path.write_text("final\n", encoding="utf-8")

        applied = json.loads(await handle_environment_tool(

            "env_apply_replace",

            str(workspace),

            {"path": "hello.txt", "expected_hash": fetched2["sha256"]},

        ))

        assert applied["ok"] is True

        assert source.read_text(encoding="utf-8") == "final\n"

        assert applied["backup_project_path"] is None

        backup_path = workspace / applied["backup_workspace_path"]

        assert backup_path.exists()

        assert backup_path.read_text(encoding="utf-8") == "external\n"

        assert not (project / ".env_backups").exists()





@pytest.mark.asyncio

async def test_environment_fetch_missing_path_returns_exact_candidates(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    (project / "wx2").mkdir(parents=True)

    workspace.mkdir()

    (project / "wx2" / "包涵.docx").write_bytes(b"docx")



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        result = json.loads(await handle_environment_tool("env_fetch", str(workspace), {"path": "file/包涵.docx"}))



    assert result["ok"] is False

    assert result["candidates"][0]["path"] == "wx2/包涵.docx"

    assert "retry env_fetch" in result["next_action"]





@pytest.mark.asyncio

async def test_environment_fetch_large_binary_returns_read_strategy(tmp_path, monkeypatch):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools import environment as env_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    pdf = project / "large.pdf"

    pdf.write_bytes(b"%PDF-1.7\n")

    monkeypatch.setattr(env_tool, "MAX_FETCH_BYTES", 4)



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        result = json.loads(await env_tool.handle_environment_tool("env_fetch", str(workspace), {"path": "large.pdf"}))



    assert result["ok"] is False

    assert result["path"] == "large.pdf"

    assert result["category"] == "office_pdf"

    assert result["suggested_helper_kind"] == "read"

    assert "Use env_run for metadata or targeted extraction" in result["next_action"]

    assert any("batch pages" in item for item in result["suggested_actions"])





@pytest.mark.asyncio

async def test_environment_read_paginates_large_text_by_default(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    project.mkdir()

    text_file = project / "large.txt"

    text_file.write_text("\n".join("x" * 200 for _ in range(300)), encoding="utf-8")



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        result = json.loads(await handle_environment_tool("env_read", str(tmp_path), {"path": "large.txt"}))



    assert result["ok"] is True

    assert result["truncated"] is True

    assert result["end_line"] < result["total_lines"]

    assert result["next_start_line"] == result["end_line"] + 1

    assert "read helper" in result["note"]





@pytest.mark.asyncio

async def test_environment_tool_rejects_path_escape(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    project.mkdir()

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        result = json.loads(await handle_environment_tool("env_read", str(tmp_path), {"path": "../secret.txt"}))

    assert result["ok"] is False

    assert "escapes environment root" in result["error"]





@pytest.mark.asyncio

async def test_environment_stream_maps_project_and_sets_runtime(monkeypatch, tmp_path):

    from app.api import environment as api

    from app.api import chat as chat_api

    from app.core.runtime_mode import current_environment, current_runtime_mode

    from app.schemas.api import EnvironmentChatRequest



    project = tmp_path / "project"

    project.mkdir()

    seen = {}



    async def fake_resolve_environment_project(**kwargs):

        return {

            "archive_id": "arch_env",

            "group_id": "env_user_u",

            "user_id": "u",

            "root_dir": str(project),

            "project_key": "pk",

            "project_name": "project",

        }



    async def fake_ensure_persona(archive_id, persona_id):

        seen["persona"] = (archive_id, persona_id)



    async def fake_orchestrate(req, trace_id, **kwargs):

        seen["mode"] = current_runtime_mode()

        env = current_environment()

        seen["env_root"] = env.root_dir if env else ""

        yield "token", {"text": "ok"}

        yield "done", {"trace_id": trace_id, "text": "ok"}

        yield "complete", {"trace_id": trace_id}



    class Guard:

        async def acquire(self, *args, **kwargs):

            seen["acquire"] = args



        async def release(self, *args, **kwargs):

            seen["release"] = args

            return True



    monkeypatch.setattr(chat_api, "resolve_environment_project", fake_resolve_environment_project)

    monkeypatch.setattr(chat_api, "_ensure_environment_persona", fake_ensure_persona)

    monkeypatch.setattr(chat_api, "orchestrate", fake_orchestrate)

    monkeypatch.setattr(chat_api, "get_group_guard", lambda: Guard())

    chat_api._idempotency_cache.clear()



    req = EnvironmentChatRequest(user_id="u", message="hello", current_dir=str(project))

    resp = await api.environment_stream(req)

    events = []

    async for item in resp.body_iterator:

        events.append((item["event"], json.loads(item["data"])))



    assert [event for event, _ in events] == ["meta", "token", "done", "complete"]

    assert seen["mode"] == "environment"

    assert seen["env_root"] == str(project)

    assert seen["persona"] == ("arch_env", "environment")

    assert seen["acquire"][0:3] == ("arch_env", "env_user_u", "u")

    assert seen["release"][0:3] == ("arch_env", "env_user_u", "u")





@pytest.mark.asyncio

async def test_chat_stream_current_dir_enters_environment_runtime(monkeypatch, tmp_path):

    from app.api import chat as chat_api

    from app.core.runtime_mode import current_environment, current_runtime_mode

    from app.schemas.api import ChatRequest



    project = tmp_path / "project"

    project.mkdir()

    seen = {}



    async def fake_resolve_environment_project(**kwargs):

        return {

            "archive_id": "arch_env_chat",

            "group_id": "env_user_u",

            "user_id": "u",

            "root_dir": str(project),

            "project_key": "pk",

            "project_name": "project",

        }



    async def fake_ensure_persona(archive_id, persona_id):

        seen["persona"] = (archive_id, persona_id)



    async def fake_orchestrate(req, trace_id, **kwargs):

        seen["mode"] = current_runtime_mode()

        env = current_environment()

        seen["env_root"] = env.root_dir if env else ""

        seen["req_ids"] = (req.archive_id, req.group_id, req.user_id)

        yield "token", {"text": "ok"}

        yield "done", {"trace_id": trace_id, "text": "ok"}

        yield "complete", {"trace_id": trace_id}



    class Guard:

        async def acquire(self, *args, **kwargs):

            seen["acquire"] = args



        async def release(self, *args, **kwargs):

            seen["release"] = args

            return True



    monkeypatch.setattr(chat_api, "resolve_environment_project", fake_resolve_environment_project)

    monkeypatch.setattr(chat_api, "_ensure_environment_persona", fake_ensure_persona)

    monkeypatch.setattr(chat_api, "orchestrate", fake_orchestrate)

    monkeypatch.setattr(chat_api, "get_group_guard", lambda: Guard())

    chat_api._idempotency_cache.clear()



    req = ChatRequest(user_id="u", message="hello", current_dir=str(project))

    resp = await chat_api.chat_stream(req)

    events = []

    async for item in resp.body_iterator:

        events.append((item["event"], json.loads(item["data"])))



    assert [event for event, _ in events] == ["meta", "token", "done", "complete"]

    assert seen["mode"] == "environment"

    assert seen["env_root"] == str(project)

    assert seen["req_ids"] == ("arch_env_chat", "env_user_u", "u")

    assert seen["persona"] == ("arch_env_chat", "environment")

    assert seen["acquire"][0:3] == ("arch_env_chat", "env_user_u", "u")





@pytest.mark.asyncio

async def test_environment_run_uses_command_risk_policy(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        result = json.loads(await handle_environment_tool(

            "env_run",

            str(workspace),

            {"command": "powershell -Command Get-Process", "timeout_sec": 5},

        ))

    assert result["ok"] is False

    assert result["category"] == "blocked_keyword"





@pytest.mark.asyncio

async def test_environment_run_env_staging_path_failure_gets_project_path_hint(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    (project / "real.txt").write_text("project file\n", encoding="utf-8")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        result = json.loads(await handle_environment_tool(

            "env_run",

            str(workspace),

            {

                "python_code": "from pathlib import Path\nprint(Path('_env/real.txt').read_text())",

                "timeout_sec": 5,

            },

        ))



    assert result["ok"] is False

    assert "FIX_HINT" in result

    assert "env_run executes with cwd set to the real project directory" in result["FIX_HINT"]

    assert "project-relative paths" in result["FIX_HINT"]

    assert "read_file, edit_file, multi_edit" in result["FIX_HINT"]





@pytest.mark.asyncio

async def test_environment_interrupt_active_and_abort(monkeypatch):

    from app.api import environment as api

    from app.schemas.api import InterruptMessageRequest



    signaled = {}



    class Guard:

        async def is_busy(self, archive_id, group_id, user_id):

            return group_id == "env_user_u"



        async def active_holders(self):

            return {

                ("arch_env", "env_user_u", "u"): "trace1",

                ("arch_chat", "group", "u"): "trace2",

            }



        async def signal_abort(self, **kwargs):

            signaled.update(kwargs)

            return True



    monkeypatch.setattr(api, "get_group_guard", lambda: Guard())

    api._interrupt_messages.clear()



    ok = await api.interrupt_message(InterruptMessageRequest(

        archive_id="arch_env",

        group_id="env_user_u",

        user_id="u",

        message="停一下",

    ))

    assert ok == {"ok": True}

    assert api._pop_interrupt_messages("arch_env", "env_user_u", "u") == ["停一下"]



    active = await api.list_active()

    assert active["items"] == [{

        "archive_id": "arch_env",

        "group_id": "env_user_u",

        "user_id": "u",

        "trace_id": "trace1",

    }]



    aborted = await api.abort_environment({

        "archive_id": "arch_env",

        "group_id": "env_user_u",

        "user_id": "u",

    })

    assert aborted == {"ok": True}

    assert signaled == {"archive_id": "arch_env", "group_id": "env_user_u", "user_id": "u"}





@pytest.mark.asyncio

async def test_environment_monitor_stream_receives_command_events(tmp_path):

    import asyncio

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool

    from app.api.environment import monitor_environment



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_monitor",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    resp = await monitor_environment(archive_id="arch_test_monitor", heartbeat_sec=1)

    iterator = resp.body_iterator.__aiter__()

    first = await iterator.__anext__()

    assert first["event"] == "snapshot"



    async def run_cmd():

        with runtime_context("environment", env):

            return json.loads(await handle_environment_tool(

                "env_run",

                str(workspace),

                {"command": "python -c \"print(123)\"", "timeout_sec": 10},

            ))



    task = asyncio.create_task(run_cmd())

    seen = []

    deadline = asyncio.get_running_loop().time() + 10

    while asyncio.get_running_loop().time() < deadline and "done" not in seen:

        item = await iterator.__anext__()

        if item["event"] == "command":

            payload = json.loads(item["data"])

            seen.append(payload.get("kind"))

    result = await task

    await iterator.aclose()



    assert result["ok"] is True

    assert "start" in seen

    assert "done" in seen





@pytest.mark.asyncio

async def test_environment_tool_workflow_events_include_tenant_fields(tmp_path):

    import asyncio

    import json

    from app.core.environment_events import environment_event_sink

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    (project / "README.md").write_text("hello\n", encoding="utf-8")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_tool_events",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    queue: asyncio.Queue = asyncio.Queue()



    with runtime_context("environment", env), environment_event_sink(

        queue,

        archive_id="arch_tool_events",

        group_id="env_user_u",

        user_id="u",

    ):

        result = json.loads(await handle_environment_tool(

            "env_read",

            str(workspace),

            {"path": "README.md"},

        ))



    assert result["ok"] is True

    events = [await queue.get(), await queue.get()]

    for event_name, payload in events:

        assert event_name == "workflow"

        assert payload["archive_id"] == "arch_tool_events"

        assert payload["group_id"] == "env_user_u"

        assert payload["user_id"] == "u"





@pytest.mark.asyncio

async def test_environment_monitor_snapshot_includes_active_helpers(tmp_path):

    import asyncio

    from app.core.core_processes import registry

    from app.core.environment_monitor import monitor



    task = asyncio.create_task(asyncio.sleep(30))

    proc_id = await registry().register_helper(

        owner="main:trace_helper_snapshot",

        task=task,

        helper_task_id="map_project",

        helper_workspace=str(tmp_path),

        abort_event=asyncio.Event(),

        description="project map",

        helper_kind="project_map",

        archive_id="arch_helper_snapshot",

        group_id="env_user_u",

        user_id="u",

    )

    try:

        snap = await monitor.snapshot(archive_id="arch_helper_snapshot", group_id="env_user_u", user_id="u")

    finally:

        task.cancel()

        await registry().unregister(proc_id)

    assert snap["active_helper_count"] >= 1

    helper = next(h for h in snap["active_helpers"] if h["task_id"] == "map_project")

    assert helper["helper_kind"] == "project_map"





@pytest.mark.asyncio

async def test_environment_monitor_snapshot_filters_helpers_by_trace(tmp_path):

    import asyncio

    from app.core.core_processes import registry

    from app.core.environment_monitor import monitor



    task_a = asyncio.create_task(asyncio.sleep(30))

    task_b = asyncio.create_task(asyncio.sleep(30))

    proc_a = await registry().register_helper(

        owner="main:trace_a",

        task=task_a,

        helper_task_id="helper_a",

        helper_workspace=str(tmp_path / "a"),

        abort_event=asyncio.Event(),

        description="helper a",

        helper_kind="project_map",

        archive_id="arch_trace_filter",

        group_id="env_user_u",

        user_id="u",

    )

    proc_b = await registry().register_helper(

        owner="main:trace_b",

        task=task_b,

        helper_task_id="helper_b",

        helper_workspace=str(tmp_path / "b"),

        abort_event=asyncio.Event(),

        description="helper b",

        helper_kind="project_map",

        archive_id="arch_trace_filter",

        group_id="env_user_u",

        user_id="u",

    )

    try:

        snap = await monitor.snapshot(

            archive_id="arch_trace_filter",

            group_id="env_user_u",

            user_id="u",

            trace_id="trace_a",

        )

    finally:

        task_a.cancel()

        task_b.cancel()

        await registry().unregister(proc_a)

        await registry().unregister(proc_b)



    task_ids = {h["task_id"] for h in snap["active_helpers"]}

    assert "helper_a" in task_ids

    assert "helper_b" not in task_ids

    assert next(h for h in snap["active_helpers"] if h["task_id"] == "helper_a")["trace_id"] == "trace_a"





@pytest.mark.asyncio

async def test_environment_monitor_history_filters_missing_trace_when_trace_requested():

    from app.core.environment_monitor import EnvironmentMonitor



    local_monitor = EnvironmentMonitor()

    await local_monitor.publish("workflow", {

        "trace_id": "trace_a",

        "archive_id": "arch_trace_history",

        "group_id": "env_user_u",

        "user_id": "u",

        "message": "keep",

    })

    await local_monitor.publish("workflow", {

        "archive_id": "arch_trace_history",

        "group_id": "env_user_u",

        "user_id": "u",

        "message": "drop_missing_trace",

    })

    await local_monitor.publish("workflow", {

        "trace_id": "trace_b",

        "archive_id": "arch_trace_history",

        "group_id": "env_user_u",

        "user_id": "u",

        "message": "drop_other_trace",

    })



    items = await local_monitor.history(

        archive_id="arch_trace_history",

        group_id="env_user_u",

        user_id="u",

        trace_id="trace_a",

    )



    assert [item["payload"]["message"] for item in items] == ["keep"]





def test_environment_event_sink_routes_background_helper_events_by_tenant():

    import asyncio

    from app.core.environment_events import environment_event_sink, publish_workflow_event



    async def run():

        queue: asyncio.Queue = asyncio.Queue()

        other_queue: asyncio.Queue = asyncio.Queue()

        with environment_event_sink(

            queue,

            archive_id="arch_inline",

            group_id="env_user_u",

            user_id="u",

        ), environment_event_sink(

            other_queue,

            archive_id="arch_other",

            group_id="env_user_other",

            user_id="other",

        ):

            publish_workflow_event({

                "kind": "helper_start",

                "archive_id": "arch_inline",

                "group_id": "env_user_u",

                "user_id": "u",

                "task_id": "map_project",

            })

            return await asyncio.wait_for(queue.get(), timeout=1), other_queue.empty()



    item, other_empty = asyncio.run(run())

    event_name, payload = item

    assert event_name == "workflow"

    assert payload["kind"] == "helper_start"

    assert payload["task_id"] == "map_project"

    assert other_empty is True





def test_environment_event_sink_routes_background_helper_events_by_trace():

    import asyncio

    from app.core.environment_events import environment_event_sink, publish_workflow_event



    async def run():

        queue_a: asyncio.Queue = asyncio.Queue()

        queue_b: asyncio.Queue = asyncio.Queue()

        with environment_event_sink(

            queue_a,

            archive_id="arch_trace_sink",

            group_id="env_user_u",

            user_id="u",

            trace_id="trace_a",

        ), environment_event_sink(

            queue_b,

            archive_id="arch_trace_sink",

            group_id="env_user_u",

            user_id="u",

            trace_id="trace_b",

        ):

            publish_workflow_event({

                "kind": "helper_start",

                "trace_id": "trace_a",

                "archive_id": "arch_trace_sink",

                "group_id": "env_user_u",

                "user_id": "u",

                "task_id": "map_project",

            })

            publish_workflow_event({

                "kind": "helper_start",

                "archive_id": "arch_trace_sink",

                "group_id": "env_user_u",

                "user_id": "u",

                "task_id": "missing_trace",

            })

            return await asyncio.wait_for(queue_a.get(), timeout=1), queue_b.empty()



    item, other_empty = asyncio.run(run())

    event_name, payload = item

    assert event_name == "workflow"

    assert payload["trace_id"] == "trace_a"

    assert other_empty is True





@pytest.mark.asyncio

async def test_environment_active_commands_and_abort(tmp_path):

    import asyncio

    import sys

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool

    from app.api.environment import active_environment_commands, abort_environment_command



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_abort",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )



    async def run_cmd():

        with runtime_context("environment", env):

            return json.loads(await handle_environment_tool(

                "env_run",

                str(workspace),

                {"command": f"{sys.executable} -c \"import time; time.sleep(30)\"", "timeout_sec": 60},

            ))



    task = asyncio.create_task(run_cmd())

    command_id = ""

    for _ in range(50):

        snap = await active_environment_commands(archive_id="arch_test_abort")

        commands = snap.get("active_commands") or []

        if commands:

            command_id = commands[0]["command_id"]

            break

        await asyncio.sleep(0.1)

    assert command_id

    abort = await abort_environment_command(command_id)

    assert abort["ok"] is True

    result = await asyncio.wait_for(task, timeout=10)

    assert result["ok"] is False

    assert result["returncode"] != 0





@pytest.mark.asyncio

async def test_environment_monitor_history_endpoint(monkeypatch):

    from app.api import environment



    async def fake_history(**kwargs):

        return [{"event": "workflow", "payload": {"group_id": kwargs["group_id"]}}]



    monkeypatch.setattr(environment.env_monitor, "history", fake_history)

    result = await environment.environment_monitor_history(group_id="env_user_u", limit=10)

    assert result == {"items": [{"event": "workflow", "payload": {"group_id": "env_user_u"}}]}





@pytest.mark.asyncio

async def test_environment_run_multiline_python_c_is_normalized(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_run_multiline",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    command = 'python -c "\\nprint(123)\\nprint(456)\\n"'

    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_run",

            str(workspace),

            {"command": command, "timeout_sec": 5},

        )

    result = json.loads(raw)



    assert result["ok"] is True

    assert result["normalized_from"] == "python -c"

    assert "123" in result["stdout"]

    assert "456" in result["stdout"]





@pytest.mark.asyncio

async def test_environment_run_python_c_compound_statement_error_gets_fix_hint(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_run_compound_hint",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    command = "python -c \"try: print(1); except Exception: print(2)\""

    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_run",

            str(workspace),

            {"command": command, "timeout_sec": 5},

        )

    result = json.loads(raw)



    assert result["ok"] is False

    assert result["normalized_from"] == "python -c"

    assert "python_code" in result["FIX_HINT"]

    assert "compound statements" in result["FIX_HINT"]





@pytest.mark.asyncio

async def test_environment_run_python_code_syntax_error_gets_script_fix_hint(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_run_python_code_hint",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_run",

            str(workspace),

            {"python_code": "if True print('bad')", "timeout_sec": 5},

        )

    result = json.loads(raw)



    assert result["ok"] is False

    assert result["python_code"] is True

    assert "Python SyntaxError inside env_run python_code" in result["FIX_HINT"]

    assert "rerun the corrected script" in result["FIX_HINT"]





@pytest.mark.asyncio

async def test_environment_run_rejects_command_plus_python_code_with_usage_hint(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_run_both_hint",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_run",

            str(workspace),

            {"command": "python -V", "python_code": "print('x')", "timeout_sec": 5},

        )

    result = json.loads(raw)



    assert result["ok"] is False

    assert "not both" in result["error"]

    assert "Use command for shell commands" in result["FIX_HINT"]

    assert "Use python_code by itself" in result["FIX_HINT"]





@pytest.mark.asyncio

async def test_environment_run_ast_parse_partial_source_gets_source_fix_hint(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_run_ast_hint",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    code = "import ast\nast.parse('def f():\\n')\n"

    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_run",

            str(workspace),

            {"python_code": code, "timeout_sec": 5},

        )

    result = json.loads(raw)



    assert result["ok"] is False

    assert "parse the complete file once" in result["FIX_HINT"]

    assert "textual line scanning" in result["FIX_HINT"]





@pytest.mark.asyncio

async def test_environment_run_blocks_bulk_source_material_body_extraction(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    (project / "reports").mkdir(parents=True)

    workspace.mkdir()

    for idx in range(3):

        (project / "reports" / f"source_{idx}.docx").write_bytes(b"fake-docx")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_source_material_guard",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    code = """

import os, zipfile

texts = []

for root, dirs, files in os.walk('.'):

    for name in files:

        if name.endswith('.docx'):

            with zipfile.ZipFile(os.path.join(root, name)) as zf:

                texts.append(zf.read('word/document.xml').decode('utf-8'))

print('\\n'.join(texts))

"""

    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_run",

            str(workspace),

            {"python_code": code, "timeout_sec": 5},

        )

    result = json.loads(raw)



    assert result["ok"] is False

    assert result["error_kind"] == "main_thread_bulk_source_material_read_should_delegate"

    assert result["suggested_next_action"]["tool"] == "delegate"

    assert result["suggested_next_action"]["task_template"]["kind"] == "read"

    assert "source-material body extraction" in result["hint"]

    assert "read helper" in result["hint"]





@pytest.mark.asyncio

async def test_environment_run_empty_success_warns(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_empty_output",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_run",

            str(workspace),

            {"command": "python -c \"pass\"", "timeout_sec": 5},

        )

    result = json.loads(raw)



    assert result["ok"] is True

    assert result["stdout"] == ""

    assert "produced no stdout/stderr" in result["empty_output_warning"]





@pytest.mark.asyncio

async def test_environment_pytest_run_is_project_root_isolated(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    (project / "pytest.ini").write_text("[pytest]\naddopts = -q\n", encoding="utf-8")

    (project / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_pytest",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )



    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_run",

            str(workspace),

            {"command": "python -m pytest test_sample.py", "timeout_sec": 30},

        )



    result = json.loads(raw)

    assert result["ok"] is True

    assert "--rootdir" in result["command"]

    assert " -c pytest.ini" in result["command"]





@pytest.mark.asyncio

async def test_environment_pytest_run_uses_empty_config_when_project_has_none(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    (project / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_pytest_empty",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )



    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_run",

            str(workspace),

            {"command": "python -m pytest test_sample.py", "timeout_sec": 30},

        )



    result = json.loads(raw)

    assert result["ok"] is True

    assert " -c " in result["command"]

    assert ".env_pytest_empty.ini" in result["command"]

    assert str(project) in result["command"]

    assert (project / ".env_pytest_empty.ini").is_file()





@pytest.mark.asyncio

async def test_environment_pytest_run_isolated_when_command_has_prefix(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    src = project / "src"

    src.mkdir(parents=True)

    workspace.mkdir()

    (project / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_pytest_prefix",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )



    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_run",

            str(workspace),

            {"command": "set PYTHONPATH=src && python -m pytest test_sample.py", "timeout_sec": 30},

        )



    result = json.loads(raw)

    assert result["ok"] is True

    assert "python -m pytest --rootdir=. -c " in result["command"]

    assert ".env_pytest_empty.ini" in result["command"]

    assert str(project) in result["command"]

    assert " test_sample.py" in result["command"]

    assert "rootdir: " + str(project) in result["stdout"]





@pytest.mark.asyncio

async def test_environment_run_does_not_inherit_parent_pythonpath(tmp_path, monkeypatch):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    bad_root = tmp_path / "bad_root"

    good_root = project / "src"

    (bad_root / "pkg").mkdir(parents=True)

    (good_root / "pkg").mkdir(parents=True)

    workspace.mkdir()

    (bad_root / "pkg" / "__init__.py").write_text("VALUE='bad'\n", encoding="utf-8")

    (good_root / "pkg" / "__init__.py").write_text("VALUE='good'\n", encoding="utf-8")

    monkeypatch.setenv("PYTHONPATH", str(bad_root))

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_env_path",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )



    with runtime_context("environment", env):

        raw_no_path = await handle_environment_tool(

            "env_run",

            str(workspace),

            {"command": "python -c \"import pkg; print(pkg.VALUE)\"", "timeout_sec": 5},

        )

        raw_explicit = await handle_environment_tool(

            "env_run",

            str(workspace),

            {"command": f"set PYTHONPATH={good_root} && python -c \"import pkg; print(pkg.VALUE)\"", "timeout_sec": 5},

        )



    no_path = json.loads(raw_no_path)

    explicit = json.loads(raw_explicit)

    assert no_path["ok"] is False

    assert "ModuleNotFoundError" in no_path["stderr"]

    assert explicit["ok"] is True

    assert explicit["stdout"].strip() == "good"





def test_environment_workspace_copies_are_not_deliverables(tmp_path):

    from app.llm.tools.workspace import list_generated_files



    workspace = tmp_path / "workspace"

    env_dir = workspace / "_env"

    env_dir.mkdir(parents=True)

    (env_dir / "index.html").write_text("<canvas></canvas>\n", encoding="utf-8")

    (workspace / "visible.md").write_text("ok\n", encoding="utf-8")



    assert list_generated_files(str(workspace)) == ["visible.md"]





def test_helper_prefixed_workspace_files_are_not_generated_deliverables(tmp_path):

    from app.llm.tools.workspace import list_generated_files



    (tmp_path / "helper_graph_full_hard_test_graph.py").write_text("def test_x(): pass\n", encoding="utf-8")

    (tmp_path / "helper_plot_final_chart.png").write_bytes(b"not really png")

    (tmp_path / "report.md").write_text("ok\n", encoding="utf-8")



    assert list_generated_files(str(tmp_path)) == ["report.md"]





def test_environment_project_deliverable_resolves_without_chat_attachment(tmp_path):

    from app.core.orchestrator_entry import _existing_environment_project_files

    from app.core.runtime_mode import EnvironmentContext, runtime_context



    project = tmp_path / "project"

    project.mkdir()

    (project / "src" / "algolab").mkdir(parents=True)

    (project / "src" / "algolab" / "graph.py").write_text("def shortest_path(): pass\n", encoding="utf-8")

    (project / "src" / "other").mkdir(parents=True)

    (project / "src" / "other" / "report.py").write_text("def render(): pass\n", encoding="utf-8")

    (project / "src" / "algolab" / "report.py").write_text("def render_report(): pass\n", encoding="utf-8")

    (project / "benchmark_results.json").write_text('{"ok": true}\n', encoding="utf-8")

    (project / "docs").mkdir()

    (project / "docs" / "report.md").write_text("# Report\n", encoding="utf-8")



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_env_dlv",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        found = _existing_environment_project_files({

            "benchmark_results.json",

            "docs/report.md",

            "graph.py",

            "report.py",

            "../escape.txt",

            "missing.json",

        })



    assert found == {"benchmark_results.json", "docs/report.md", "graph.py"}





@pytest.mark.asyncio

async def test_environment_scoped_code_task_can_pass_broad_guard(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.delegate import _sanitize_and_validate_tasks



    env = EnvironmentContext(

        root_dir=str(tmp_path / "project"),

        archive_id="env-user",

        group_id="env-user",

        user_id="env-user",

        project_key="snake",

        project_name="Snake",

    )

    prompt = """

Build the first slice of a browser Snake game.

1. Create the page shell.

2. Create the visual layout.

3. Create the game loop.

4. Create keyboard controls.

5. Create scoring.

6. Create restart behavior.

"""

    with runtime_context("environment", env):

        result = await _sanitize_and_validate_tasks(

            {

                "tasks": [

                    {

                        "task_id": "snake_slice",

                        "kind": "code",

                        "mode": "easy",

                        "prompt": prompt,

                        "expected_outputs": [

                            "_env/src/taskboard/models.py",

                            "_env/src/taskboard/storage.py",

                            "_env/src/taskboard/cli.py",

                            "_env/src/taskboard/filters.py",

                            "_env/src/taskboard/formatters.py",

                            "_env/src/taskboard/validation.py",

                            "_env/tests/test_models.py",

                            "_env/tests/test_cli.py",

                            "_env/README.md",

                        ],

                    }

                ],

            },

            main_workspace=str(tmp_path),

            archive_id="env-user",

            group_id="env-user",

            user_id="env-user",

        )



    assert not isinstance(result, str)

    assert {task["task_id"] for task in result} == {"snake_slice", "snake_slice_hard"}





@pytest.mark.asyncio

async def test_environment_main_workspace_blocks_large_new_env_python_copy(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.workspace_file_ops import handle_write



    workspace = tmp_path / "workspace"

    workspace.mkdir()

    env = EnvironmentContext(

        root_dir=str(tmp_path / "project"),

        archive_id="env-user",

        group_id="env-user",

        user_id="env-user",

        project_key="large-py",

    )

    content = "VALUE = 1\n" * 300

    with runtime_context("environment", env):

        result = await handle_write(str(workspace), "_env/tests/test_large.py", content)



    assert result["ok"] is False

    assert result["blocked_reason"] == "main_thread_env_project_artifact_should_delegate"

    assert result["delegate_required"] is True





@pytest.mark.asyncio

async def test_environment_workspace_can_write_normal_deliverable(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.workspace_file_ops import handle_write



    workspace = tmp_path / "workspace"

    workspace.mkdir()

    env = EnvironmentContext(

        root_dir=str(tmp_path / "project"),

        archive_id="env-user",

        group_id="env-user",

        user_id="env-user",

        project_key="deliverable",

    )

    content = "# Report\n\nSummary.\n"

    with runtime_context("environment", env):

        result = await handle_write(str(workspace), "analysis_outputs/report.md", content)



    assert result["ok"] is True

    assert (workspace / "analysis_outputs" / "report.md").read_text(encoding="utf-8") == content





@pytest.mark.asyncio

async def test_environment_main_workspace_existing_env_copy_write_points_to_edit_flow(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.workspace_file_ops import handle_write



    workspace = tmp_path / "workspace"

    existing = workspace / "_env" / "tests" / "test_existing.py"

    existing.parent.mkdir(parents=True)

    existing.write_text("VALUE = 1\n", encoding="utf-8")

    env = EnvironmentContext(

        root_dir=str(tmp_path / "project"),

        archive_id="env-user",

        group_id="env-user",

        user_id="env-user",

        project_key="existing-py",

    )



    with runtime_context("environment", env):

        result = await handle_write(str(workspace), "_env/tests/test_existing.py", "VALUE = 2\n")



    assert result["ok"] is False

    assert result["blocked_reason"] == "env_project_copy_write_overwrite_forbidden"

    assert "modify it with edit_file/multi_edit/insert_in_file" in result["error"]

    assert "whole-file rewrite is intended" in result["error"]





@pytest.mark.asyncio

async def test_environment_workspace_write_rejects_staged_root_and_existing_directory(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.workspace_file_ops import handle_write



    workspace = tmp_path / "workspace"

    (workspace / "src").mkdir(parents=True)

    env = EnvironmentContext(

        root_dir=str(tmp_path / "project"),

        archive_id="env-user",

        group_id="env-user",

        user_id="env-user",

        project_key="write-paths",

    )



    with runtime_context("environment", env):

        staged_root = await handle_write(str(workspace), "_env/", "x")

        existing_dir = await handle_write(str(workspace), "src", "x")



    assert staged_root["ok"] is False

    assert staged_root["error"] == "path_is_directory_or_missing_staged_root"

    assert existing_dir["ok"] is False

    assert existing_dir["error"] == "path_is_directory"





def test_environment_delegate_auto_fetches_referenced_project_files(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.delegate_actions import _auto_fetch_environment_workspace_refs



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    source = project / "src" / "pkg" / "core.py"

    source.parent.mkdir(parents=True)

    workspace.mkdir()

    source.write_text("def value():\n    return 1\n", encoding="utf-8")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="env-user",

        group_id="env-user",

        user_id="env-user",

        project_key="auto-fetch",

    )

    tasks = [{

        "task_id": "core",

        "prompt": "Edit `_env/src/pkg/core.py` and keep behavior compatible.",

        "expected_outputs": ["_env/src/pkg/core.py"],

    }]



    with runtime_context("environment", env):

        stats = _auto_fetch_environment_workspace_refs(str(workspace), tasks)



    copied = workspace / "_env" / "src" / "pkg" / "core.py"

    assert stats["fetched"] == ["src/pkg/core.py"]

    assert copied.read_text(encoding="utf-8") == "def value():\n    return 1\n"





def test_environment_delegate_auto_fetches_referenced_project_directories(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.delegate_actions import _auto_fetch_environment_workspace_refs



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    source_dir = project / "src" / "pkg"

    backup_dir = source_dir / ".env_backups"

    source_dir.mkdir(parents=True)

    backup_dir.mkdir()

    workspace.mkdir()

    (source_dir / "core.py").write_text("VALUE = 1\n", encoding="utf-8")

    (source_dir / "util.py").write_text("VALUE = 2\n", encoding="utf-8")

    (backup_dir / "old.py").write_text("stale\n", encoding="utf-8")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="env-user",

        group_id="env-user",

        user_id="env-user",

        project_key="auto-fetch-dir",

    )

    tasks = [{

        "task_id": "pkg",

        "prompt": "Index `_env/src/pkg` before editing `_env/src/pkg/core.py`.",

        "expected_outputs": ["_env/src/pkg/core.py"],

    }]



    with runtime_context("environment", env):

        stats = _auto_fetch_environment_workspace_refs(str(workspace), tasks)



    assert stats["fetched"] == ["src/pkg/core.py", "src/pkg/util.py"]

    assert (workspace / "_env" / "src" / "pkg" / "core.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    assert (workspace / "_env" / "src" / "pkg" / "util.py").read_text(encoding="utf-8") == "VALUE = 2\n"

    assert not (workspace / "_env" / "src" / "pkg" / ".env_backups" / "old.py").exists()





def test_environment_delegate_auto_fetches_project_context_files(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.delegate_actions import _auto_fetch_environment_workspace_refs



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    pkg = project / "src" / "taskboard"

    pkg.mkdir(parents=True)

    workspace.mkdir()

    (project / "pyproject.toml").write_text("[project]\nname = 'taskboard'\n", encoding="utf-8")

    (project / "README.md").write_text("# TaskBoard\n", encoding="utf-8")

    (pkg / "__init__.py").write_text("__version__ = '0.1.0'\n", encoding="utf-8")

    (pkg / "cli.py").write_text("def main():\n    return 0\n", encoding="utf-8")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="env-user",

        group_id="env-user",

        user_id="env-user",

        project_key="auto-fetch-context",

    )

    tasks = [{

        "task_id": "cli",

        "prompt": "Edit `_env/src/taskboard/cli.py` and run an import smoke check.",

        "expected_outputs": ["_env/src/taskboard/cli.py"],

    }]



    with runtime_context("environment", env):

        stats = _auto_fetch_environment_workspace_refs(str(workspace), tasks)



    assert "src/taskboard/cli.py" in stats["fetched"]

    assert "src/taskboard/__init__.py" in stats["fetched"]

    assert "pyproject.toml" in stats["fetched"]

    assert (workspace / "_env" / "pyproject.toml").is_file()

    assert (workspace / "_env" / "src" / "taskboard" / "__init__.py").is_file()



