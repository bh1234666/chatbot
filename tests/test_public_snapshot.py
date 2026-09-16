from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "public_snapshot", Path(__file__).resolve().parents[1] / "scripts/export_public_snapshot.py"
)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def write(root: Path, relative: str, text: str = "fixture\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_snapshot_keeps_runtime_files_and_excludes_local_material(tmp_path, monkeypatch):
    source, output = tmp_path / "source", tmp_path / "public"
    expected = {
        ".env.example", "app/main.py", "scripts/repo_python.bat",
        "scripts/repo_python.ps1", "scripts/qq_project_mode.py",
        "scripts/ocr_benchmark/generate_cases.py", "start_no_gpu.bat",
        "startbot_full.bat", "startbot_full_nogpu.bat", "startbot_nogpu.bat",
        "agent_frontend/src/app.js", "config/matplotlib/matplotlibrc",
        "tests/test_public_snapshot.py",
    }
    private = {
        ".env", "cred.json", "config/.env.production", "config/api.private.json",
        "config/server.key", "config/matplotlib/fontlist-v390.json",
        "scripts/local_operation.py", "agent_frontend/.cleanup_state.json",
        "agent_frontend/src/app.js.bak_previous", "agent_frontend/src/app.repair_try.js",
        "tests/test_j03_wire_id_compat.py", "tests/test_sqlite_composite_benchmark_contract.py",
        "app/__pycache__/cached.pyc", "data/chatbot.db",
    }
    for relative in expected | private:
        write(source, relative)
    monkeypatch.setattr(exporter, "ROOT", source)
    actual = {dst.relative_to(output).as_posix() for _, dst in exporter.build_plan(output)}
    assert actual == expected


def test_public_rewrites_remove_account_and_private_endpoint_without_editing_source(tmp_path):
    source, output = tmp_path / "source", tmp_path / "public"
    account = "9876" + "543210"
    endpoint = "https://chat." + "ekti.cc/v1"
    original = f'if not defined QQ_BOT_NUM set "QQ_BOT_NUM={account}"\n'
    paths = ["start.bat", "startbot.bat", "start_qqbot.bat"]
    for relative in paths:
        write(source, relative, original)
    write(source, ".env.example", f"GPT55_BASE_URL={endpoint}\n")
    exporter.perform_copy([(source / rel, output / rel) for rel in [*paths, ".env.example"]])
    exporter.apply_rewrites(output)
    assert (source / "startbot.bat").read_text() == original
    for relative in paths:
        text = (output / relative).read_text()
        assert account not in text
        assert 'set /p "QQ_BOT_NUM=' in text
    assert endpoint not in (output / ".env.example").read_text()
    assert exporter.scan_secrets(output) == []


@pytest.mark.parametrize("kind", ["api", "github", "qq", "private_key"])
def test_scan_reports_location_without_repeating_secret(tmp_path, kind):
    values = {
        "api": "sk-proj-" + "aB_12-" * 12,
        "github": "ghp_" + "abc123" * 8,
        "qq": "QQ_BOT_NUM=" + "9876" + "543210",
        "private_key": "-----BEGIN " + "PRIVATE KEY-----",
    }
    secret = values[kind]
    write(tmp_path, "config/example.txt", "first line\n" + secret + "\n")
    findings = exporter.scan_secrets(tmp_path)
    assert len(findings) == 1
    assert findings[0][0].relative_to(tmp_path).as_posix() == "config/example.txt"
    assert findings[0][2] == "line 2 (value redacted)"
    assert secret not in repr(findings)


@pytest.mark.parametrize("destination", ["source", "child", "parent"])
def test_force_refuses_overlapping_source_paths(tmp_path, monkeypatch, destination):
    source = tmp_path / "source"
    sentinel = write(source, "keep.txt")
    output = {"source": source, "child": source / "public", "parent": tmp_path}[destination]
    monkeypatch.setattr(exporter, "ROOT", source)
    monkeypatch.setattr(sys, "argv", ["export", "--out", str(output), "--force"])
    assert exporter.main() == 2
    assert sentinel.read_text() == "fixture\n"


def test_refresh_preserves_git_history_and_replaces_only_output(tmp_path, monkeypatch):
    source, output = tmp_path / "source", tmp_path / "public"
    write(source, "README.md", "updated\n")
    write(output, "stale.txt")
    git_head = write(output, ".git/HEAD", "ref: refs/heads/main\n")
    monkeypatch.setattr(exporter, "ROOT", source)
    monkeypatch.setattr(sys, "argv", ["export", "--out", str(output), "--force"])
    assert exporter.main() == 0
    assert git_head.read_text() == "ref: refs/heads/main\n"
    assert not (output / "stale.txt").exists()
    assert (output / "README.md").read_text() == "updated\n"
    assert (source / "README.md").read_text() == "updated\n"
