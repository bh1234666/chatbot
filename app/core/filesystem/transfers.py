"""File transfer primitives for the replacement file-management pipeline."""

from __future__ import annotations

import shutil
from pathlib import Path

from .models import FileKind, FileRecord, FileStatus, Visibility
from .registry import FileRegistry, category_for_path, file_sha256


def stage_project_file(registry: FileRegistry, project_path: str) -> FileRecord:
    """Copy one project file into the workspace staged area and register it."""
    if registry.project_root is None:
        raise ValueError("project_root is required for staging")
    source = registry.resolver.safe_project_path(project_path, must_exist=True)
    if not source.is_file():
        raise FileNotFoundError(project_path)
    rel = source.relative_to(registry.project_root).as_posix()
    staged_rel = registry.resolver.project_to_staged_path(rel)
    target = registry.resolver.safe_workspace_path(staged_rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    record = registry.upsert_project_file(
        source,
        kind=FileKind.STAGED_INPUT,
        status=FileStatus.STAGED,
        visibility=Visibility.PROJECT,
        origin="stage_project_file",
        staged=True,
        metadata={"staged_path": staged_rel},
    )
    record.workspace_path = staged_rel
    record.status = FileStatus.STAGED
    record.kind = FileKind.STAGED_INPUT
    registry.add_or_update(record)
    registry.save()
    return record


def intake_workspace_file(
    registry: FileRegistry,
    workspace_path: str,
    *,
    kind: FileKind,
    status: FileStatus = FileStatus.READY,
    visibility: Visibility = Visibility.INTERNAL,
    owner_task_id: str = "",
    helper_kind: str = "",
    display_name: str = "",
    metadata: dict | None = None,
) -> FileRecord:
    """Register an existing workspace file without guessing its downstream role."""
    target = registry.resolver.safe_workspace_path(workspace_path, must_exist=True)
    if not target.is_file():
        raise FileNotFoundError(workspace_path)
    rel = target.relative_to(registry.workspace_root).as_posix()
    stat = target.stat()
    record = FileRecord(
        file_id=registry.new_file_id(f"{kind}:{rel}:{owner_task_id}"),
        scope_id=registry.scope_id,
        kind=kind,
        status=status,
        visibility=visibility,
        workspace_path=rel,
        display_name=display_name or target.name,
        origin="workspace_intake",
        owner_task_id=owner_task_id,
        helper_kind=helper_kind,
        category=category_for_path(rel),
        size=stat.st_size,
        sha256=file_sha256(target),
        metadata=dict(metadata or {}),
    )
    registry.add_or_update(record)
    registry.save()
    return record


def promote_deliverable(
    registry: FileRegistry,
    workspace_path: str,
    *,
    display_name: str = "",
    owner_task_id: str = "",
    metadata: dict | None = None,
) -> FileRecord:
    record = intake_workspace_file(
        registry,
        workspace_path,
        kind=FileKind.DELIVERABLE,
        status=FileStatus.DELIVERED,
        visibility=Visibility.DELIVERABLE,
        owner_task_id=owner_task_id,
        display_name=display_name,
        metadata=metadata,
    )
    record.verified = True
    registry.add_or_update(record)
    registry.save()
    return record
