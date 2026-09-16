from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "repo_python.ps1"


def _run_launcher(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LAUNCHER),
            *args,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_repo_python_launcher_supports_inline_code_execution():
    result = _run_launcher("-c", "print('launcher-ok')")

    assert result.returncode == 0
    assert "launcher-ok" in result.stdout


def test_repo_python_launcher_passes_double_dash_script_arguments():
    result = _run_launcher("scripts/create_local_snapshot.py", "--check")

    assert result.returncode == 0
    assert "create_local_snapshot.py OK" in result.stdout


def test_repo_python_launcher_healthcheck_reports_resolved_interpreter():
    result = _run_launcher("-HealthCheck")

    assert result.returncode == 0
    assert "OK " in result.stdout


def test_repo_python_launcher_print_path_outputs_python_executable():
    result = _run_launcher("-PrintPath")

    assert result.returncode == 0
    assert "python.exe" in result.stdout.lower()
