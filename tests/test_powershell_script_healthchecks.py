from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell health checks are Windows-specific")
@pytest.mark.parametrize(
    ("script_rel", "args", "marker"),
    [
        ("scripts/repo_python.ps1", ["-HealthCheck"], "OK "),
        ("scripts/precheck.ps1", ["-Check"], "precheck.ps1 OK"),
        ("scripts/cleanup_generated.ps1", ["-Check"], "cleanup_generated.ps1 OK"),
        ("stop_all_services.ps1", ["-Check"], "stop_all_services.ps1 OK"),
    ],
)
def test_powershell_script_healthchecks(script_rel: str, args: list[str], marker: str):
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / script_rel),
            *args,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert marker in result.stdout, script_rel
