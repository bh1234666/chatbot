"""Per-request event sink used by environment streaming APIs."""
from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

log = logging.getLogger(__name__)


_event_queue_var: ContextVar[asyncio.Queue | None] = ContextVar(
    "environment_event_queue",
    default=None,
)
_event_sink_meta_var: ContextVar[tuple[str, str, str, str] | None] = ContextVar(
    "environment_event_sink_meta",
    default=None,
)
_sink_seq = 0


@dataclass(frozen=True)
class _RegisteredSink:
    sink_id: int
    queue: asyncio.Queue
    archive_id: str = ""
    group_id: str = ""
    user_id: str = ""
    trace_id: str = ""


_registered_sinks: dict[int, _RegisteredSink] = {}


@contextmanager
def environment_event_sink(
    queue: asyncio.Queue | None,
    *,
    archive_id: str = "",
    group_id: str = "",
    user_id: str = "",
    trace_id: str = "",
) -> Iterator[None]:
    """Route environment workflow events to the current SSE request.

    The context variable covers normal in-call tool execution. The explicit
    registry covers helper/background tasks whose asyncio context may no longer
    contain the request-local queue, while still filtering by tenant fields.
    """
    global _sink_seq
    sink_id: int | None = None
    if queue is not None:
        _sink_seq += 1
        sink_id = _sink_seq
        _registered_sinks[sink_id] = _RegisteredSink(
            sink_id=sink_id,
            queue=queue,
            archive_id=archive_id,
            group_id=group_id,
            user_id=user_id,
            trace_id=trace_id,
        )
    token = _event_queue_var.set(queue)
    meta_token = _event_sink_meta_var.set((archive_id, group_id, user_id, trace_id))
    try:
        yield
    finally:
        try:
            _event_sink_meta_var.reset(meta_token)
        except ValueError:
            _event_sink_meta_var.set(None)
            log.warning("environment event sink meta reset crossed context; cleared current context")
        try:
            _event_queue_var.reset(token)
        except ValueError:
            _event_queue_var.set(None)
            log.warning("environment event sink queue reset crossed context; cleared current context")
        if sink_id is not None:
            _registered_sinks.pop(sink_id, None)


def emit_environment_event(event: str, payload: dict) -> None:
    queue = _event_queue_var.get(None)
    if queue is None:
        return
    meta = _event_sink_meta_var.get(None)
    if meta is not None:
        archive_id, group_id, user_id, trace_id = meta
        if not _sink_matches(_RegisteredSink(0, queue, archive_id, group_id, user_id, trace_id), payload):
            return
    _put_event(queue, event, payload)


def publish_environment_event(event: str, payload: dict) -> None:
    """Publish a workflow event to the inline stream and the global monitor.

    The function is intentionally sync so tool/helper code can call it without
    awaiting or changing existing control flow.
    """
    seen: set[int] = set()
    queue = _event_queue_var.get(None)
    if queue is not None:
        meta = _event_sink_meta_var.get(None)
        if meta is None:
            _put_event(queue, event, payload)
            seen.add(id(queue))
        else:
            archive_id, group_id, user_id, trace_id = meta
            if _sink_matches(_RegisteredSink(0, queue, archive_id, group_id, user_id, trace_id), payload):
                _put_event(queue, event, payload)
                seen.add(id(queue))
    for sink in list(_registered_sinks.values()):
        if id(sink.queue) in seen:
            continue
        if not _sink_matches(sink, payload):
            continue
        _put_event(sink.queue, event, payload)
        seen.add(id(sink.queue))
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        from app.core.bg_tasks import schedule
        from app.core.environment_monitor import monitor as env_monitor

        schedule(env_monitor.publish(event, payload), name=f"environment_event:{event}")
    except Exception:
        pass


def publish_workflow_event(payload: dict) -> None:
    try:
        from app.core.intermediate_feedback import publish_feedback_workflow_event

        publish_feedback_workflow_event(payload)
    except Exception:
        pass
    publish_environment_event("workflow", payload)


def _put_event(queue: asyncio.Queue, event: str, payload: dict) -> None:
    try:
        queue.put_nowait((event, payload))
    except Exception:
        pass


def _sink_matches(sink: _RegisteredSink, payload: dict) -> bool:
    if sink.archive_id and payload.get("archive_id") and payload.get("archive_id") != sink.archive_id:
        return False
    if sink.group_id and payload.get("group_id") and payload.get("group_id") != sink.group_id:
        return False
    if sink.user_id and payload.get("user_id") and payload.get("user_id") != sink.user_id:
        return False
    if sink.trace_id and payload.get("trace_id") != sink.trace_id:
        return False
    return True
