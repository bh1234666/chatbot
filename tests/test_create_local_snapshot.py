from __future__ import annotations

import importlib.util
from pathlib import Path
from zipfile import ZipFile


def _load_snapshot_module():
    path = Path("scripts/create_local_snapshot.py")
    spec = importlib.util.spec_from_file_location("create_local_snapshot", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_should_include_filters_transient_and_reserved_names():
    module = _load_snapshot_module()

    assert module.should_include(Path("app/main.py"))
    assert not module.should_include(Path("backups/existing.zip"))
    assert not module.should_include(Path("tests/__pycache__/x.pyc"))
    assert not module.should_include(Path("stress_tools/runs/result.json"))
    assert not module.should_include(Path("app/llm/stuck_helper/nul"))
    assert not module.should_include(Path(".env"))


def test_create_snapshot_writes_manifest_and_project_files(tmp_path):
    module = _load_snapshot_module()
    root = tmp_path / "repo"
    out_dir = tmp_path / "out"
    (root / "app").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "backups").mkdir()
    (root / "app" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / "logs" / "debug.log").write_text("ignore\n", encoding="utf-8")
    (root / "backups" / "old.zip").write_text("ignore\n", encoding="utf-8")

    archive_path = module.create_snapshot(root, out_dir)

    assert archive_path.exists()
    with ZipFile(archive_path) as zf:
        names = set(zf.namelist())
    assert "app/main.py" in names
    assert "README.md" in names
    assert "SNAPSHOT_MANIFEST.json" in names
    assert "logs/debug.log" not in names
    assert "backups/old.zip" not in names


def test_create_snapshot_cleans_up_temp_archive_and_replaces_atomically(tmp_path):
    module = _load_snapshot_module()
    root = tmp_path / "repo"
    out_dir = tmp_path / "out"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("print('ok')\n", encoding="utf-8")

    archive_path = module.create_snapshot(root, out_dir)

    assert archive_path.exists()
    assert archive_path.suffix == ".zip"
    assert not archive_path.with_suffix(".zip.tmp").exists()
