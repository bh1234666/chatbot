from pathlib import Path

import pytest


def test_main_thread_blocks_windows_copy_from_helper_sandbox(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    ws = tmp_path
    helper = ws / ".temp" / "_delegate_user_task" / "_env" / "src"
    helper.mkdir(parents=True)
    (helper / "module.py").write_text("print('x')\n", encoding="utf-8")

    decision = analyze_command(
        r'copy ".temp\_delegate_user_task\_env\src\module.py" "_env\src\module.py" /Y',
        str(ws),
        is_main_thread=True,
    )

    assert not decision.allowed
    assert decision.category == "helper_sandbox_copy"
    assert "file_map" in decision.reason
    assert "main_available_files" in decision.reason


def test_main_thread_blocks_python_copy_from_helper_sandbox(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    ws = tmp_path
    command = (
        "python -c \"import shutil; "
        "shutil.copy('.temp/_delegate_user_task/_env/src/module.py', '_env/src/module.py')\""
    )

    decision = analyze_command(command, str(ws), is_main_thread=True)

    assert not decision.allowed
    assert decision.category == "helper_sandbox_copy"
    assert "resume/replace" in decision.reason


def test_main_thread_can_read_helper_sandbox_for_diagnostics(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    ws = tmp_path
    helper_file = ws / ".temp" / "_delegate_user_task" / "helper.log"
    helper_file.parent.mkdir(parents=True)
    helper_file.write_text("diagnostic\n", encoding="utf-8")

    decision = analyze_command(f'cmd /c type "{helper_file}"', str(ws), is_main_thread=True)

    assert decision.allowed


def test_helper_local_copy_inside_own_sandbox_still_allowed(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    helper_ws = tmp_path / ".temp" / "_delegate_user_task"
    helper_ws.mkdir(parents=True)

    decision = analyze_command("copy local.txt local2.txt /Y", str(helper_ws), is_main_thread=False)

    assert decision.allowed


@pytest.mark.asyncio
async def test_workspace_run_blocks_python_copy_before_command_translation(tmp_path):
    from app.llm.tools.workspace_run import handle_run

    ws = tmp_path
    command = (
        "python -c \"import shutil; "
        "shutil.copy('.temp/_delegate_user_task/_env/src/module.py', '_env/src/module.py')\""
    )

    result = await handle_run(str(ws), command, timeout_sec=5)

    assert result["ok"] is False
    assert result["blocked_reason"] == "helper_sandbox_copy"
    assert not list(Path(ws).glob("_py_cmd_*.py"))
