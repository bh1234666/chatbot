"""Create a restorable local project snapshot as a zip archive.

This snapshot is intended for local rollback/analysis. It keeps source and
project configuration while excluding transient caches, local databases,
generated logs, and previous backups that make snapshots fragile or huge.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = ROOT / "backups"
WINDOWS_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

EXCLUDE_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".benchmarks",
    "node_modules",
    "dist",
    "build",
    "backups",
    "logs",
    "output",
    "tmp",
    "runs",
}

EXCLUDE_PATH_PREFIXES = (
    ".temp/",
    ".tmp_bg_manual/",
    ".tmp_bg_manual2/",
    "api_bg_",
    "bg_ws_",
    "tmp_bg_",
    "stress_tools/runs/",
    "agent_frontend/dist/",
    "agent_frontend/node_modules/",
)

EXCLUDE_FILE_GLOBS = (
    "*.pyc",
    "*.pyo",
    "*.db",
    "*.db-shm",
    "*.db-wal",
    "*.sqlite",
    "*.sqlite3",
    "*.log",
    "*.zip",
    "*.tar",
    "*.tar.gz",
    "*.7z",
    "*.mp3",
    "*.mp4",
    "*.wav",
    ".env",
    ".env.local",
    ".env.*.local",
)


def _is_windows_device_name(path: Path) -> bool:
    stem = path.name.split(".", 1)[0].lower()
    return stem in WINDOWS_DEVICE_NAMES


def _is_excluded_file(path: Path) -> bool:
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in EXCLUDE_FILE_GLOBS)


def should_include(rel_path: Path) -> bool:
    rel_str = rel_path.as_posix()
    parts = rel_path.parts
    if any(part in EXCLUDE_DIR_NAMES for part in parts[:-1]):
        return False
    if any(rel_str.startswith(prefix) for prefix in EXCLUDE_PATH_PREFIXES):
        return False
    if _is_windows_device_name(rel_path):
        return False
    if _is_excluded_file(rel_path):
        return False
    return True


def iter_snapshot_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=lambda _err: None):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = [
            name
            for name in dirnames
            if should_include((rel_dir / name) if rel_dir != Path(".") else Path(name))
        ]
        for filename in filenames:
            rel_path = (rel_dir / filename) if rel_dir != Path(".") else Path(filename)
            if should_include(rel_path):
                files.append(root / rel_path)
    files.sort(key=lambda item: item.relative_to(root).as_posix())
    return files


def create_snapshot(root: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = out_dir / f"chatbot_snapshot_{stamp}.zip"
    temp_archive_path = archive_path.with_suffix(".zip.tmp")
    files = iter_snapshot_files(root)
    manifest = {
        "root": str(root),
        "file_count": len(files),
        "files": [path.relative_to(root).as_posix() for path in files],
    }
    if temp_archive_path.exists():
        temp_archive_path.unlink()
    try:
        with ZipFile(temp_archive_path, "w", compression=ZIP_DEFLATED) as zf:
            for path in files:
                rel = path.relative_to(root)
                zf.write(path, arcname=rel.as_posix())
            zf.writestr("SNAPSHOT_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        temp_archive_path.replace(archive_path)
    finally:
        if temp_archive_path.exists():
            temp_archive_path.unlink()
    return archive_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a local zip snapshot for the current project.")
    parser.add_argument("--check", action="store_true", help="Validate script wiring without creating a snapshot.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)

    if args.check:
        print("create_local_snapshot.py OK")
        return 0

    archive_path = create_snapshot(ROOT, args.out_dir)
    print(archive_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
