"""Shared per-user interrupt message queue for chat-compatible APIs."""
from __future__ import annotations

import time
from collections import deque

from app.schemas.api import InterruptMessageRequest


_interrupt_messages: dict[tuple[str, str, str], deque[dict]] = {}


def push_interrupt_message(req: InterruptMessageRequest, *, maxlen: int = 10) -> None:
    key = (req.archive_id, req.group_id, req.user_id)
    queue = _interrupt_messages.setdefault(key, deque(maxlen=maxlen))
    if req.client_msg_id and any(item.get("client_msg_id") == req.client_msg_id for item in queue):
        return
    queue.append({
        "message": req.message,
        "client_msg_id": req.client_msg_id or "",
        "ts": time.monotonic(),
    })


def pop_interrupt_messages(archive_id: str, group_id: str, user_id: str) -> list[str]:
    queue = _interrupt_messages.pop((archive_id, group_id, user_id), None)
    if not queue:
        return []
    out: list[str] = []
    seen_ids: set[str] = set()
    now = time.monotonic()
    while queue:
        item = queue.popleft()
        if now - float(item.get("ts") or 0) > 120.0:
            continue
        client_msg_id = str(item.get("client_msg_id") or "")
        if client_msg_id:
            if client_msg_id in seen_ids:
                continue
            seen_ids.add(client_msg_id)
        msg = str(item.get("message") or "").strip()
        if msg:
            out.append(msg)
    return out


def clear_interrupt_messages() -> None:
    _interrupt_messages.clear()


def interrupt_messages_raw() -> dict[tuple[str, str, str], deque[dict]]:
    return _interrupt_messages
