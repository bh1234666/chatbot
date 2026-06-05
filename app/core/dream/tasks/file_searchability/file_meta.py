from __future__ import annotations

from typing import Any


def is_active_file_metadata(meta: dict[str, Any]) -> bool:
    """Return true for file records that are still worth indexing."""
    deleted = meta.get("deleted")
    if deleted is True or str(deleted).lower() == "true":
        return False

    status = str(meta.get("download_status") or "").lower()
    if status and status not in {"done", "ok", "complete", "completed"}:
        return False

    return True


async def emit_file_indexed(node: dict[str, Any], task_name: str) -> None:
    """Notify Dream that a file node gained searchable content."""
    try:
        from app.core.dream.event_bus import event_bus
        await event_bus.emit(
            "file_indexed",
            archive_id=node.get("archive_id"),
            group_id=node.get("group_id"),
            file_id=node.get("id"),
            task=task_name,
        )
    except Exception:
        pass
