"""Cross-platform cleanup for common generated Python project artifacts."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIR_NAMES_TO_REMOVE = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}


def find_cleanup_targets(root: Path) -> list[Path]:
    targets: set[Path] = set()
    for dirpath, dirnames, _filenames in os.walk(root, topdown=True, onerror=lambda _err: None):
        current = Path(dirpath)
        if current.name in DIR_NAMES_TO_REMOVE:
            targets.add(current)
            dirnames[:] = []
            continue
        dirnames[:] = [name for name in dirnames if name not in {".git", ".venv"}]
    return sorted(targets)


def remove_targets(targets: list[Path]) -> int:
    removed = 0
    for path in targets:
        shutil.rmtree(path, ignore_errors=False)
        removed += 1
    return removed


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    if "--check" in argv:
        print("clean_generated.py OK")
        return 0
    targets = find_cleanup_targets(ROOT)
    removed = remove_targets(targets)
    print(f"removed {removed} directories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
