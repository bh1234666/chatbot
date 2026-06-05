"""JSON-backed file registry used as the new file-system source of truth."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Iterable

from .models import FileKind, FileRecord, FileStatus, Visibility
from .path_resolver import PathResolver, normalize_project_path

REGISTRY_FILENAME = ".file_registry.json"


def file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def category_for_path(path: str) -> str:
    ext = Path(path).suffix.lower().lstrip(".")
    if ext in {"py", "pyi", "js", "ts", "tsx", "jsx", "c", "h", "cpp", "hpp", "cc", "rs", "go", "java", "sql", "sh", "bat", "ps1"}:
        return "code"
    if ext in {"md", "txt", "csv", "tsv", "json", "yaml", "yml", "toml", "ini", "html", "htm", "css"}:
        return "text"
    if ext in {"docx", "doc", "pdf", "xlsx", "xls", "pptx", "ppt"}:
        return "office_pdf"
    if ext in {"png", "jpg", "jpeg", "webp", "bmp", "gif"}:
        return "image"
    if ext in {"mp3", "wav", "m4a", "mp4", "mov", "webm", "flac"}:
        return "audio_video"
    if ext in {"zip", "rar", "7z", "tar", "gz"}:
        return "archive"
    return "other"


class FileRegistry:
    def __init__(
        self,
        *,
        scope_id: str,
        workspace_root: str | Path,
        project_root: str | Path | None = None,
        path: str | Path | None = None,
    ) -> None:
        self.scope_id = scope_id
        self.workspace_root = Path(workspace_root).resolve()
        self.project_root = Path(project_root).resolve() if project_root else None
        self.path = Path(path).resolve() if path else self.workspace_root / REGISTRY_FILENAME
        self.resolver = PathResolver(project_root=self.project_root, workspace_root=self.workspace_root)
        self.records: dict[str, FileRecord] = {}

    @classmethod
    def load(
        cls,
        *,
        scope_id: str,
        workspace_root: str | Path,
        project_root: str | Path | None = None,
        path: str | Path | None = None,
    ) -> "FileRegistry":
        registry = cls(scope_id=scope_id, workspace_root=workspace_root, project_root=project_root, path=path)
        if registry.path.is_file():
            try:
                payload = json.loads(registry.path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            for raw in payload.get("records") or []:
                if not isinstance(raw, dict):
                    continue
                record = FileRecord.from_dict(raw)
                if record.file_id:
                    registry.records[record.file_id] = record
        return registry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "scope_id": self.scope_id,
            "project_root": str(self.project_root) if self.project_root else "",
            "workspace_root": str(self.workspace_root),
            "updated_at": time.time(),
            "records": [record.to_dict() for record in sorted(self.records.values(), key=lambda r: r.file_id)],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def add_or_update(self, record: FileRecord) -> FileRecord:
        if not record.file_id:
            record.file_id = self.new_file_id(record.project_path or record.workspace_path or record.display_name)
        record.scope_id = record.scope_id or self.scope_id
        self.records[record.file_id] = record
        return record

    def new_file_id(self, seed: str = "") -> str:
        prefix = hashlib.sha1(f"{self.scope_id}:{seed}".encode("utf-8", errors="ignore")).hexdigest()[:10]
        candidate = f"file_{prefix}"
        if candidate not in self.records:
            return candidate
        return f"file_{prefix}_{uuid.uuid4().hex[:8]}"

    def upsert_project_file(
        self,
        path: Path,
        *,
        kind: FileKind = FileKind.PROJECT_SOURCE,
        status: FileStatus = FileStatus.INDEXED,
        visibility: Visibility = Visibility.PROJECT,
        origin: str = "project_index",
        staged: bool = False,
        metadata: dict | None = None,
    ) -> FileRecord:
        if self.project_root is None:
            raise ValueError("project_root is required for project files")
        abs_path = path.resolve()
        rel = normalize_project_path(abs_path.relative_to(self.project_root).as_posix())
        existing = self.find_by_project_path(rel)
        file_id = existing.file_id if existing else self.new_file_id(rel)
        stat = abs_path.stat()
        record = FileRecord(
            file_id=file_id,
            scope_id=self.scope_id,
            kind=kind,
            status=status,
            visibility=visibility,
            project_path=rel,
            workspace_path=self.resolver.project_to_staged_path(rel) if staged else "",
            display_name=Path(rel).name,
            origin=origin,
            category=category_for_path(rel),
            size=stat.st_size,
            sha256=file_sha256(abs_path),
            metadata=dict(metadata or {}),
        )
        record.metadata["mtime"] = stat.st_mtime
        record.metadata["staged"] = staged
        return self.add_or_update(record)

    def register_deliverable(
        self,
        workspace_path: str,
        *,
        display_name: str = "",
        owner_task_id: str = "",
        metadata: dict | None = None,
    ) -> FileRecord:
        abs_path = self.resolver.safe_workspace_path(workspace_path, must_exist=True)
        rel = abs_path.relative_to(self.workspace_root).as_posix()
        file_id = self.new_file_id(f"deliverable:{rel}")
        stat = abs_path.stat()
        record = FileRecord(
            file_id=file_id,
            scope_id=self.scope_id,
            kind=FileKind.DELIVERABLE,
            status=FileStatus.DELIVERED,
            visibility=Visibility.DELIVERABLE,
            workspace_path=rel,
            display_name=display_name or abs_path.name,
            origin="deliverable_registry",
            owner_task_id=owner_task_id,
            category=category_for_path(rel),
            size=stat.st_size,
            sha256=file_sha256(abs_path),
            verified=True,
            metadata=dict(metadata or {}),
        )
        return self.add_or_update(record)

    def find_by_project_path(self, project_path: str) -> FileRecord | None:
        rel = normalize_project_path(project_path)
        for record in self.records.values():
            if record.project_path == rel:
                return record
        return None

    def find_by_workspace_path(self, workspace_path: str) -> FileRecord | None:
        rel = normalize_project_path(workspace_path)
        for record in self.records.values():
            if normalize_project_path(record.workspace_path) == rel:
                return record
        return None

    def list_records(
        self,
        *,
        kind: FileKind | None = None,
        visibility: Visibility | None = None,
        status: FileStatus | None = None,
    ) -> list[FileRecord]:
        items: Iterable[FileRecord] = self.records.values()
        if kind is not None:
            items = [record for record in items if record.kind == kind]
        if visibility is not None:
            items = [record for record in items if record.visibility == visibility]
        if status is not None:
            items = [record for record in items if record.status == status]
        return sorted(items, key=lambda r: (r.project_path or r.workspace_path or r.display_name, r.file_id))
