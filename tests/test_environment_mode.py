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



    # Round 17 (#2): environment mode prunes chat-memory/group-file tools
    # (semantically dead in benchmark/project mode), then adds env_* tools.
    # The contract is now: (ROUND2_TOOLS minus the static pruned set) preserved
    # in order, env tools appended. Stable within the mode for prefix caching.
    pruned = registry._ENVIRONMENT_IRRELEVANT_TOOLS
    baseline_names = [n for n in _tool_names(registry.ROUND2_TOOLS) if n not in pruned]

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

    assert not (set(names) & pruned)

    assert "env_inventory" in names

    assert "env_list_tree" in names

    assert "env_run" in names



def test_env_run_schema_requires_exactly_one_execution_body():

    from app.llm.tools.environment import ENV_RUN_SCHEMA



    params = ENV_RUN_SCHEMA["function"]["parameters"]

    assert {"command", "python_code"} <= set(params["properties"])

    assert params["oneOf"] == [

        {"required": ["command"], "not": {"required": ["python_code"]}},

        {"required": ["python_code"], "not": {"required": ["command"]}},

    ]

    assert "Alternative to command" in params["properties"]["python_code"]["description"]

    assert "二选一" in params["properties"]["python_code"]["description"]



@pytest.mark.asyncio

async def test_env_run_runtime_still_reports_both_execution_bodies_as_fact(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    project.mkdir()

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_env_run_body_fact",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )



    with runtime_context("environment", env):

        result = await handle_environment_tool(

            "env_run",

            str(tmp_path / "workspace"),

            {"command": "python --version", "python_code": "print('x')"},

        )



    data = json.loads(result)

    assert data["ok"] is False

    assert data["error_kind"] == "both_command_and_python_code"

    assert "command 与 python_code 二选一" in data["error"]

    assert "env_run accepts exactly one execution body" in data["FIX_HINT"]





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
async def test_env_inventory_exposes_acceptance_script_paths(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    project.mkdir()
    (project / "users.db").write_bytes(b"sqlite")
    (project / "verify_results.py").write_text("print('PASS')\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test_inventory_acceptance_script",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )

    with runtime_context("environment", env):
        result = json.loads(await handle_environment_tool(
            "env_inventory",
            str(workspace),
            {"limit": 20},
        ))

    assert result["ok"] is True
    assert result["acceptance_script_paths"] == ["verify_results.py"]
    assert "Project-provided verify/check/validate scripts are acceptance facts" in result["acceptance_script_note"]


@pytest.mark.asyncio
async def test_env_inventory_suffix_filter_still_exposes_acceptance_script_paths(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    project.mkdir()
    (project / "users.db").write_bytes(b"sqlite")
    (project / "result.csv").write_text("x\n", encoding="utf-8")
    (project / "verify_results.py").write_text("print('PASS')\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test_inventory_filtered_acceptance_script",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )

    with runtime_context("environment", env):
        result = json.loads(await handle_environment_tool(
            "env_inventory",
            str(workspace),
            {"suffixes": [".db", ".csv"], "limit": 20},
        ))

    listed_paths = [row["project_path"] for row in result["resources"]]
    assert "verify_results.py" not in listed_paths
    assert result["acceptance_script_paths"] == ["verify_results.py"]
    assert "acceptance facts" in result["acceptance_script_note"]


@pytest.mark.asyncio
async def test_env_run_verifier_missing_workspace_content_returns_acceptance_fact(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    project.mkdir()
    (project / "verify_summary_structure.py").write_text(
        "print(\"FAIL: workspace missing required content: ['decision']\")\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
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
            {"command": "python verify_summary_structure.py", "timeout_sec": 5},
        ))

    assert result["ok"] is False
    fact = result["acceptance_failure_fact"]
    assert fact["kind"] == "acceptance_failure_fact"
    assert "missing workspace/project content" in fact["fact"]
    assert "workspace missing required content" in fact["observed_failure"]


@pytest.mark.asyncio
async def test_env_read_workspace_scanning_verifier_returns_acceptance_script_fact(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    project.mkdir()
    (project / "verify_summary_structure.py").write_text(
        '"""Recursive workspace search verifier."""\n'
        "from pathlib import Path\n\n"
        "def iter_workspace_text_files(root: Path = Path('.')):\n"
        "    for path in root.rglob('*'):\n"
        "        if path.is_file():\n"
        "            yield path, path.read_text(encoding='utf-8', errors='ignore')\n\n"
        "def workspace_blob() -> str:\n"
        "    return '\\n'.join(text for _, text in iter_workspace_text_files())\n\n"
        "def main() -> int:\n"
        "    blob = workspace_blob().lower()\n"
        "    needed = ['decision']\n"
        "    any_of = ['open', 'still', 'outstanding']\n"
        "    return 0 if all(s in blob for s in needed) and any(s in blob for s in any_of) else 1\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )

    with runtime_context("environment", env):
        result = json.loads(await handle_environment_tool(
            "env_read",
            str(workspace),
            {"path": "verify_summary_structure.py"},
        ))

    assert result["ok"] is True
    fact = result["acceptance_script_fact"]
    assert fact["kind"] == "acceptance_script_read_fact"
    assert fact["scans_project_or_workspace_text"] is True
    assert "chat-only final response is not part of that scanned text" in fact["content_visibility_fact"]
    assert {"name": "needed", "strings": ["decision"]} in fact["literal_string_lists"]
    assert {"name": "any_of", "strings": ["open", "still", "outstanding"]} in fact["literal_string_lists"]
    assert "Fact: env_read returned an acceptance/check script" in result["next_action_instruction"]
    assert "chat-only final response is not included in that scan" in result["next_action_instruction"]
    assert "source/test body" not in result["next_action_instruction"]


@pytest.mark.asyncio
async def test_env_read_command_shim_returns_command_fact(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    project.mkdir()
    (project / "python3.cmd").write_text(
        '@"C:\\Python\\python.exe" "runner_shim.py" %*\n',
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test_command_shim",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )

    with runtime_context("environment", env):
        result = json.loads(await handle_environment_tool(
            "env_read",
            str(workspace),
            {"path": "python3.cmd"},
        ))

    assert result["ok"] is True
    fact = result["command_shim_read_fact"]
    assert fact["kind"] == "command_shim_read_fact"
    assert "command-routing evidence" in fact["fact"]
    assert "runner failure" in result["next_action_instruction"]


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



def test_env_text_detection_includes_node_scripts_and_logs(tmp_path):

    from app.llm.tools.environment import _looks_text

    for name in ("verify_form.cjs", "vite.config.mjs", "server.log"):

        path = tmp_path / name

        path.write_text("plain text\n", encoding="utf-8")

        assert _looks_text(path)



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


@pytest.mark.asyncio

async def test_env_read_expected_file_reports_exact_text_reference(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    (project / "expected").mkdir(parents=True)

    workspace.mkdir()

    (project / "expected" / "report.txt").write_text("A\nB\n", encoding="utf-8", newline="\n")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_expected_reference",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_read",

            str(workspace),

            {"path": "expected/report.txt"},

        )

    result = json.loads(raw)



    assert result["ok"] is True

    reference = result["exact_text_reference"]

    assert reference["kind"] == "exact_text_reference"

    assert reference["path"] == "expected/report.txt"

    assert reference["text_facts"]["newline_counts"] == {"crlf": 0, "lf": 2, "cr": 0}

    assert "line order" in reference["fact"]
    assert "verifier's text-vs-byte comparison semantics" in reference["fact"]

    assert "行序" in reference["fact"]
    assert "stdout 是否需要逐字节匹配取决于验证器语义" in reference["fact"]




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



    # Round 17 (#2): chat-memory/group-file schemas are pruned in environment
    # mode; the surviving chat tools keep their order and identity, and the
    # chat delegate object itself is reused (not rebuilt).
    pruned = registry._ENVIRONMENT_IRRELEVANT_TOOLS
    surviving_chat = [t for t in chat_tools if t["function"]["name"] not in pruned]

    assert env_tools[:len(surviving_chat)] == surviving_chat

    assert env_tools[len(surviving_chat) - 1] is surviving_chat[-1]

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

    assert len(text.encode("utf-8")) < 4500
    assert "maintaining a real local project" in text
    assert "chat workspace is separate from the project" in text
    assert "project_map" in text
    assert "file_summary" in text
    assert "impact_review" in text
    assert "whole-directory questions" in text
    assert "delegate_inventory" in text
    assert "delegate_inventory` is only a shortcut tool" in text
    assert "inventory, summarize" not in text
    assert "env_apply_create after confirmed absence" in text
    assert "env_run executes in the real project directory" in text
    assert "observed result" in text
    assert "Read project paths with env_* tools" in text
    assert "project-relative paths without `_env/`" in text
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

    assert "workspace.write is chat-workspace scratch" in round2

    assert "env_apply_create or env_apply_replace" in round2
    assert "Bare helper filenames and workspace.write are chat-workspace artifacts" in round2
    assert "not project-verifier-visible" in round2



    for text in (addon, round2, schema_desc):

        assert "python_code" in text

        assert "outside the project tree" in text

        assert (
            "inspection scripts are not project files" in text.lower()
            or "检查脚本不属于项目文件" in text
            or "临时检查脚本不属于项目文件" in text
        )
        assert (
            "characters, bytes, file size, line count, file count" in text.lower()
            or "Characters, bytes, file size, line count, and file count are" in text
            or "Label units exactly" in text
        )
        assert "transient inspection scripts" in text

    assert "env_apply_create" in addon
    assert "helper output with a bare filename is a chat-workspace artifact" in addon





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



    assert "Bulk source-material extraction belongs to helpers" in addon
    assert "Office/PDF/media artifacts" in addon
    assert "bulk" in schema_desc
    assert "Office/PDF/image" in schema_desc
    assert "read helpers" in schema_desc
    assert "code-helper acceptance_check" in schema_desc
    assert "narrow checks for main-owned changes" in schema_desc
    assert "stdout_facts" in schema_desc
    assert "CRLF line endings" in schema_desc
    assert "do not convert them into a byte-level output requirement" in schema_desc
    assert "delegate bulk authoring/extraction/computation" in round2
    assert "batch of main-thread source/test reads usually duplicates helper work" in round2





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

        assert "_env" in text

    assert "read_file/search_in_file/code_index/edit_file/multi_edit" in addon
    assert "workspace/_env only for existing staged copies" in round2

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



    assert "workspace.mkdir" in addon
    assert "chat-workspace folders" in addon
    assert "env_apply_create" in addon
    assert "creates project parent directories" in addon
    assert "confirmed new files" in addon
    assert "confirmed absence" in addon





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



    assert "platform-appropriate commands" in addon
    assert "observed result" in addon
    assert "project-relative paths" in addon
    assert "run project pytest from the project's own root" in round2
    assert "observed commands/results" in round2





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
    assert "internal staging path" in addon





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



    assert "substantially rewritten source, tests, scripts" in addon
    assert "delegate authoring" in addon
    assert "keep long file bodies out of main-thread tool calls" in addon.lower()
    assert "delegate bulk authoring" in round2





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



    assert "delegate authoring" in addon
    assert "keep long file bodies out of main-thread tool calls" in addon.lower()
    assert "framework contract" in round2 or "bounded helpers" in round2





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

        "import os\n"

        "print('env_root=' + Path(os.environ['ENV_PROJECT_ROOT']).name)\n"

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

    assert "env_root=project" in result["stdout"]

    assert "ENV_PROJECT_ROOT contains the real project root path" in result["python_code_project_root_fact"]

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

    assert result["content"] == ""
    assert result["content_compacted"] is True
    assert result["source_handoff_fact"]["kind"] == "main_thread_source_body_compacted"

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



    assert result["ok"] is True

    assert "_redirected_from" not in result

    assert result["content"] == ""

    assert result["content_compacted"] is True

    assert result["content_omitted_reason"] == "staged_copy_missing_project_path_exists"

    assert result["path_zone"] == "staged_file"

    assert result["staged_path"] == "_env/main.py"

    assert result["staged_path_exists"] is False

    assert result["project_path"] == "main.py"

    assert result["project_path_exists"] is True

    assert "env_read" in result["suggested_tools"]

    assert "path evidence" in result["_next_action_instruction"]





@pytest.mark.asyncio

async def test_environment_read_file_existing_env_copy_full_read_stays_compact_for_main(tmp_path):

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

    assert result["content"] == ""
    assert result["content_compacted"] is True
    assert result["content_omitted_reason"] == "main_thread_staged_source_body_compacted"
    assert result["project_path"] == "main.py"
    assert result["total_lines"] == 1



@pytest.mark.asyncio

async def test_environment_read_file_existing_env_copy_range_read_returns_text_for_main(tmp_path):

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

        raw = await registry._handle_read_file(
            str(workspace),
            {"path": "_env/main.py", "start_line": 1, "end_line": 1},
        )

    result = json.loads(raw)



    assert result["ok"] is True

    assert result["path"] == "_env/main.py"

    assert "print('staged')" in result["content"]

    assert "content_compacted" not in result



@pytest.mark.asyncio

async def test_environment_read_file_existing_env_copy_helper_full_read_returns_text(tmp_path):

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

        raw = await registry._handle_read_file(
            str(workspace),
            {"path": "_env/main.py"},
            caller_kind="helper",
        )

    result = json.loads(raw)



    assert result["ok"] is True

    assert result["path"] == "_env/main.py"

    assert "print('staged')" in result["content"]

    assert "content_compacted" not in result





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



    assert result["ok"] is True

    assert result["index_executed"] is False

    assert result["content_omitted_reason"] == "staged_copy_missing_project_path_exists"

    assert "_redirected_from" not in result

    assert result["path_zone"] == "staged_file"

    assert result["staged_path"] == "_env/app/main.py"

    assert result["project_path"] == "app/main.py"

    assert result["project_path_exists"] is True

    assert "env_fetch" in result["suggested_tools"]





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



    assert result["ok"] is True

    assert result["search_executed"] is False

    assert result["matches"] == []

    assert result["content_omitted_reason"] == "staged_copy_missing_project_path_exists"

    assert "_redirected_from" not in result

    assert result["path_zone"] == "staged_file"

    assert result["staged_path"] == "_env/src/feature.py"

    assert result["project_path"] == "src/feature.py"

    assert result["project_path_exists"] is True

    assert "env_search" in result["suggested_tools"]





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

async def test_environment_list_tree_compact_text_materials_suggests_single_helper_handoff(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    (project / "materials").mkdir(parents=True)
    workspace.mkdir()
    for idx in range(1, 9):
        (project / "materials" / f"source_{idx:02d}.txt").write_text(f"item {idx}\n", encoding="utf-8")
    (project / "prefs.yaml").write_text("format: concise\n", encoding="utf-8")
    (project / "verify_outputs.py").write_text("print('PASS')\n", encoding="utf-8")

    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test_tree_compact_text",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )
    with runtime_context("environment", env):
        raw = await handle_environment_tool(
            "env_list_tree",
            str(workspace),
            {"path": ".", "max_depth": 2, "limit": 80},
        )
    result = json.loads(raw)

    assert result["ok"] is True
    assert result["text_material_handoff_fact"]["kind"] == "compact_text_material_set"
    assert result["text_material_handoff_fact"]["material_count"] == 9
    assert result["acceptance_script_paths"] == ["verify_outputs.py"]
    assert "compact text-material set" in result["next_action_instruction"]
    assert "single read/edit/code helper" in result["next_action_instruction"]
    assert "kind='inventory'" not in result["next_action_instruction"]
    assert "large, mixed, or structurally complex" not in result["next_action_instruction"]


@pytest.mark.asyncio
async def test_env_inventory_compact_text_materials_returns_handoff_fact(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    project.mkdir()
    for idx in range(1, 6):
        (project / f"note_{idx:02d}.md").write_text(f"# note {idx}\n", encoding="utf-8")
    (project / "prefs.json").write_text('{"style":"brief"}\n', encoding="utf-8")
    workspace = tmp_path / "workspace"
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test_inventory_compact_text",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )

    with runtime_context("environment", env):
        result = json.loads(await handle_environment_tool(
            "env_inventory",
            str(workspace),
            {"limit": 20},
        ))

    assert result["ok"] is True
    assert result["text_material_handoff_fact"]["kind"] == "compact_text_material_set"
    assert result["text_material_handoff_fact"]["material_count"] == 6
    assert "compact text-material set" in result["next_action_instruction"]
    assert "Split broad content extraction" not in result["next_action_instruction"]


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


@pytest.mark.asyncio
async def test_environment_list_tree_small_code_project_suggests_helper_handoff(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    (project / "tests").mkdir(parents=True)
    workspace.mkdir()
    (project / "config_loader.py").write_text("def load_config():\n    return {}\n", encoding="utf-8")
    (project / "app_config.py").write_text("DEFAULTS = {}\n", encoding="utf-8")
    (project / "python3.cmd").write_text('@"C:\\Python\\python.exe" %*\n', encoding="utf-8")
    (project / "tests" / "test_config_loader.py").write_text("def test_config():\n    assert True\n", encoding="utf-8")

    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test_tree_code_small",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )
    with runtime_context("environment", env):
        raw = await handle_environment_tool(
            "env_list_tree",
            str(workspace),
            {"path": ".", "max_depth": 3, "limit": 20},
        )
    result = json.loads(raw)

    assert result["ok"] is True
    assert "source/test/acceptance project paths" in result["next_action_instruction"]
    assert "input_files" in result["next_action_instruction"]
    assert "duplicates code-helper work" in result["next_action_instruction"]
    assert "source reading is not the same pre-edit evidence" in result["next_action_instruction"]
    assert "python3.cmd" not in result["next_action_instruction"].split("project paths", 1)[-1].split(")", 1)[0]
    assert "Interpreter/runner shim paths" in result["next_action_instruction"]
    assert result["helper_handoff_fact"]["kind"] == "code_helper_handoff_ready"
    assert result["helper_handoff_fact"]["project_paths"] == [
        "app_config.py",
        "config_loader.py",
        "tests/test_config_loader.py",
    ]
    assert "increases coordinator context" in result["helper_handoff_fact"]["main_thread_read_value"]
    assert result["helper_handoff_fact"]["command_shim_paths"] == ["python3.cmd"]
    assert "kind='inventory'" not in result["next_action_instruction"]


@pytest.mark.asyncio
async def test_environment_list_tree_small_code_project_exposes_acceptance_scripts(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    (project / "normalizer.py").write_text("def normalize(value):\n    return value.strip()\n", encoding="utf-8")
    (project / "verify_added_tests.py").write_text(
        "from pathlib import Path\nassert Path('tests/test_normalizer.py').exists()\n",
        encoding="utf-8",
    )
    (project / "python3.cmd").write_text('@"C:\\Python\\python.exe" %*\n', encoding="utf-8")

    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test_tree_acceptance_script",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )
    with runtime_context("environment", env):
        raw = await handle_environment_tool(
            "env_list_tree",
            str(workspace),
            {"path": ".", "max_depth": 2, "limit": 20},
        )
    result = json.loads(raw)

    assert result["ok"] is True
    assert result["acceptance_script_paths"] == ["verify_added_tests.py"]
    assert result["helper_handoff_fact"]["kind"] == "code_helper_handoff_ready"
    assert result["helper_handoff_fact"]["acceptance_script_paths"] == ["verify_added_tests.py"]
    assert "Project-provided verify/check/validate scripts are acceptance facts" in result["helper_handoff_fact"]["acceptance_script_note"]
    assert "source/test/acceptance project paths" in result["next_action_instruction"]
    assert "verify/check/validate scripts in the listing are acceptance facts" in result["next_action_instruction"]
    assert "read or run them when target paths or completion checks are ambiguous" in result["next_action_instruction"]
    assert "source reading is not the same pre-edit evidence" in result["next_action_instruction"]





@pytest.mark.asyncio
async def test_environment_list_tree_data_query_project_exposes_helper_handoff_fact(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    (project / "users.db").write_bytes(b"SQLite format 3\x00")
    (project / "verify_results.py").write_text(
        "from pathlib import Path\nassert Path('eu_active_2026.csv').exists()\n",
        encoding="utf-8",
    )

    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test_tree_data_query",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )
    with runtime_context("environment", env):
        raw = await handle_environment_tool(
            "env_list_tree",
            str(workspace),
            {"path": ".", "max_depth": 2, "limit": 20},
        )
    result = json.loads(raw)

    assert result["ok"] is True
    assert result["acceptance_script_paths"] == ["verify_results.py"]
    assert result["helper_handoff_fact"]["kind"] == "code_helper_handoff_ready"
    assert result["helper_handoff_fact"]["data_paths"] == ["users.db"]
    assert result["helper_handoff_fact"]["project_paths"] == ["users.db", "verify_results.py"]
    assert "data/query artifact tasks" in result["helper_handoff_fact"]["data_path_note"]
    assert "data/acceptance project paths" in result["next_action_instruction"]
    assert "probe data files" in result["next_action_instruction"]
    assert "env_read/env_run calls over listed source/test/data files" in result["next_action_instruction"]


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

    assert "main thread light" in text

    assert "acceptance checklist" in text

    assert "Project file paths are evidence, not guesses" in text

    assert "run project pytest from the project's own root" in text

    assert "apply verified slices" in text
    assert "verified project evidence" in text
    assert "project-relative paths without `_env/`" in text
    assert "Tool failures may add path/schema facts dynamically" in text
    assert len(text.encode("utf-8")) < 1800


def test_env_read_schema_prefers_helper_handoff_for_coding_source_paths():

    from app.llm.tools.environment import ENV_FETCH_SCHEMA, ENV_READ_SCHEMA

    desc = ENV_READ_SCHEMA["function"]["description"]

    assert "For coding/debugging" in desc

    assert "env_inventory/env_list_tree identifies likely source or test paths" in desc

    assert "input_files with acceptance_checks" in desc

    assert "do its own reading, diagnosis, edits, and tests" in desc

    assert "主进程只在缺少路由事实、局部主责事实或验收记账事实时读取源码正文" in desc

    fetch_desc = ENV_FETCH_SCHEMA["function"]["description"]
    assert "For helper handoff alone" in fetch_desc
    assert "project-relative input_files already carry the routing fact" in fetch_desc
    assert "after a helper returns a staged edit candidate" in fetch_desc
    assert "expected_hash, diff, or apply evidence" in fetch_desc





def test_env_list_tree_small_code_project_returns_helper_handoff_fact():

    from app.llm.tools.environment import _list_tree_workflow_hint

    rows = [

        {"path": "config_loader.py", "type": "file", "size": 100},

        {"path": "app_config.py", "type": "file", "size": 80},

        {"path": "python3.cmd", "type": "file", "size": 40},

        {"path": "tests", "type": "dir"},

        {"path": "tests/test_config_loader.py", "type": "file", "size": 200},

    ]

    hint = _list_tree_workflow_hint(rows)

    assert "source/test/acceptance project paths" in hint

    assert "compact code-helper request" in hint

    assert "input_files" in hint
    assert "duplicates code-helper work" in hint

    assert "python3.cmd" not in hint.split("project paths", 1)[-1].split(")", 1)[0]

    assert "Interpreter/runner shim paths" in hint

    assert "diff/apply" in hint

    assert "源码/测试/验收路径" in hint


def test_env_list_tree_handoff_fact_uses_acceptance_scripts_without_existing_tests_dir():

    from app.llm.tools.environment import _list_tree_code_helper_handoff_fact, _list_tree_workflow_hint

    rows = [

        {"path": "normalizer.py", "type": "file", "size": 100},

        {"path": "verify_added_tests.py", "type": "file", "size": 300},

        {"path": "python3.cmd", "type": "file", "size": 40},

    ]

    fact = _list_tree_code_helper_handoff_fact(rows)
    hint = _list_tree_workflow_hint(rows)

    assert fact is not None
    assert fact["project_paths"] == ["normalizer.py", "verify_added_tests.py"]
    assert fact["acceptance_script_paths"] == ["verify_added_tests.py"]
    assert "acceptance facts" in fact["acceptance_script_note"]
    assert "source/test/acceptance project paths" in hint
    assert "target paths or completion checks are ambiguous" in hint


def test_stream_text_facts_expose_exact_newline_and_tail_details():

    from app.llm.tools.environment import _stream_text_facts

    facts = _stream_text_facts("a\r\nb\r\n\r\n")

    assert facts["line_count"] == 3
    assert facts["ends_with_newline"] is True
    assert facts["trailing_blank_lines"] == 1
    assert facts["newline_counts"] == {"crlf": 3, "lf": 0, "cr": 0}
    assert facts["tail_repr"] == "'a\\r\\nb\\r\\n\\r\\n'"



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



    assert "Apply helper-completed coherent slices to project paths" in text
    assert "Broad multi-file/greenfield" in text
    assert "compact framework contract" in text
    assert "bounded slots" in text
    assert "Validation claims must name the exact command" in text
    assert "Final replies use project-relative paths" in text
    assert "项目事实以 env 证据为准" in text





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

async def test_environment_diff_allows_apply_without_refetch_overwriting_staged_copy(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    source = project / "hello.txt"

    source.write_text("one\n", encoding="utf-8")

    staged = workspace / "_env" / "hello.txt"

    staged.parent.mkdir(parents=True)

    staged.write_text("two\n", encoding="utf-8")



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        diff = json.loads(await handle_environment_tool(

            "env_diff",

            str(workspace),

            {"path": "hello.txt", "workspace_path": "_env/hello.txt"},

        ))

        assert diff["ok"] is True

        assert diff["changed"] is True

        assert staged.read_text(encoding="utf-8") == "two\n"



        applied = json.loads(await handle_environment_tool(

            "env_apply_replace",

            str(workspace),

            {

                "path": "hello.txt",

                "workspace_path": "_env/hello.txt",

                "expected_hash": diff["source_sha256"],

            },

        ))



    assert applied["ok"] is True

    assert source.read_text(encoding="utf-8") == "two\n"



@pytest.mark.asyncio

async def test_environment_apply_accepts_current_hash_without_refetch_overwriting_helper_output(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool

    import hashlib



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    source = project / "hello.txt"

    source.write_text("one\n", encoding="utf-8")

    staged = workspace / "_env" / "hello.txt"

    staged.parent.mkdir(parents=True)

    staged.write_text("two\n", encoding="utf-8")

    expected = hashlib.sha256(source.read_bytes()).hexdigest()



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        applied = json.loads(await handle_environment_tool(

            "env_apply_replace",

            str(workspace),

            {

                "path": "hello.txt",

                "workspace_path": "_env/hello.txt",

                "expected_hash": expected,

            },

        ))



    assert applied["ok"] is True

    assert source.read_text(encoding="utf-8") == "two\n"

    assert staged.read_text(encoding="utf-8") == "two\n"



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

    assert result["recovery_facts"]["matching_helper_kind"] == "read"
    assert result["recovery_facts"]["matching_project_path"] == "large.pdf"
    assert result["suggested_helper_kind"] == "read"

    assert "Recovery facts: env_run can collect metadata" in result["next_action"]

    assert any("page batching" in item or "batch" in item for item in result["observed_recovery_options"])





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
    assert result["tool_result_truncated"] is True
    assert result["content_truncated"] is True
    assert "content_full_saved_path" in result
    assert "only the head excerpt" in result["visible_excerpt_policy"]


@pytest.mark.asyncio
async def test_direct_env_read_long_content_is_budgeted_before_context(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    project.mkdir()
    (project / "large.txt").write_text("HEAD\n" + ("x" * 9000), encoding="utf-8")

    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )
    with runtime_context("environment", env):
        result = json.loads(await handle_environment_tool(
            "env_read",
            str(tmp_path),
            {"path": "large.txt", "max_chars": 30000},
        ))

    assert result["ok"] is True
    assert result["tool_result_truncated"] is True
    assert result["output_truncated"] is True
    assert result["content_truncated"] is True
    assert result["content"].startswith("HEAD")
    assert len(result["content"]) < 4000
    saved = tmp_path / result["content_full_saved_path"]
    assert saved.is_file()
    assert saved.read_text(encoding="utf-8").startswith("HEAD")


@pytest.mark.asyncio

async def test_env_read_source_body_returns_fact_first_helper_handoff_hint(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    project.mkdir()

    source = project / "config_loader.py"

    source.write_text("\n".join(f"line_{idx} = {idx}" for idx in range(12)), encoding="utf-8")



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        result = json.loads(await handle_environment_tool("env_read", str(tmp_path), {"path": "config_loader.py"}))



    assert result["ok"] is True

    assert result["truncated"] is False
    assert result["content"] == ""
    assert result["content_compacted"] is True
    assert result["source_handoff_fact"]["kind"] == "main_thread_source_body_compacted"
    assert result["source_handoff_fact"]["total_lines"] == 12

    hint = result["next_action_instruction"]

    assert "returned no body content by default" in hint

    assert "input_files" in hint

    assert "reading, diagnosis, edits, and tests" in hint

    assert "smallest relevant line range" in hint

    assert "源码/测试正文默认未注入主进程上下文" in hint

    assert "code helper" in hint

    assert "事实：" in hint


@pytest.mark.asyncio
async def test_env_read_source_body_full_read_stays_compact_with_include_content(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    project.mkdir()
    (project / "config_loader.py").write_text("VALUE = 1\nVALUE2 = 2\n", encoding="utf-8")
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )

    with runtime_context("environment", env):
        explicit = json.loads(await handle_environment_tool(
            "env_read",
            str(tmp_path),
            {"path": "config_loader.py", "include_content": True},
        ))
        ranged = json.loads(await handle_environment_tool(
            "env_read",
            str(tmp_path),
            {"path": "config_loader.py", "start_line": 1, "end_line": 1},
        ))

    assert explicit["ok"] is True
    assert explicit["content"] == ""
    assert explicit["content_compacted" ] is True
    assert explicit["source_handoff_fact"]["kind"] == "main_thread_source_body_compacted"
    assert ranged["ok"] is True
    assert ranged["content"] == "VALUE = 1"
    assert "content_compacted" not in ranged


@pytest.mark.asyncio
async def test_registry_dispatch_budgets_long_env_read_content(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.registry import dispatch

    project = tmp_path / "project"
    project.mkdir()
    (project / "large.txt").write_text("HEAD\n" + ("x" * 9000), encoding="utf-8")
    workspace = tmp_path / "workspace"
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test_env_dispatch_budget",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )

    with runtime_context("environment", env):
        raw = await dispatch(
            "env_read",
            {"path": "large.txt", "max_chars": 30000},
            archive_id="arch_test_env_dispatch_budget",
            group_id="env_user_u",
            user_id="u",
            workspace_dir=str(workspace),
        )
    result = json.loads(raw)

    assert result["ok"] is True
    assert result["tool_result_truncated"] is True
    assert result["content_truncated"] is True
    assert result["content"].startswith("HEAD")
    assert len(result["content"]) < 4000
    saved = workspace / result["full_result_saved_path"]
    assert saved.is_file()
    assert "HEAD" in saved.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_env_list_tree_compact_code_project_returns_helper_handoff_fact(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    project.mkdir()
    (project / "contracts").mkdir()
    (project / "contracts" / "tests").mkdir()
    (project / "service").mkdir()
    (project / "service" / "tests").mkdir()
    (project / "contracts" / "customer_event.py").write_text("def validate_event(payload):\n    return payload\n", encoding="utf-8")
    (project / "contracts" / "tests" / "test_schema.py").write_text("def test_schema():\n    assert True\n", encoding="utf-8")
    (project / "service" / "render.py").write_text("def render_account(event):\n    return str(event)\n", encoding="utf-8")
    (project / "service" / "tests" / "test_client.py").write_text("def test_client():\n    assert True\n", encoding="utf-8")
    (project / "pytest.cmd").write_text("@echo off\npython -m pytest %*\n", encoding="utf-8")
    (project / "python3.cmd").write_text("@echo off\npython %*\n", encoding="utf-8")

    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )
    with runtime_context("environment", env):
        result = json.loads(await handle_environment_tool("env_list_tree", str(tmp_path), {"path": ".", "max_depth": 3, "limit": 200}))

    assert result["ok"] is True
    assert result["helper_handoff_fact"]["kind"] == "code_helper_handoff_ready"
    assert "contracts/customer_event.py" in result["helper_handoff_fact"]["project_paths"]
    assert "service/render.py" in result["helper_handoff_fact"]["project_paths"]
    assert "pytest.cmd" not in result["helper_handoff_fact"]["project_paths"]
    assert "python3.cmd" not in result["helper_handoff_fact"]["project_paths"]
    assert result["helper_handoff_fact"]["command_shim_paths"] == ["pytest.cmd", "python3.cmd"]
    hint = result["next_action_instruction"]
    assert "compact listing already exposes source/test/acceptance project paths" in hint
    assert "A parallel batch of main-thread env_read calls" in hint
    assert "duplicates code-helper work" in hint
    assert "主进程批量展开源码会重复 helper 工作" in hint


@pytest.mark.asyncio
async def test_env_search_source_matches_do_not_inject_handoff_hint(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("def run(payload):\n    return payload['customer_name']\n", encoding="utf-8")

    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )
    with runtime_context("environment", env):
        result = json.loads(await handle_environment_tool("env_search", str(tmp_path), {"query": "customer_name"}))

    assert result["ok"] is True
    assert "matched_project_paths" not in result
    assert "next_action_instruction" not in result





@pytest.mark.asyncio

async def test_env_fetch_source_file_returns_helper_handoff_hint(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    (project / "config_loader.py").write_text("def load_config():\n    return {}\n", encoding="utf-8")



    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        result = json.loads(await handle_environment_tool("env_fetch", str(workspace), {"path": "config_loader.py"}))



    assert result["ok"] is True

    assert result["workspace_path"] == "_env/config_loader.py"

    hint = result["next_action_instruction"]

    assert "env_fetch staged config_loader.py as _env/config_loader.py" in hint

    assert "already usable in input_files" in hint

    assert "use existing failure evidence or run a baseline only when useful" in hint

    assert "delegate the staged output to a helper with expected_outputs" in hint

    assert "narrow checks for main-owned changes" in hint

    assert "diagnose, edit, and test" in hint

    assert "项目源码修改通常交给 helper" in hint



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

    assert ok == {"ok": True, "queued": True, "aborted": True, "stage": "", "reason": ""}

    assert api._pop_interrupt_messages("arch_env", "env_user_u", "u") == ["停一下"]



    assert signaled == {"archive_id": "arch_env", "group_id": "env_user_u", "user_id": "u"}

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



def test_environment_event_sink_routes_helper_child_trace_by_parent_trace():

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

                "kind": "main_tool_done",

                "proc_type": "helper",

                "trace_id": "trace_a.helper_task",

                "parent_trace_id": "trace_a",

                "archive_id": "arch_trace_sink",

                "group_id": "env_user_u",

                "user_id": "u",

                "tool": "bash",

            })

            return await asyncio.wait_for(queue_a.get(), timeout=1), queue_b.empty()



    item, other_empty = asyncio.run(run())

    event_name, payload = item

    assert event_name == "workflow"

    assert payload["trace_id"] == "trace_a.helper_task"

    assert payload["parent_trace_id"] == "trace_a"

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


def test_env_run_normalizes_python_cmd_shim_multiline_python_c(tmp_path):
    from app.llm.tools.environment import _normalize_env_command

    command = 'python3.cmd -c "\\nprint(123)\\nprint(456)\\n"'
    translated, script_name, script_path = _normalize_env_command(command, tmp_path)

    assert translated.startswith("python3.cmd ")
    assert script_name.endswith(".py")
    assert script_path is not None
    script = script_path.read_text(encoding="utf-8")
    assert "sys.path[0] = ''" in script
    assert script.endswith("\nprint(123)\nprint(456)\n")





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

    assert result["recovery_facts"]["matching_helper_kind"] == "read"

    assert "source-material body extraction" in result["hint"]

    assert "read helper" in result["hint"]



@pytest.mark.asyncio

async def test_environment_run_allows_targeted_docx_output_validation(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    (project / "report.docx").write_bytes(b"not-a-real-docx")

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_targeted_docx_validation",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    code = """

import zipfile, re

path = 'report.docx'

print('validation check for headings, tables, forbidden path hygiene, and spot-check numbers')

print(path)

"""

    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_run",

            str(workspace),

            {"python_code": code, "timeout_sec": 5},

        )

    result = json.loads(raw)



    assert result["ok"] is True

    assert result.get("error_kind") != "main_thread_bulk_source_material_read_should_delegate"

    assert "validation check" in result["stdout"]





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





def test_env_run_empty_python_c_followup_fact_is_specific():

    from app.llm.tools.environment import _env_run_empty_output_followup_fact

    fact = _env_run_empty_output_followup_fact("python3 -c \"print('x')\"")

    assert fact is not None

    assert "python_code" in fact

    shim_fact = _env_run_empty_output_followup_fact("python3.cmd -c \"print('x')\"")

    assert shim_fact is not None

    assert "python_code" in shim_fact

    assert _env_run_empty_output_followup_fact("python verify_results.py") is None

    assert _env_run_empty_output_followup_fact(
        "python3 -c \"print('x')\"",
        python_code_used=True,
    ) is None


def test_env_run_python_data_file_fact_identifies_data_as_script():

    from app.llm.tools.environment import _env_run_python_data_file_fact

    fact = _env_run_python_data_file_fact('python3 users.db ".tables"')

    assert fact is not None

    assert fact["kind"] == "python_launcher_data_file_fact"

    assert fact["data_argument"] == "users.db"

    assert "first non-option argument as a Python script path" in fact["fact"]

    assert "not a SQLite/CSV/JSON data CLI invocation" in fact["fact"]

    assert _env_run_python_data_file_fact("python3 -c \"print('x')\"") is None

    assert _env_run_python_data_file_fact("python verify_results.py") is None

    assert _env_run_python_data_file_fact(
        "python3 users.db",
        python_code_used=True,
    ) is None


def test_env_run_python_non_python_script_fact_identifies_runner_shim_as_source_arg():

    from app.llm.tools.environment import _env_run_python_non_python_script_fact

    fact = _env_run_python_non_python_script_fact('python3 .\\python3.cmd -c "print(1)"')

    assert fact is not None

    assert fact["kind"] == "python_launcher_non_python_script_fact"

    assert fact["script_argument"] == ".\\python3.cmd"

    assert fact["script_suffix"] == ".cmd"

    assert "first non-option argument as Python source code" in fact["fact"]

    assert _env_run_python_non_python_script_fact("python3 verify_results.py") is None

    assert _env_run_python_non_python_script_fact("python3 -c \"print('x')\"") is None

    assert _env_run_python_non_python_script_fact(
        "python3 .\\python3.cmd",
        python_code_used=True,
    ) is None


def test_env_run_staged_path_fact_reports_project_and_workspace_existence(tmp_path):

    from app.llm.tools.environment import _env_run_staged_path_fact

    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    (workspace / "_env").mkdir()

    (workspace / "_env" / "users.db").write_text("staged", encoding="utf-8")

    fact = _env_run_staged_path_fact(
        'sqlite3 _env/users.db ".schema"',
        project_cwd=project,
        workspace_dir=str(workspace),
    )

    assert fact is not None

    assert fact["kind"] == "env_run_staged_path_fact"

    assert fact["paths"][0]["path"] == "_env/users.db"

    assert fact["paths"][0]["exists_in_env_run_project_cwd"] is False

    assert fact["paths"][0]["exists_in_chat_workspace"] is True

    assert "real project cwd" in fact["fact"]


@pytest.mark.asyncio

async def test_environment_run_reports_stdout_trailing_blank_lines(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.environment import handle_environment_tool



    project = tmp_path / "project"

    workspace = tmp_path / "workspace"

    project.mkdir()

    workspace.mkdir()

    env = EnvironmentContext(

        root_dir=str(project),

        archive_id="arch_test_trailing_blank_output",

        group_id="env_user_u",

        user_id="u",

        project_key="p",

    )

    with runtime_context("environment", env):

        raw = await handle_environment_tool(

            "env_run",

            str(workspace),

            {"python_code": "print('alpha')\nprint()\n", "timeout_sec": 5},

        )

    result = json.loads(raw)



    assert result["ok"] is True

    assert result["stdout"].replace("\r\n", "\n") == "alpha\n\n"

    assert result["stdout_facts"]["line_count"] == 2

    assert result["stdout_facts"]["ends_with_newline"] is True

    assert result["stdout_facts"]["trailing_blank_lines"] == 1

    assert "Exact stdout checks can fail" in result["stdout_exact_text_warning"]
    assert "use text comparison for text stdout checks" in result["stdout_exact_text_warning"]
    assert "只有明确要求字节级输出时才按 repr/字节处理" in result["stdout_exact_text_warning"]


@pytest.mark.asyncio
async def test_environment_run_long_stdout_saves_full_output(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test_long_stdout",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )
    with runtime_context("environment", env):
        raw = await handle_environment_tool(
            "env_run",
            str(workspace),
            {"python_code": "print('B' * 40000)", "timeout_sec": 5},
        )
    result = json.loads(raw)

    assert result["ok"] is True
    assert result["stdout_truncated"] is True
    assert len(result["stdout"]) <= 30000
    saved = workspace / result["stdout_full_saved_path"]
    assert saved.is_file()
    full = saved.read_text(encoding="utf-8")
    assert len(full) >= 40000
    assert full.rstrip("\n").endswith("B")




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

    assert {task["task_id"] for task in result} == {"snake_slice"}
    assert result[0]["mode"] == "easy"





@pytest.mark.asyncio

async def test_environment_code_helper_prompt_mentions_live_url_apply_boundary(tmp_path):

    from app.core.runtime_mode import EnvironmentContext, runtime_context

    from app.llm.tools.delegate import _sanitize_and_validate_tasks



    env = EnvironmentContext(

        root_dir=str(tmp_path / "project"),

        archive_id="env-user",

        group_id="env-user",

        user_id="env-user",

        project_key="webapp",

        project_name="WebApp",

    )

    with runtime_context("environment", env):

        result = await _sanitize_and_validate_tasks(

            {

                "tasks": [

                    {

                        "task_id": "fix_form",

                        "kind": "code",

                        "mode": "easy",

                        "prompt": "Fix _env/app.js and verify the running URL after the fix.",

                        "expected_outputs": ["_env/app.js"],

                        "acceptance_checks": ["running URL succeeds after fix"],

                    }

                ],

            },

            main_workspace=str(tmp_path),

            archive_id="env-user",

            group_id="env-user",

            user_id="env-user",

        )



    prompt = result[0]["prompt"]

    assert "helper `_env/...` edits do not affect that service until the main process applies" in prompt

    assert "final live URL success after apply is main-process evidence" in prompt
    assert "`_env` is the project root for imports" in prompt



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



    assert result["ok"] is True
    assert (workspace / "_env" / "tests" / "test_large.py").read_text(encoding="utf-8") == content





@pytest.mark.asyncio

async def test_environment_workspace_write_points_project_deliverables_to_env_apply(tmp_path):

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



    assert result["ok"] is False

    assert result["blocked_reason"] == "environment_workspace_write_not_project_file"

    assert result["chat_workspace_only"] is True
    assert result["project_file_created"] is False
    assert "did not create project file" in result["next_action_instruction"]
    assert result["delegate_required"] is True
    assert result["recovery_facts"]["matching_helper_kind"] == "edit"
    assert result["candidate_preserved"] is True
    assert (workspace / result["candidate_workspace_path"]).read_text(encoding="utf-8") == content

    assert not (workspace / "analysis_outputs" / "report.md").exists()


@pytest.mark.asyncio
async def test_environment_workspace_write_omits_large_suggested_content(tmp_path):
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
    content = "section body\n" * 500
    with runtime_context("environment", env):
        result = await handle_write(str(workspace), "analysis_outputs/report.md", content)

    assert result["ok"] is False
    assert result["blocked_reason"] == "environment_workspace_write_not_project_file"
    assert result["content_chars"] == len(content)
    assert result["content_omitted_from_suggestion"] is True
    assert result["recovery_facts"]["matching_helper_kind"] == "edit"
    assert result["delegate_required"] is True
    assert result["candidate_preserved"] is True
    candidate_path = workspace / result["candidate_workspace_path"]
    assert candidate_path.read_text(encoding="utf-8") == content
    assert result["recovery_facts"]["input_files"] == [result["candidate_workspace_path"]]
    assert "section body" not in result["recovery_facts"]["helper_prompt_fact"]
    assert not (workspace / "analysis_outputs" / "report.md").exists()


@pytest.mark.asyncio
async def test_env_apply_create_large_candidate_requires_ready_helper_provenance(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="env-user",
        group_id="env-user",
        user_id="env-user",
        project_key="candidate-apply",
    )
    content = "# Report\n\n" + ("section body\n" * 240)
    with runtime_context("environment", env):
        blocked = json.loads(await handle_environment_tool(
            "env_apply_create",
            str(workspace),
            {"path": "analysis_outputs/report.md", "content": content},
        ))

    assert blocked["ok"] is False
    assert blocked["error_kind"] == "main_thread_project_artifact_create_should_delegate"
    assert blocked["candidate_preserved"] is True
    assert blocked["recovery_facts"]["input_files"] == [blocked["candidate_workspace_path"]]
    assert "not a clean producer-owned output by itself" in blocked["candidate_handoff_fact"]

    with runtime_context("environment", env):
        applied = json.loads(await handle_environment_tool(
            "env_apply_create",
            str(workspace),
            {
                "path": "analysis_outputs/report.md",
                "workspace_path": blocked["candidate_workspace_path"],
            },
        ))

    assert applied["ok"] is False
    assert applied["error_kind"] == "staged_environment_file_without_ready_provenance"
    assert applied["workspace_path"] == blocked["candidate_workspace_path"]
    assert not (project / "analysis_outputs" / "report.md").exists()


@pytest.mark.asyncio
async def test_environment_workspace_write_small_project_note_keeps_apply_hint(tmp_path):
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
    content = "short note\n"
    with runtime_context("environment", env):
        result = await handle_write(str(workspace), "notes.txt", content)

    assert result["ok"] is False
    assert result["blocked_reason"] == "environment_workspace_write_not_project_file"
    assert result["recovery_facts"]["matching_tool_shape"] == "env_apply_create"
    assert result["recovery_facts"]["arguments"] == {
        "path": "notes.txt",
        "content": content,
    }
    assert not (workspace / "notes.txt").exists()


@pytest.mark.asyncio
async def test_environment_workspace_write_project_note_name_delegates_to_helper(tmp_path):
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
    content = "# API Notes\n\nObserved endpoint facts.\n"
    with runtime_context("environment", env):
        result = await handle_write(str(workspace), "api_notes.md", content)

    assert result["ok"] is False
    assert result["blocked_reason"] == "environment_workspace_write_not_project_file"
    assert result["delegate_required"] is True
    assert result["recovery_facts"]["expected_outputs"] == ["_env/api_notes.md"]
    assert result["candidate_preserved"] is True
    assert (workspace / result["candidate_workspace_path"]).read_text(encoding="utf-8") == content
    assert not (workspace / "api_notes.md").exists()


@pytest.mark.asyncio
async def test_env_run_reports_project_file_mutations_without_blocking(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test_env_run_mutation",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )
    with runtime_context("environment", env):
        raw = await handle_environment_tool(
            "env_run",
            str(workspace),
            {
                "python_code": "from pathlib import Path\nPath('active.csv').write_text('x\\n', encoding='utf-8')\nprint('done')\n",
            },
        )
    result = json.loads(raw)

    assert result["ok"] is True
    assert (project / "active.csv").read_text(encoding="utf-8") == "x\n"
    assert result["project_mutation_fact"]["kind"] == "env_run_project_mutation_fact"
    assert result["project_mutation_fact"]["created_project_files"] == ["active.csv"]
    assert result["project_mutations"]["created"] == ["active.csv"]
    assert "env_apply_create/env_apply_replace" in result["project_mutation_fact"]["fact"]


@pytest.mark.asyncio
async def test_env_apply_create_existing_preserves_candidate_for_replace(tmp_path):
    import hashlib

    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    target = project / "result.csv"
    target.write_text("channel,count\nold,1\n", encoding="utf-8")
    old_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test_apply_existing_candidate",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )
    candidate = "channel,count\nnew,2\n"

    with runtime_context("environment", env):
        raw = await handle_environment_tool(
            "env_apply_create",
            str(workspace),
            {"path": "result.csv", "content": candidate},
        )
        create_result = json.loads(raw)
        replace_raw = await handle_environment_tool(
            "env_apply_replace",
            str(workspace),
            {
                "path": "result.csv",
                "workspace_path": create_result["candidate_workspace_path"],
                "expected_hash": create_result["current_hash"],
            },
        )
        replace_result = json.loads(replace_raw)

    candidate_path = workspace / create_result["candidate_workspace_path"]
    assert create_result["ok"] is False
    assert create_result["error_kind"] == "env_apply_create_target_exists"
    assert create_result["current_hash"] == old_hash
    assert create_result["candidate_preserved"] is True
    assert candidate_path.read_text(encoding="utf-8") == candidate
    assert "project file was not changed" in create_result["candidate_preservation_fact"]
    assert replace_result["ok"] is True
    assert replace_result["path"] == "result.csv"
    assert target.read_text(encoding="utf-8") == candidate





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



    assert result["ok"] is True

    assert result["staged_project_copy"] is True

    assert result["staged_project_path"] == "_env/tests/test_existing.py"

    assert "env_diff" in result["suggested_next_tools"]

    assert existing.read_text(encoding="utf-8") == "VALUE = 2\n"





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





@pytest.mark.asyncio
async def test_env_apply_results_state_artifact_auto_registration(tmp_path):
    """Applied files are auto-recorded as ready artifacts; the result must say so
    to stop the main model from spending a turn on agent_state register_artifact
    (t4-cross-repo-migration 20260609_113326)."""
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    (project / "hello.txt").write_text("one\n", encoding="utf-8")

    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )
    with runtime_context("environment", env):
        fetched = json.loads(await handle_environment_tool("env_fetch", str(workspace), {"path": "hello.txt"}))
        (workspace / fetched["workspace_path"]).write_text("two\n", encoding="utf-8")
        applied = json.loads(await handle_environment_tool(
            "env_apply_replace",
            str(workspace),
            {"path": "hello.txt", "expected_hash": fetched["sha256"]},
        ))
        assert applied["ok"] is True
        assert applied["artifact_registered"] is True
        assert "register_artifact call for this path is redundant" in applied["artifact_fact"]
        assert applied["acceptance_fact"]["kind"] == "project_apply_acceptance_fact"
        assert applied["acceptance_fact"]["path"] == "hello.txt"
        assert applied["acceptance_fact"]["helper_owned"] is False
        assert "project state at the time it ran" in applied["acceptance_fact"]["fact"]
        assert "later project apply covers the earlier state" in applied["acceptance_fact"]["fact"]

        created = json.loads(await handle_environment_tool(
            "env_apply_create",
            str(workspace),
            {"path": "fresh.txt", "content": "new file\n"},
        ))
        assert created["ok"] is True
        assert created["artifact_registered"] is True
        assert "register_artifact call for this path is redundant" in created["artifact_fact"]
        assert created["acceptance_fact"]["helper_owned"] is False
        assert "direct main-thread content" in created["acceptance_fact"]["fact"]


@pytest.mark.asyncio
async def test_env_fetch_preserves_staged_copy_with_unapplied_edits(tmp_path):
    """Regression for 20260610_134512: a parallel env_diff + env_fetch turn let
    env_fetch overwrite the helper-edited `_env/...` copy with pristine project
    content, so env_apply_replace applied an unchanged file and the task burned
    two extra helper rounds re-producing the fix."""
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    (project / "config_loader.py").write_text("original\n", encoding="utf-8")

    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )
    with runtime_context("environment", env):
        fetched = json.loads(await handle_environment_tool(
            "env_fetch", str(workspace), {"path": "config_loader.py"}
        ))
        assert fetched["ok"] is True
        staged = workspace / fetched["workspace_path"]
        staged.write_text("edited by helper\n", encoding="utf-8")

        refetch = json.loads(await handle_environment_tool(
            "env_fetch", str(workspace), {"path": "config_loader.py"}
        ))
        assert refetch["ok"] is True
        assert refetch["staged_copy_preserved"] is True
        assert staged.read_text(encoding="utf-8") == "edited by helper\n"
        assert "env_fetch kept it instead of overwriting" in refetch["fact"]
        assert refetch["staged_sha256"] != refetch["source_sha256"]

        forced = json.loads(await handle_environment_tool(
            "env_fetch", str(workspace), {"path": "config_loader.py", "force": True}
        ))
        assert forced["ok"] is True
        assert "staged_copy_preserved" not in forced
        assert staged.read_text(encoding="utf-8") == "original\n"

        # Unchanged staged copy refetches normally without the preservation fact.
        again = json.loads(await handle_environment_tool(
            "env_fetch", str(workspace), {"path": "config_loader.py"}
        ))
        assert again["ok"] is True
        assert "staged_copy_preserved" not in again


@pytest.mark.asyncio
async def test_env_fetch_missing_target_with_staged_copy_points_to_apply_create(tmp_path):
    """20260610_163156: the model env_fetched a not-yet-existing project file to
    get an expected_hash before creating it; two path-not-found errors dinged
    recovery. A new project file needs no fetch — point at env_apply_create."""
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    staged = workspace / "triage_report.md"
    staged.write_text("# report\n", encoding="utf-8")

    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )
    with runtime_context("environment", env):
        result = json.loads(await handle_environment_tool(
            "env_fetch", str(workspace), {"path": "triage_report.md"}
        ))

    assert result["ok"] is False
    assert result["staged_copy_exists"] == "triage_report.md"
    assert "env_apply_create" in result["next_action"]
    assert "needs no fetch/hash" in result["next_action"] or "need no fetch/hash" in result["next_action"]
