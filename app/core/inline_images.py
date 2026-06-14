from __future__ import annotations

import os
import re
import time
from datetime import datetime

from app.core.source_attribution import current_user_source_match

_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
_RECENT_INLINE_IMAGE_SEC = 120.0
_RECENT_INLINE_IMAGE_CAP = 10
_LOCAL_IMAGE_RE = re.compile(r"\[本地image:\s*([^\]\s]+)", re.IGNORECASE)


def _image_basename(name: str) -> str:
    return str(name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]


def scan_inline_images(archive_id: str, group_id: str) -> list[dict]:
    if not archive_id or not group_id:
        return []
    try:
        from app.llm.tools import workspace as ws_tool

        main_ws = ws_tool.create_workspace(archive_id, group_id)
        media_dir = os.path.join(main_ws, "_downloaded_media")
        if not os.path.isdir(media_dir):
            return []

        now = time.time()
        out: list[dict] = []
        for entry in os.listdir(media_dir):
            if not entry.lower().endswith(_IMAGE_EXTENSIONS):
                continue
            path = os.path.join(media_dir, entry)
            try:
                st = os.stat(path)
            except OSError:
                continue
            out.append({
                "name": entry,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "mtime_str": datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M:%S"),
            })
        out.sort(key=lambda item: (-item["mtime"], item["name"].lower()))
        recent_count = 0
        for item in out:
            is_recent = (now - item["mtime"]) < _RECENT_INLINE_IMAGE_SEC
            if is_recent and recent_count < _RECENT_INLINE_IMAGE_CAP:
                item["is_session"] = True
                recent_count += 1
            else:
                item["is_session"] = False
        return out
    except Exception:
        return []


def annotate_inline_images(
    inline_images: list[dict] | None,
    recent_group_messages: list[dict] | None,
    *,
    current_user_id: str = "",
    current_user_name: str = "",
) -> list[dict]:
    """Attach uploader facts to scanned visual inputs using recent message refs.

    Disk files in `_downloaded_media` are group-shared. Recent group messages
    are the nearest reliable source for who sent a local image marker, so this
    function keeps the shared image list while separating same-user implicit
    source candidates from other participants' shared context.
    """
    images = [dict(item) for item in (inline_images or []) if isinstance(item, dict)]
    if not images:
        return []

    owner_by_name: dict[str, dict] = {}
    for msg in recent_group_messages or []:
        if not isinstance(msg, dict):
            continue
        content = str(msg.get("content") or msg.get("text") or "")
        if not content:
            continue
        for match in _LOCAL_IMAGE_RE.finditer(content):
            name = _image_basename(match.group(1))
            if not name:
                continue
            owner_by_name[name] = {
                "uploader_user_id": str(msg.get("user_id") or ""),
                "uploader_name": str(
                    msg.get("user_name") or msg.get("sender_name") or msg.get("sender") or ""
                ),
                "source_message_id": msg.get("id"),
                "source_created_at": msg.get("created_at"),
            }

    current_user_id = str(current_user_id or "")
    current_user_name = str(current_user_name or "")
    for item in images:
        name = _image_basename(str(item.get("name") or ""))
        owner = owner_by_name.get(name)
        if owner:
            item.update(owner)
            owner_id = str(owner.get("uploader_user_id") or "")
            owner_name = str(owner.get("uploader_name") or "")
            item["current_user_match"] = current_user_source_match(
                current_user_id=current_user_id,
                current_user_name=current_user_name,
                uploader_id=owner_id,
                uploader_name=owner_name,
            )
        else:
            item.setdefault("current_user_match", None)
    return images
