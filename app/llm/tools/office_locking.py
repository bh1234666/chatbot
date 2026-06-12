"""Shared Office file-write coordination helpers."""
from __future__ import annotations

import asyncio
import os
import time

_DOCX_WRITE_LOCKS: dict[str, asyncio.Lock] = {}


def docx_write_lock(target: str) -> asyncio.Lock:
    key = os.path.abspath(target)
    lock = _DOCX_WRITE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _DOCX_WRITE_LOCKS[key] = lock
    return lock


def replace_file_with_retries(tmp_path: str, target: str, *, attempts: int = 6) -> str | None:
    """Replace target, retrying short Windows file-lock races."""
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            os.replace(tmp_path, target)
            return None
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.15 * (attempt + 1))
    return (
        "PermissionError while replacing target after retries: "
        f"{last_error}. The target file is likely still locked by another read/write operation."
    )
