"""Project and source-material indexing for the unified file registry."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from .models import FileKind, FileRecord, FileStatus, Visibility
from .registry import FileRegistry, category_for_path

DEFAULT_SKIP_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".env_backups",
    "backups",
    "del",
    "logs",
    ".temp",
    ".prev",
    "_helpers_shared",
    "tmp_guard_debug",
    "stress_tools",
    "group_sim",
    "mineru",
    "mingw64",
    "umi-ocr",
    "ominvioce",
    "napcat",
    "claude-code-main",
}

DEFAULT_SKIP_REL_PREFIXES = {
    "data/workspaces",
    "data/inventory_smoke_workspace",
}

DEFAULT_SKIP_FILES = {
    "chatbot.db",
    "chatbot.db-shm",
    "chatbot.db-wal",
    "data/environment_projects.json",
}


def should_skip_rel(path: str, *, skip_dirs: Iterable[str] = DEFAULT_SKIP_DIRS) -> str | None:
    norm = str(path or "").replace("\\", "/").strip("/")
    if not norm:
        return None
    norm_no_slash = norm.rstrip("/")
    name = Path(norm).name
    if name.startswith("~$"):
        return "office_lock_file"
    if norm_no_slash in DEFAULT_SKIP_FILES:
        return "runtime_state"
    if any(norm_no_slash == prefix or norm_no_slash.startswith(prefix + "/") for prefix in DEFAULT_SKIP_REL_PREFIXES):
        return "runtime_state"
    if name.startswith(".") or "/." in norm:
        return "hidden_or_internal"
    parts = set(Path(norm).parts)
    if parts & set(skip_dirs):
        return "generated_or_heavy"
    return None


def index_project(
    project_root: str | Path,
    workspace_root: str | Path,
    *,
    scope_id: str,
    max_entries: int = 10000,
    max_depth: int = 12,
    save: bool = True,
) -> FileRegistry:
    project = Path(project_root).resolve()
    workspace = Path(workspace_root).resolve()
    registry = FileRegistry.load(scope_id=scope_id, workspace_root=workspace, project_root=project)
    scanned = 0
    omitted = 0
    indexed_paths: set[str] = set()
    category_counts: dict[str, int] = {}
    suffix_counts: dict[str, int] = {}

    for current, dirnames, filenames in os.walk(project):
        cur = Path(current)
        rel_dir = "." if cur == project else cur.relative_to(project).as_posix()
        depth = 0 if rel_dir == "." else len(Path(rel_dir).parts)
        kept_dirnames: list[str] = []
        for name in sorted(dirnames):
            rel_child = (cur / name).relative_to(project).as_posix()
            if should_skip_rel(rel_child + "/"):
                omitted += 1
                continue
            kept_dirnames.append(name)
        dirnames[:] = kept_dirnames
        if depth >= max_depth:
            omitted += len(dirnames)
            dirnames[:] = []
        for filename in sorted(filenames):
            path = cur / filename
            rel = path.relative_to(project).as_posix()
            reason = should_skip_rel(rel)
            if reason:
                omitted += 1
                continue
            if scanned >= max_entries:
                omitted += 1
                continue
            try:
                record = registry.upsert_project_file(
                    path,
                    kind=FileKind.PROJECT_SOURCE,
                    status=FileStatus.INDEXED,
                    visibility=Visibility.PROJECT,
                    origin="project_index",
                    staged=(workspace / "_env" / rel).is_file(),
                    metadata={"skip_reason": ""},
                )
            except OSError:
                omitted += 1
                continue
            indexed_paths.add(record.project_path)
            scanned += 1
            category_counts[record.category] = category_counts.get(record.category, 0) + 1
            suffix = Path(rel).suffix.lower() or "(no suffix)"
            suffix_counts[suffix] = suffix_counts.get(suffix, 0) + 1

    stale_index_ids = [
        file_id
        for file_id, record in registry.records.items()
        if (
            file_id != "__index_summary__"
            and record.origin == "project_index"
            and record.kind == FileKind.PROJECT_SOURCE
            and record.project_path not in indexed_paths
        )
    ]
    for file_id in stale_index_ids:
        del registry.records[file_id]

    registry.add_or_update(
        FileRecord(
            file_id="__index_summary__",
            scope_id=scope_id,
            kind=FileKind.SCRATCH,
            status=FileStatus.READY,
            visibility=Visibility.INTERNAL,
            display_name="index_summary",
            origin="project_index",
            metadata={
                "total_indexed": scanned,
                "omitted_entries": omitted,
                "category_counts": category_counts,
                "suffix_counts": dict(sorted(suffix_counts.items(), key=lambda item: (-item[1], item[0]))[:120]),
            },
        )
    )
    if save:
        registry.save()
    return registry


def summarize_registry_for_manifest(registry: FileRegistry) -> dict:
    records = [record for record in registry.list_records() if record.file_id != "__index_summary__"]
    category_counts: dict[str, int] = {}
    suffix_counts: dict[str, int] = {}
    key_candidate_paths: list[str] = []
    for record in records:
        category_counts[record.category] = category_counts.get(record.category, 0) + 1
        suffix = Path(record.project_path or record.workspace_path).suffix.lower() or "(no suffix)"
        suffix_counts[suffix] = suffix_counts.get(suffix, 0) + 1
        lower_name = Path(record.project_path).name.lower()
        lower_rel = record.project_path.lower()
        if (
            lower_name in {
                "readme.md",
                "readme.txt",
                "readme",
                "package.json",
                "pyproject.toml",
                "requirements.txt",
                "makefile",
                "cmakelists.txt",
                "go.mod",
                "cargo.toml",
            }
            or "/test" in f"/{lower_rel}"
            or lower_name.startswith("test_")
            or any(token in lower_rel for token in ("ielts", "vocab", "report"))
            or any(token in record.display_name for token in ("写作", "口语", "听力", "阅读", "词汇", "作业", "报告"))
        ):
            key_candidate_paths.append(record.project_path)
    summary_record = registry.records.get("__index_summary__")
    omitted = 0
    if summary_record:
        omitted = int(summary_record.metadata.get("omitted_entries") or 0)
    return {
        "listed_files": len(records),
        "omitted_entries": omitted,
        "category_counts": category_counts,
        "suffix_counts": dict(sorted(suffix_counts.items(), key=lambda item: (-item[1], item[0]))[:120]),
        "key_candidate_paths": sorted(set(key_candidate_paths))[:240],
    }
