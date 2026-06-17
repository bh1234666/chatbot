"""Environment-mode API for local project agent clients."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from app.api.interrupts import interrupt_messages_raw, pop_interrupt_messages, pop_interrupt_payloads, push_interrupt_message
from app.core.locks import get_group_guard
from app.core.environment_monitor import monitor as env_monitor
from app.core.environment_projects import resolve_environment_project
from app.schemas.api import ChatRequest, EnvironmentChatRequest, InterruptMessageRequest


log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/environment", tags=["environment"])
SSE_RESPONSE_HEADERS = {"Content-Type": "text/event-stream; charset=utf-8"}

_interrupt_messages = interrupt_messages_raw()


def _push_interrupt_message(req: InterruptMessageRequest) -> None:
    push_interrupt_message(req, maxlen=10)


def _pop_interrupt_messages(archive_id: str, group_id: str, user_id: str) -> list[str]:
    return pop_interrupt_messages(archive_id, group_id, user_id)


def _pop_interrupt_payloads(archive_id: str, group_id: str, user_id: str) -> list[dict]:
    return pop_interrupt_payloads(archive_id, group_id, user_id)


def _sse(event_name: str, payload: dict) -> dict:
    return {"event": event_name, "data": json.dumps(payload, ensure_ascii=False)}


def _monitor_match(payload: dict, archive_id: str, group_id: str, user_id: str, trace_id: str = "") -> bool:
    if archive_id and payload.get("archive_id") and payload.get("archive_id") != archive_id:
        return False
    if group_id and payload.get("group_id") and payload.get("group_id") != group_id:
        return False
    if user_id and payload.get("user_id") and payload.get("user_id") != user_id:
        return False
    if trace_id and payload.get("trace_id") != trace_id:
        return False
    return True


@router.post("/stream")
async def environment_stream(req: EnvironmentChatRequest):
    from app.api.chat import chat_stream

    if not req.current_dir.strip():
        raise HTTPException(status_code=422, detail="current_dir is required for environment stream")
    return await chat_stream(ChatRequest(
        user_id=req.user_id,
        user_name=req.user_name,
        message=req.message,
        current_dir=req.current_dir,
        project_id=req.project_id,
        persona_id=req.persona_id,
        client_msg_id=req.client_msg_id,
        attached_file_ids=req.attached_file_ids,
    ))


@router.post("/interrupt_message")
async def interrupt_message(req: InterruptMessageRequest) -> dict:
    archive_id = req.archive_id
    group_id = req.group_id
    user_id = req.user_id
    current_dir = str(req.current_dir or "").strip()
    project_id = str(req.project_id or "").strip()
    if current_dir:
        if not user_id:
            raise HTTPException(status_code=422, detail="missing required fields: user_id")
        try:
            env_project = await resolve_environment_project(
                user_id=str(user_id),
                current_dir=current_dir,
                project_id=project_id,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        archive_id = env_project["archive_id"]
        group_id = env_project["group_id"]
    active = await get_group_guard().is_busy(archive_id, group_id, user_id)
    if active:
        queue_req = req.model_copy(update={"archive_id": archive_id, "group_id": group_id, "user_id": user_id})
        _push_interrupt_message(queue_req)
        guard = get_group_guard()
        stage = guard.get_stage(archive_id, group_id, user_id) if hasattr(guard, "get_stage") else ""
        signaled = await guard.signal_abort(
            archive_id=archive_id,
            group_id=group_id,
            user_id=user_id,
        )
        return {
            "ok": active,
            "queued": True,
            "aborted": signaled,
            "stage": stage,
            "reason": "" if signaled else ("queued_no_preempt" if stage in {"round3", "round2_5"} else "not_preempted"),
        }
    return {"ok": active}


@router.get("/active")
async def list_active() -> dict:
    holders = await get_group_guard().active_holders()
    return {
        "items": [
            {"archive_id": k[0], "group_id": k[1], "user_id": k[2], "trace_id": v}
            for k, v in holders.items()
            if str(k[1]).startswith("env_user")
        ]
    }


@router.get("/monitor")
async def monitor_environment(
    archive_id: str = "",
    group_id: str = "",
    user_id: str = "",
    trace_id: str = "",
    heartbeat_sec: float = 10.0,
):
    heartbeat_sec = max(1.0, min(float(heartbeat_sec or 10.0), 60.0))

    async def event_gen():
        queue = await env_monitor.subscribe()
        try:
            yield _sse("snapshot", await env_monitor.snapshot(
                archive_id=archive_id,
                group_id=group_id,
                user_id=user_id,
                trace_id=trace_id,
            ))
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=heartbeat_sec)
                except asyncio.TimeoutError:
                    yield _sse("heartbeat", await env_monitor.snapshot(
                        archive_id=archive_id,
                        group_id=group_id,
                        user_id=user_id,
                        trace_id=trace_id,
                    ))
                    continue
                payload = item.get("payload") or {}
                if not _monitor_match(payload, archive_id, group_id, user_id, trace_id):
                    continue
                yield _sse(str(item.get("event") or "workflow"), payload)
        except asyncio.CancelledError:
            raise
        finally:
            await env_monitor.unsubscribe(queue)

    return EventSourceResponse(event_gen(), headers=SSE_RESPONSE_HEADERS)


@router.get("/commands/active")
async def active_environment_commands(
    archive_id: str = "",
    group_id: str = "",
    user_id: str = "",
    trace_id: str = "",
) -> dict:
    return await env_monitor.snapshot(
        archive_id=archive_id,
        group_id=group_id,
        user_id=user_id,
        trace_id=trace_id,
    )


@router.get("/monitor/history")
async def environment_monitor_history(
    archive_id: str = "",
    group_id: str = "",
    user_id: str = "",
    trace_id: str = "",
    limit: int = 200,
) -> dict:
    return {
        "items": await env_monitor.history(
            archive_id=archive_id,
            group_id=group_id,
            user_id=user_id,
            trace_id=trace_id,
            limit=limit,
        )
    }


@router.post("/commands/{command_id}/abort")
async def abort_environment_command(command_id: str) -> dict:
    return await env_monitor.abort_command(command_id)


@router.post("/abort")
async def abort_environment(body: dict) -> dict:
    user_id = str(body.get("user_id") or "")
    archive_id = str(body.get("archive_id") or "")
    group_id = str(body.get("group_id") or "")
    current_dir = str(body.get("current_dir") or "").strip()
    project_id = str(body.get("project_id") or "").strip()
    if current_dir:
        if not user_id:
            raise HTTPException(status_code=422, detail="missing required fields: user_id")
        try:
            env_project = await resolve_environment_project(
                user_id=user_id,
                current_dir=current_dir,
                project_id=project_id,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        archive_id = env_project["archive_id"]
        group_id = env_project["group_id"]

    missing = [
        k for k, v in
        (("archive_id", archive_id), ("group_id", group_id), ("user_id", user_id))
        if not v
    ]
    if missing:
        raise HTTPException(status_code=422, detail=f"missing required fields: {', '.join(missing)}")
    ok = await get_group_guard().signal_abort(
        archive_id=archive_id,
        group_id=group_id,
        user_id=user_id,
    )
    return {"ok": ok}
