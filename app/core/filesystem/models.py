"""Data models for the unified file registry."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class FileKind(StrEnum):
    PROJECT_SOURCE = "project_source"
    USER_UPLOAD = "user_upload"
    STAGED_INPUT = "staged_input"
    HELPER_INPUT = "helper_input"
    HELPER_OUTPUT = "helper_output"
    EVIDENCE = "evidence"
    DELIVERABLE = "deliverable"
    SCRATCH = "scratch"
    BACKUP = "backup"


class FileStatus(StrEnum):
    INDEXED = "indexed"
    PLANNED = "planned"
    STAGED = "staged"
    AVAILABLE = "available"
    READING = "reading"
    READ = "read"
    PARTIAL = "partial"
    WRITING = "writing"
    READY = "ready"
    FAILED = "failed"
    VERIFIED = "verified"
    PROMOTED = "promoted"
    APPLIED = "applied"
    DELIVERED = "delivered"


class Visibility(StrEnum):
    INTERNAL = "internal"
    EVIDENCE = "evidence"
    PROJECT = "project"
    DELIVERABLE = "deliverable"


@dataclass(slots=True)
class FileRecord:
    file_id: str
    scope_id: str
    kind: FileKind
    status: FileStatus
    visibility: Visibility
    project_path: str = ""
    workspace_path: str = ""
    helper_path: str = ""
    display_name: str = ""
    origin: str = ""
    owner_task_id: str = ""
    helper_kind: str = ""
    category: str = "other"
    size: int | None = None
    sha256: str = ""
    declared: bool = False
    expected: bool = False
    verified: bool = False
    read_state: str = ""
    write_state: str = ""
    apply_state: str = ""
    errors: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = str(self.kind)
        data["status"] = str(self.status)
        data["visibility"] = str(self.visibility)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileRecord":
        return cls(
            file_id=str(data.get("file_id") or ""),
            scope_id=str(data.get("scope_id") or ""),
            kind=FileKind(str(data.get("kind") or FileKind.SCRATCH)),
            status=FileStatus(str(data.get("status") or FileStatus.INDEXED)),
            visibility=Visibility(str(data.get("visibility") or Visibility.INTERNAL)),
            project_path=str(data.get("project_path") or ""),
            workspace_path=str(data.get("workspace_path") or ""),
            helper_path=str(data.get("helper_path") or ""),
            display_name=str(data.get("display_name") or ""),
            origin=str(data.get("origin") or ""),
            owner_task_id=str(data.get("owner_task_id") or ""),
            helper_kind=str(data.get("helper_kind") or ""),
            category=str(data.get("category") or "other"),
            size=data.get("size") if isinstance(data.get("size"), int) else None,
            sha256=str(data.get("sha256") or ""),
            declared=bool(data.get("declared")),
            expected=bool(data.get("expected")),
            verified=bool(data.get("verified")),
            read_state=str(data.get("read_state") or ""),
            write_state=str(data.get("write_state") or ""),
            apply_state=str(data.get("apply_state") or ""),
            errors=list(data.get("errors") or []),
            metadata=dict(data.get("metadata") or {}),
        )
