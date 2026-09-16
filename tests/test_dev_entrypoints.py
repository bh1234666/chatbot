from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_makefile_uses_repo_python_for_core_targets():
    text = (ROOT / "Makefile").read_text(encoding="utf-8", errors="ignore")

    assert "PYTHON_RUN = powershell -NoProfile -ExecutionPolicy Bypass -File scripts/repo_python.ps1" in text
    for target in (
        "$(PYTHON_RUN) -m pip install -r requirements.txt",
        "$(PYTHON_RUN) -m pytest",
        "$(PYTHON_RUN) -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1",
        "$(PYTHON_RUN) scripts/create_local_snapshot.py",
        "$(PYTHON_RUN) scripts/clean_generated.py",
    ):
        assert target in text


def test_precheck_script_uses_repo_python_and_supports_healthcheck():
    text = (ROOT / "scripts" / "precheck.ps1").read_text(encoding="utf-8", errors="ignore")

    assert '$pythonRunner = Join-Path $PSScriptRoot "repo_python.ps1"' in text
    assert '--check' in text
    assert '-HealthCheck' in text
    assert 'Write-Host "precheck.ps1 OK"' in text


@pytest.mark.skipif(sys.platform != "win32", reason="precheck health check is Windows-specific")
def test_precheck_healthcheck():
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "precheck.ps1"),
            "--check",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "precheck.ps1 OK" in result.stdout
