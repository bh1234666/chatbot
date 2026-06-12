import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_analyze_command_allows_common_build_and_test_commands(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    ws = str(tmp_path)

    assert analyze_command("python script.py", ws).allowed
    assert analyze_command("npm test", ws).allowed
    assert analyze_command("gcc main.c -o app.exe", ws).allowed
    assert analyze_command("cmd /c dir /b", ws).allowed


def test_analyze_command_allows_compiler_format_flags_and_python_format_method(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    ws = str(tmp_path)

    assert analyze_command("gcc main.c -Wall -Wformat -o app.exe", ws).allowed
    assert analyze_command("python -c \"print('{} {}'.format(1, 2))\"", ws).allowed


def test_analyze_command_blocks_format_executable_after_cmd_switches(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    ws = str(tmp_path)

    for command in (
        "format C:",
        "cmd /c format C:",
        "cmd /d /c format C:",
        "cmd /q /d /s /c format C:",
    ):
        decision = analyze_command(command, ws)
        assert not decision.allowed, command
        assert decision.category == "blocked_keyword"


def test_analyze_command_blocks_high_risk_system_commands(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    ws = str(tmp_path)

    decision = analyze_command("powershell -Command Get-Process", ws)
    assert not decision.allowed
    assert decision.category == "blocked_keyword"

    decision = analyze_command("shutdown /s /t 0", ws)
    assert not decision.allowed


def test_analyze_command_blocks_outside_writes(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    ws = str(tmp_path)
    outside = tmp_path.parent / "outside.txt"

    decision = analyze_command(f"cmd /c echo hello > {outside}", ws)
    assert not decision.allowed
    assert decision.category == "outside_redirect"

    decision = analyze_command(f"gcc main.c -o {outside}", ws)
    assert not decision.allowed
    assert decision.category == "outside_output"


def test_analyze_command_blocks_helper_prev_access(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    prev_file = tmp_path / ".prev" / "old.txt"
    decision = analyze_command(f"cmd /c type {prev_file}", str(tmp_path), is_main_thread=False)

    assert not decision.allowed
    assert decision.category == "helper_scope"


def test_analyze_command_allows_main_thread_prev_read(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    prev_file = tmp_path / ".prev" / "old.txt"
    decision = analyze_command(f"cmd /c type {prev_file}", str(tmp_path), is_main_thread=True)

    assert decision.allowed


def test_analyze_command_blocks_dangerous_cmd_keywords_even_with_suffix(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    for command in (
        "reg delete HKCU\\Software\\Example /f",
        "taskkill /IM python.exe /F",
        "msiexec /i package.msi",
    ):
        decision = analyze_command(command, str(tmp_path))
        assert not decision.allowed
        assert decision.category == "blocked_keyword"


def test_analyze_command_allows_workspace_redirect_and_blocks_outside_destructive(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    inside = tmp_path / "out.txt"
    outside = tmp_path.parent / "outside.txt"

    assert analyze_command(f"cmd /c echo hello > {inside}", str(tmp_path)).allowed

    decision = analyze_command(f"cmd /c del {outside}", str(tmp_path))
    assert not decision.allowed
    assert decision.category == "outside_destructive"


def test_analyze_command_blocks_bare_cmd_builtin_outside_writes(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    inside = tmp_path / "inside.txt"
    outside = tmp_path.parent / "outside.txt"

    assert analyze_command(f"ren {inside} renamed.txt", str(tmp_path)).allowed

    decision = analyze_command(f"ren {outside} outside.off", str(tmp_path))
    assert not decision.allowed
    assert decision.category == "outside_destructive"

    decision = analyze_command(f"python script.py > {outside}", str(tmp_path))
    assert not decision.allowed
    assert decision.category == "outside_redirect"


def test_analyze_command_allows_null_device_redirects(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    ws = str(tmp_path)

    assert analyze_command("ls -la *.docx 2>/dev/null | head -20", ws).allowed
    assert analyze_command("cmd /c dir missing 2>nul", ws).allowed

    outside = tmp_path.parent / "outside.txt"
    decision = analyze_command(f"python script.py 2>{outside}", ws)
    assert not decision.allowed
    assert decision.category == "outside_redirect"


def test_analyze_command_blocks_helper_unix_copy_to_project_path(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    helper_ws = tmp_path / ".temp" / "_delegate_user_env_tests"
    (helper_ws / "_env").mkdir(parents=True)

    decision = analyze_command(
        "cp _env/app.js /f/chatbot/stress_tools/runs/current/state/workspace/project/app.js 2>/dev/null",
        str(helper_ws),
        is_main_thread=False,
    )

    assert not decision.allowed
    assert decision.category == "helper_scope"
    assert "staged local `_env/...` copy" in decision.reason


def test_analyze_command_blocks_helper_python_access_to_project_path(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    helper_ws = tmp_path / ".temp" / "_delegate_user_env_tests"
    (helper_ws / "_env").mkdir(parents=True)

    for command in (
        "python -c \"p='F:/chatbot/project/app.js'; print(open(p).readline())\"",
        "python -c \"p='F:/chatbot/project/app.js'; open(p, 'w').write('x')\"",
    ):
        decision = analyze_command(command, str(helper_ws), is_main_thread=False)
        assert not decision.allowed, command
        assert decision.category == "helper_scope"


def test_analyze_command_allows_helper_url_arguments(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    helper_ws = tmp_path / ".temp" / "_delegate_user_env_tests"
    helper_ws.mkdir(parents=True)

    commands = (
        "curl -s http://127.0.0.1:61422/app.js 2>/dev/null | head -3",
        "cd _env && curl -s http://127.0.0.1:9123/health",
    )

    for command in commands:
        decision = analyze_command(command, str(helper_ws), is_main_thread=False)
        assert decision.allowed, command


def test_analyze_command_allows_helper_unix_copy_inside_workspace(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    helper_ws = tmp_path / ".temp" / "_delegate_user_env_tests"
    (helper_ws / "_env").mkdir(parents=True)

    decision = analyze_command(
        "cp _env/app.js _env/app.fixed.js 2>/dev/null",
        str(helper_ws),
        is_main_thread=False,
    )

    assert decision.allowed


def test_analyze_command_allows_unix_null_device_redirect_on_destructive_ops(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    decision = analyze_command("cp _env/app.js _env/app.fixed.js 2>/dev/null", str(tmp_path))

    assert decision.allowed




def test_analyze_command_blocks_helper_parent_read_access(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    helper_ws = tmp_path / ".temp" / "_delegate_user_chart"
    helper_ws.mkdir(parents=True)

    for command in (
        "cmd /c type ..\\bench_runner_assemble_bench_out.csv",
        "cmd /c dir /s /b ..\\bench_runner_assemble*",
        "cmd /c copy ..\\bench_runner_assemble_bench_out.csv bench_runner_assemble_bench_out.csv",
    ):
        decision = analyze_command(command, str(helper_ws), is_main_thread=False)
        assert not decision.allowed
        assert decision.category == "helper_scope"
        assert "fetch_to_temp(source='main'" in decision.reason


def test_analyze_command_points_environment_helper_to_env_copy(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    helper_ws = tmp_path / ".temp" / "_delegate_user_env_tests"
    helper_ws.mkdir(parents=True)
    project_root = tmp_path / "project"
    project_root.mkdir()
    target = project_root / "_env" / "app" / "tests" / "test_environment.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_x(): pass\n", encoding="utf-8")

    decision = analyze_command(
        f"cmd /c type {target}",
        str(helper_ws),
        is_main_thread=False,
    )

    assert not decision.allowed
    assert decision.category == "helper_scope"
    assert "staged local `_env/...` copy" in decision.reason
    assert "commands run from the helper sandbox" in decision.reason
    assert "cd _env/" in decision.reason
    assert "fetch_to_temp(source='main'" not in decision.reason
    assert "resource" not in decision.reason.lower()


def test_analyze_command_points_environment_helper_absolute_project_path_to_env_copy(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    helper_ws = tmp_path / ".temp" / "_delegate_user_env_tests"
    (helper_ws / "_env").mkdir(parents=True)
    project_root = tmp_path / "project"
    target = project_root / "app" / "tests" / "test_environment.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_x(): pass\n", encoding="utf-8")

    decision = analyze_command(
        f"cmd /c type {target}",
        str(helper_ws),
        is_main_thread=False,
    )

    assert not decision.allowed
    assert decision.category == "helper_scope"
    assert "staged local `_env/...` copy" in decision.reason
    assert "commands run from the helper sandbox" in decision.reason
    assert "cd _env/" in decision.reason
    assert "fetch_to_temp(source='main'" not in decision.reason


def test_analyze_command_allows_helper_local_read_access(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    helper_ws = tmp_path / ".temp" / "_delegate_user_chart"
    helper_ws.mkdir(parents=True)

    decision = analyze_command(
        "cmd /c type bench_runner_assemble_bench_out.csv",
        str(helper_ws),
        is_main_thread=False,
    )

    assert decision.allowed


def test_analyze_command_blocks_gcc_risky_plugin_inputs(tmp_path):
    from app.llm.tools.command_risk import analyze_command

    for command in (
        "gcc main.c -fplugin=evil.dll",
        "gcc main.c @args.txt",
        "gcc main.c -specs evil.spec",
    ):
        decision = analyze_command(command, str(tmp_path))
        assert not decision.allowed
        assert decision.category in {"gcc_blocked_flag", "gcc_at_file"}


def test_analyze_command_blocks_main_environment_python_write_to_staged_project_copy(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.command_risk import analyze_command

    (tmp_path / "_env").mkdir()
    command = "python -c \"open('_env/app.js','w').write('fixed')\""
    env = EnvironmentContext(
        root_dir=str(tmp_path),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )

    with runtime_context("environment", env):
        decision = analyze_command(command, str(tmp_path), is_main_thread=True)

    assert not decision.allowed
    assert decision.category == "main_thread_env_project_edit_should_delegate"
    assert "Fact:" in decision.reason
    assert "_env/..." in decision.reason


def test_analyze_command_blocks_main_environment_redirect_to_staged_project_copy(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.command_risk import analyze_command

    (tmp_path / "_env").mkdir()
    env = EnvironmentContext(
        root_dir=str(tmp_path),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )

    with runtime_context("environment", env):
        decision = analyze_command("cmd /c echo fixed > _env/app.js", str(tmp_path), is_main_thread=True)

    assert not decision.allowed
    assert decision.category == "main_thread_env_project_edit_should_delegate"


def test_analyze_command_allows_main_environment_read_and_validation_against_staged_copy(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.command_risk import analyze_command

    (tmp_path / "_env").mkdir()
    env = EnvironmentContext(
        root_dir=str(tmp_path),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )

    with runtime_context("environment", env):
        assert analyze_command("python -c \"print(open('_env/app.js').read())\"", str(tmp_path), is_main_thread=True).allowed
        assert analyze_command("cmd /c type _env\\app.js", str(tmp_path), is_main_thread=True).allowed
        assert analyze_command("cd _env && npm test", str(tmp_path), is_main_thread=True).allowed


def test_analyze_command_allows_helper_environment_write_to_local_staged_output(tmp_path):
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.command_risk import analyze_command

    helper_ws = tmp_path / ".temp" / "_delegate_user_env_tests"
    (helper_ws / "_env").mkdir(parents=True)
    env = EnvironmentContext(
        root_dir=str(tmp_path),
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )

    with runtime_context("environment", env):
        decision = analyze_command(
            "python -c \"open('_env/app.js','w').write('fixed')\"",
            str(helper_ws),
            is_main_thread=False,
        )

    assert decision.allowed
