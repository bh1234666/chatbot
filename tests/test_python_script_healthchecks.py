from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON_RUNNER = ROOT / "scripts" / "repo_python.ps1"


def _run_python_script_check(script_rel: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PYTHON_RUNNER),
            script_rel,
            "--check",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_python_script_healthchecks():
    scripts = [
        ("scripts/clean_generated.py", "clean_generated.py OK"),
        ("scripts/create_local_snapshot.py", "create_local_snapshot.py OK"),
        ("scripts/export_public_snapshot.py", "export_public_snapshot.py OK"),
        ("scripts/cache_report.py", "cache_report.py OK"),
    ]

    for script_rel, marker in scripts:
        result = _run_python_script_check(script_rel)
        assert result.returncode == 0, f"{script_rel}: {result.stderr or result.stdout}"
        assert marker in result.stdout, script_rel
