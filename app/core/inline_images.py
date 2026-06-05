from __future__ import annotations

import os
import time
from datetime import datetime

_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
_RECENT_INLINE_IMAGE_SEC = 120.0
_RECENT_INLINE_IMAGE_CAP = 10


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
