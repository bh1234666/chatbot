import json

import pytest


def test_stage3_placeholder_token_hits_skip_natural_words():
    from stress_tools.run_db_paper_stage3_docx import placeholder_token_hits

    text = "Efficient insertion and deletion are normal database terms."

    assert placeholder_token_hits(text) == []
    assert placeholder_token_hits("TODO: replace this INSERT text") == ["TODO", "INSERT"]


@pytest.mark.asyncio
async def test_environment_run_absolute_path_failure_reports_path_fact(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    missing_external = (tmp_path / "old_run" / "_env" / "bench_results" / "rbt.csv").resolve()
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test_absolute_path_hint",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )
    code = f"open({str(missing_external)!r}).read()"

    with runtime_context("environment", env):
        raw = await handle_environment_tool(
            "env_run",
            str(workspace),
            {"python_code": code, "timeout_sec": 5},
        )
    result = json.loads(raw)

    assert result["ok"] is False
    assert "absolute filesystem path" in result["FIX_HINT"]
    assert "project-relative paths" in result["FIX_HINT"]
    assert "workspace tools" in result["FIX_HINT"]


@pytest.mark.asyncio
async def test_env_run_allows_single_docx_targeted_validation_probe(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.environment import handle_environment_tool

    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    (project / "paper.docx").write_bytes(b"PK\x03\x04fake-docx")
    env = EnvironmentContext(
        root_dir=str(project),
        archive_id="arch_test_single_docx_validation",
        group_id="env_user_u",
        user_id="u",
        project_key="p",
    )
    code = """
import zipfile, re, os
path = "paper.docx"
print("validation check for sections, tables, forbidden placeholders")
print(os.path.exists(path))
try:
    with zipfile.ZipFile(path) as z:
        print("tables", len([n for n in z.namelist() if n.endswith(".xml")]))
except Exception as exc:
    print(type(exc).__name__)
"""

    with runtime_context("environment", env):
        raw = await handle_environment_tool(
            "env_run",
            str(workspace),
            {"python_code": code, "timeout_sec": 5},
        )
    result = json.loads(raw)

    assert result.get("error_kind") != "main_thread_bulk_source_material_read_should_delegate"
    assert "validation check" in result.get("stdout", "")
