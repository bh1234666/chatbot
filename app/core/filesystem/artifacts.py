"""Deliverable views backed by the unified file registry."""

from __future__ import annotations

from .models import FileKind, Visibility
from .registry import FileRegistry


def list_deliverable_records(registry: FileRegistry) -> list[dict]:
    items = []
    for record in registry.list_records(kind=FileKind.DELIVERABLE, visibility=Visibility.DELIVERABLE):
        if not record.workspace_path:
            continue
        items.append({
            "id": record.file_id,
            "name": record.display_name or record.workspace_path.rsplit("/", 1)[-1],
            "rel_path": record.workspace_path,
            "workspace_path": record.workspace_path,
            "size": int(record.size or 0),
            "status": "ready" if record.verified else str(record.status),
            "kind": "artifact",
            "created_at": int(record.metadata.get("mtime") or 0),
            "category": record.category,
            "sha256": record.sha256,
        })
    return items
