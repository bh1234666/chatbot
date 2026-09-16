from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform != "win32", reason="batch health checks are Windows-specific")
@pytest.mark.parametrize(
    ("script_name", "args", "expected_text"),
    [
        ("start.bat", ["--check"], "start.bat OK"),
        ("startbot.bat", ["--check"], "startbot.bat OK"),
        ("start_backend.bat", ["--check"], "start_backend.bat OK"),
        ("start_agent.bat", ["--check"], "start_agent.bat OK"),
        ("start_qqbot.bat", ["--check"], "start_qqbot.bat OK"),
        ("botctl.bat", ["--check"], "botctl.bat OK"),
        ("auto_publish.bat", ["--check"], "auto_publish.bat OK"),
        ("export_public_snapshot.bat", ["--check"], "export_public_snapshot.bat OK"),
        ("cleanup_generated.bat", ["--check"], "cleanup_generated.bat OK"),
        ("stop_all_services.bat", ["--check"], "stop_all_services.ps1 OK"),
        ("switch_model_pool.bat", ["--check"], "switch_model_pool.bat OK"),
        ("open_agent.bat", ["--check"], "open_agent.bat OK"),
    ],
)
def test_batch_script_healthchecks(script_name: str, args: list[str], expected_text: str):
    result = subprocess.run(
        ["cmd", "/c", script_name, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert expected_text in result.stdout
