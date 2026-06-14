"""Registry for files that the bot has actually delivered to the user."""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from app.core.filesystem import FileRegistry, promote_deliverable
from app.core.filesystem.artifacts import list_deliverable_records
from app.db.pool import pool
from app.llm.tools import workspace as ws_tool


def _clean_rel_path(value: str) -> str:
    rel = str(value or "").split("?", 1)[0].split("#", 1)[0].replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p and p not in (".", "..")]
    return "/".join(parts)


def _artifact_id(rel_path: str, name: str) -> str:
    key = rel_path or name or str(time.time())
    digest = hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:24]
    return f"artifact_{digest}"


def _workspace_file(archive_id: str, group_id: str, rel_path: str) -> Path | None:
    if not rel_path:
        return None
    base = Path(ws_tool.get_persistent_workspace_path(archive_id, group_id)).resolve()
    try:
        resolved = Path(ws_tool._safe_resolve(str(base), rel_path)).resolve()
        resolved.relative_to(base)
    except Exception:
        return None
    if not resolved.is_file() or resolved.is_symlink():
        return None
    return resolved


def _artifact_registry(archive_id: str, group_id: str) -> FileRegistry | None:
    try:
        workspace = Path(ws_tool.get_persistent_workspace_path(archive_id, group_id)).resolve()
        return FileRegistry.load(scope_id=f"artifacts:{archive_id}:{group_id}", workspace_root=workspace)
    except Exception:
        return None


def _normalize_done_file(archive_id: str, group_id: str, item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    rel_path = _clean_rel_path(
        item.get("rel_path")
        or item.get("workspace_path")
        or item.get("path")
        or ""
    )
    name = os.path.basename(str(item.get("name") or rel_path or "artifact"))
    if not rel_path:
        url = str(item.get("url") or item.get("download_url") or "")
        marker = f"/v1/chat/files/{archive_id}/{group_id}/"
        if marker in url:
            rel_path = _clean_rel_path(url.split(marker, 1)[1])
    if not rel_path:
        return None

    local = _workspace_file(archive_id, group_id, rel_path)
    size = 0
    created_at = int(time.time())
    if local is not None:
        stat = local.stat()
        size = int(stat.st_size)
        created_at = int(stat.st_mtime)
    elif item.get("size") is not None:
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0

    return {
        "id": _artifact_id(rel_path, name),
        "name": name,
        "rel_path": rel_path,
        "workspace_path": rel_path,
        "size": size,
        "created_at": created_at,
    }


async def record_delivered_files(
    archive_id: str,
    group_id: str,
    files: list[dict] | tuple[dict, ...] | None,
) -> int:
    """Persist only files that appeared in done.files."""
    if not files:
        return 0
    normalized = [
        item for item in (
            _normalize_done_file(archive_id, group_id, raw) for raw in files
        )
        if item is not None
    ]
    if not normalized:
        return 0

    now = int(time.time())
    registry = _artifact_registry(archive_id, group_id)
    async with pool().acquire() as conn:
        for item in normalized:
            if registry is not None:
                try:
                    record = promote_deliverable(
                        registry,
                        item["workspace_path"],
                        display_name=item["name"],
                        metadata={"source": "done.files", "delivered_at": item["created_at"] or now},
                    )
                    item["id"] = record.file_id
                except Exception:
                    pass
            await conn.execute(
                """
                INSERT INTO bot_delivered_artifacts
                    (archive_id, group_id, artifact_id, file_name, file_size,
                     delivered_at, workspace_path)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (archive_id, group_id, artifact_id) DO UPDATE
                    SET file_name = EXCLUDED.file_name,
                        file_size = EXCLUDED.file_size,
                        delivered_at = EXCLUDED.delivered_at,
                        workspace_path = EXCLUDED.workspace_path
                """,
                archive_id,
                group_id,
                item["id"],
                item["name"],
                item["size"],
                item["created_at"] or now,
                item["workspace_path"],
            )
    return len(normalized)


async def list_delivered_files(archive_id: str, group_id: str) -> list[dict]:
    registry_items: list[dict] = []
    registry = _artifact_registry(archive_id, group_id)
    if registry is not None:
        try:
            registry_items = list_deliverable_records(registry)
        except Exception:
            registry_items = []
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT artifact_id, file_name, file_size, delivered_at, workspace_path
            FROM bot_delivered_artifacts
            WHERE archive_id = $1 AND group_id = $2
            ORDER BY delivered_at DESC
            """,
            archive_id,
            group_id,
        )
    items_by_path: dict[str, dict] = {}
    for item in registry_items:
        rel_path = _clean_rel_path(item.get("workspace_path") or item.get("rel_path") or "")
        if not rel_path:
            continue
        item = {
            **item,
            "rel_path": rel_path,
            "workspace_path": rel_path,
            "download_url": f"/v1/chat/files/{archive_id}/{group_id}/{rel_path}",
            "preview_url": f"/v1/chat/file-preview/{archive_id}/{group_id}/{rel_path}",
        }
        items_by_path[rel_path] = item
    for row in rows:
        rel_path = _clean_rel_path(row["workspace_path"])
        if not rel_path:
            continue
        items_by_path.setdefault(rel_path, {
            "id": row["artifact_id"],
            "name": row["file_name"] or os.path.basename(rel_path),
            "rel_path": rel_path,
            "workspace_path": rel_path,
            "size": int(row["file_size"] or 0),
            "status": "ready",
            "kind": "artifact",
            "created_at": int(row["delivered_at"] or 0),
            "download_url": f"/v1/chat/files/{archive_id}/{group_id}/{rel_path}",
            "preview_url": f"/v1/chat/file-preview/{archive_id}/{group_id}/{rel_path}",
        })
    items = sorted(items_by_path.values(), key=lambda item: int(item.get("created_at") or 0), reverse=True)
    return items
