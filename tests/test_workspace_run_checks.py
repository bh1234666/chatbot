from pathlib import Path


def test_helper_scope_check_allows_url_arguments(tmp_path):
    from app.llm.tools import workspace_run_checks as checks

    ws = tmp_path / "helper"
    (ws / "_env").mkdir(parents=True)

    checks.set_main_thread_provider(lambda: False)
    try:
        assert checks._touches_prev_or_outside(
            "node verify_form.cjs http://127.0.0.1:1234/",
            str(ws),
        ) is False
        assert checks._security_check(
            "node verify_form.cjs http://127.0.0.1:1234/",
            str(ws),
        ) is None
        assert checks._touches_prev_or_outside(
            "cd _env && node verify_form.cjs http://127.0.0.1:1234/",
            str(ws),
        ) is False
        assert checks._security_check(
            "cd _env && node verify_form.cjs http://127.0.0.1:1234/",
            str(ws),
        ) is None
    finally:
        checks.set_main_thread_provider(None)


def test_helper_scope_check_still_blocks_absolute_paths(tmp_path):
    from app.llm.tools import workspace_run_checks as checks

    ws = tmp_path / "helper"
    ws.mkdir(parents=True)
    outside = Path.cwd().anchor + "outside_project_file.js"

    checks.set_main_thread_provider(lambda: False)
    try:
        error = checks._security_check(f"node {outside}", str(ws))
    finally:
        checks.set_main_thread_provider(None)

    assert error is not None
    assert "helpers cannot access" in error


def test_security_check_allows_python_start_method(tmp_path):
    from app.llm.tools import workspace_run_checks as checks

    command = (
        "python -c \"from playwright.sync_api import sync_playwright; "
        "p = sync_playwright().start(); print(p.chromium)\""
    )

    assert checks._security_check(command, str(tmp_path)) is None


def test_security_check_still_blocks_start_executable(tmp_path):
    from app.llm.tools import workspace_run_checks as checks

    for command in (
        "start python server.py",
        "cmd /c start python server.py",
        "cmd /d /c start python server.py",
    ):
        error = checks._security_check(command, str(tmp_path))
        assert error is not None, command
        assert "restricted executable 'start'" in error
        assert "Foreground-run" in error or "前台运行" in error
