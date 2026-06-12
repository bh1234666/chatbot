import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.mark.asyncio
async def test_workspace_run_blocks_helper_python_c_outside_project_path(tmp_path):
    from app.core.core_processes import reset_current_owner, set_current_owner
    from app.llm.tools import workspace_run

    helper_ws = tmp_path / ".temp" / "_delegate_user_env_tests"
    (helper_ws / "_env").mkdir(parents=True)

    token = set_current_owner("helper:test:env")
    try:
        result = await workspace_run.handle_run(
            str(helper_ws),
            "python -c \"open('F:/chatbot/project/app.js', 'w').write('x')\"",
            timeout_sec=5,
        )
    finally:
        reset_current_owner(token)

    assert result["ok"] is False
    assert result.get("blocked_reason") == "helper_scope"


@pytest.mark.asyncio
async def test_workspace_run_blocks_main_environment_python_write_to_staged_project_copy(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools import workspace_run

    (tmp_path / "_env").mkdir()
    target = tmp_path / "_env" / "app.js"
    target.write_text("old\n", encoding="utf-8")
    env = EnvironmentContext(
        root_dir=str(tmp_path),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )

    with runtime_context("environment", env):
        result = await workspace_run.handle_run(
            str(tmp_path),
            "python -c \"open('_env/app.js','w').write('new')\"",
            timeout_sec=5,
        )

    assert result["ok"] is False
    assert result.get("blocked_reason") == "main_thread_env_project_edit_should_delegate"
    assert target.read_text(encoding="utf-8") == "old\n"
