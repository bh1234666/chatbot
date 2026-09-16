from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_clean_module():
    path = Path("scripts/clean_generated.py")
    spec = importlib.util.spec_from_file_location("clean_generated", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_find_cleanup_targets_locates_common_cache_dirs(tmp_path):
    module = _load_clean_module()
    (tmp_path / "app" / "__pycache__").mkdir(parents=True)
    (tmp_path / "tests" / ".pytest_cache").mkdir(parents=True)
    (tmp_path / "docs").mkdir()

    targets = module.find_cleanup_targets(tmp_path)
    rels = {path.relative_to(tmp_path).as_posix() for path in targets}

    assert "app/__pycache__" in rels
    assert "tests/.pytest_cache" in rels
    assert "docs" not in rels


def test_remove_targets_deletes_cache_dirs(tmp_path):
    module = _load_clean_module()
    pycache_dir = tmp_path / "app" / "__pycache__"
    pytest_cache_dir = tmp_path / "tests" / ".pytest_cache"
    pycache_dir.mkdir(parents=True)
    pytest_cache_dir.mkdir(parents=True)

    removed = module.remove_targets([pycache_dir, pytest_cache_dir])

    assert removed == 2
    assert not pycache_dir.exists()
    assert not pytest_cache_dir.exists()
