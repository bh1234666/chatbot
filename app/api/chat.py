"""
Chat API: SSE streaming output with per-user mutual exclusion.

Lock semantics:
- The same (archive_id, group_id, user_id) may have only one active chat flow.
- Different users in the same group do not block each other.
- If a new request arrives while the same user is active, return 409 Conflict immediately.
- The bridge can use that response to decide whether to send an interrupt.
- The lock covers the full lifecycle: orchestration plus post-response maintenance.
- Clients should wait for the `complete` SSE event before sending the next message for that user.

SSE events:
  event: meta       data: {"trace_id": "..."}
  event: progress   data: {"round": "loading|analyzing|planning|responding|maintaining"}
  event: progress   data: {"round": "planning", "tool_call": "python", "tool_call_count": 2}
  event: token      data: {"text": "..."}
  event: done       data: {"tendencies": {...}, "trace_id": "..."}
  event: complete   data: {"trace_id": "..."}
  event: error      data: {"code": "...", "message": "..."}

409 response shape:
  {"detail": {"code": "group_busy", "message": "...", "active_trace_id": "..."}}
"""
import asyncio
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

from app.api.interrupts import interrupt_messages_raw, pop_interrupt_messages, pop_interrupt_payloads, push_interrupt_message
from app.schemas.api import (
    AutoContinueCheckRequest,
    AutoContinueCheckResponse,
    ChatRequest,
    InterruptMessageRequest,
)
from app.memory import archive as archive_dao
from app.memory import bot_config
from app.core.orchestrator import orchestrate
from app.core.locks import get_group_guard, GroupBusyError
from app.core import debug
from app.core.environment_events import environment_event_sink
from app.core.environment_monitor import monitor as env_monitor
from app.core.environment_projects import resolve_environment_project
from app.core.runtime_mode import EnvironmentContext, runtime_context
from app.core.file_policy import MAX_DOWNLOAD_BYTES, classify_file_for_delivery
from app.core.file_preview import preview_file
from app.config import settings
from app.llm.tools import workspace as ws_tool
from app.memory import persona_files
from app.memory import bot_artifacts
from app.memory import bot_files as bot_file_store


log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/chat", tags=["chat"])
SSE_RESPONSE_HEADERS = {"Content-Type": "text/event-stream; charset=utf-8"}


# client_msg_id idempotency:
# Network retries from NapCat or other bridges can resend the same message and
# otherwise run duplicate orchestration, write duplicate hot memories, and fire
# duplicate abort signals. Keep a short in-process LRU keyed by
# (archive_id, group_id, user_id, client_msg_id).
#
# Cache entry shape:
#   (trace_id, ts, done_payload | None, complete_payload | None, token_text)
# Replay emits meta, cached token text when it remains user-visible, cached done
# when available, and complete.
_IDEMPOTENCY_TTL = settings.idempotency_ttl_sec
_IDEMPOTENCY_MAX = settings.idempotency_max_entries
_idempotency_cache: "OrderedDict[tuple, tuple[str, float, dict | None, dict | None, str]]" = OrderedDict()
_interrupt_messages = interrupt_messages_raw()


def _truncate_for_auto_continue(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[-limit:]


def _auto_continue_user_payload(req: AutoContinueCheckRequest) -> str:
    """Return stable, compact model-visible JSON for auto-continue checks.

    高频小调用使用固定字段顺序和紧凑 JSON，降低前缀抖动。
    """
    payload = {
        "assistant_reply": _truncate_for_auto_continue(req.assistant_reply, 12000),
        "auto_continue_elapsed_sec": round(float(req.auto_continue_elapsed_sec or 0.0), 3),
        "max_auto_continue_sec": round(float(req.max_auto_continue_sec or 0.0), 3),
        "recent_context": _truncate_for_auto_continue(req.recent_context, 6000),
        "user_message": _truncate_for_auto_continue(req.user_message, 8000),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _normalize_auto_continue(raw: dict, req: AutoContinueCheckRequest) -> AutoContinueCheckResponse:
    if req.max_auto_continue_sec <= 0 or req.auto_continue_elapsed_sec >= req.max_auto_continue_sec:
        return AutoContinueCheckResponse(
            should_continue=False,
            confidence=0.0,
            reason="auto_continue_time_limit_reached",
            continue_message="继续",
        )

    should_continue = bool(raw.get("should_continue"))
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    reason = str(raw.get("reason") or "").strip()
    if len(reason) > 300:
        reason = reason[:300]

    continue_message = str(raw.get("continue_message") or "继续").strip() or "继续"
    if should_continue and continue_message in {"继续", "缁х画"}:
        anchor = _truncate_for_auto_continue(req.user_message, 80)
        continue_message = f"继续完成同一任务：{anchor}" if anchor else "继续"
    if len(continue_message) > 80:
        continue_message = continue_message[:80].strip() or "继续"

    # Keep this endpoint as an LLM judge plus hard transport/time boundaries.
    # Do not override the judge with phrase markers such as "next step" or
    # "continue"; those symbolic rules drift across tasks and languages.
    if confidence < 0.55:
        should_continue = False

    return AutoContinueCheckResponse(
        should_continue=should_continue,
        confidence=confidence,
        reason=reason,
        continue_message=continue_message,
    )


def _record_delivered_files_background(
    archive_id: str,
    group_id: str,
    files: list[dict],
) -> None:
    task = asyncio.create_task(
        bot_artifacts.record_delivered_files(archive_id, group_id, files)
    )

    def _log_failure(done: asyncio.Task) -> None:
        try:
            done.result()
        except Exception as exc:
            log.warning("record delivered artifacts failed: %s", exc)
            debug.warn(f"record delivered artifacts failed: {type(exc).__name__}: {exc}")

    task.add_done_callback(_log_failure)


def _interrupt_key(archive_id: str, group_id: str, user_id: str) -> tuple[str, str, str]:
    return (archive_id, group_id, user_id)


def _pop_interrupt_messages(archive_id: str, group_id: str, user_id: str) -> list[str]:
    return pop_interrupt_messages(archive_id, group_id, user_id)


def _pop_interrupt_payloads(archive_id: str, group_id: str, user_id: str) -> list[dict]:
    return pop_interrupt_payloads(archive_id, group_id, user_id)


def _push_interrupt_message(req: InterruptMessageRequest) -> None:
    push_interrupt_message(req, maxlen=8)


@router.post("/auto-continue/check", response_model=AutoContinueCheckResponse)
async def auto_continue_check(req: AutoContinueCheckRequest) -> AutoContinueCheckResponse:
    """Decide whether a client should send an automatic follow-up "继续".

    This endpoint only judges the last user/request pair. It never starts a chat
    run; bridges/frontends own the decision to send the returned continue text.
    """
    if req.max_auto_continue_sec <= 0 or req.auto_continue_elapsed_sec >= req.max_auto_continue_sec:
        return _normalize_auto_continue({}, req)

    from app.core import guard_prompts as _gp
    messages = [
        {
            "role": "system",
            "content": _gp.AUTO_CONTINUE_JUDGE_SYSTEM,
        },
        {
            "role": "user",
            "content": _auto_continue_user_payload(req),
        },
    ]
    try:
        from app.llm import model_pool

        raw = await model_pool.chat_json(
            messages,
            model_spec=model_pool.resolve_task("auto_continue_check"),
            metrics_tag="json.auto_continue_check",
        )
    except Exception as exc:
        log.warning("auto_continue_check llm failed: %s", exc)
        debug.warn(f"auto_continue_check failed: {type(exc).__name__}: {exc}")
        return AutoContinueCheckResponse(
            should_continue=False,
            confidence=0.0,
            reason=f"llm_error:{type(exc).__name__}",
            continue_message="继续",
        )
    return _normalize_auto_continue(raw, req)


def _idempotency_check(req: ChatRequest) -> str | None:
    """Return None for a new request, or the prior trace_id for a duplicate."""
    if not req.client_msg_id:
        return None  # No client id: preserve backward compatibility and do not dedupe.

    now = time.time()
    key = (req.archive_id, req.group_id, req.user_id, req.client_msg_id)

    # Drop expired entries from the LRU head.
    while _idempotency_cache:
        first_key = next(iter(_idempotency_cache))
        ts = _idempotency_cache[first_key][1]
        if now - ts > _IDEMPOTENCY_TTL:
            _idempotency_cache.popitem(last=False)
        else:
            break

    # Replay duplicate requests within the TTL window.
    if key in _idempotency_cache:
        trace_id, ts, _done, _complete, _text = _idempotency_cache[key]
        if now - ts <= _IDEMPOTENCY_TTL:
            return trace_id
    return None


def _idempotency_register(req: ChatRequest, trace_id: str) -> None:
    if not req.client_msg_id:
        return
    key = (req.archive_id, req.group_id, req.user_id, req.client_msg_id)
    _idempotency_cache[key] = (trace_id, time.time(), None, None, "")
    if len(_idempotency_cache) > _IDEMPOTENCY_MAX:
        _idempotency_cache.popitem(last=False)


def _idempotency_record_event(
    req: ChatRequest, event_name: str, payload: dict,
) -> None:
    """Store done/complete payloads so duplicate SSE requests can replay fully."""
    if not req.client_msg_id:
        return
    key = (req.archive_id, req.group_id, req.user_id, req.client_msg_id)
    entry = _idempotency_cache.get(key)
    if not entry:
        return
    trace_id, ts, done_payload, complete_payload, text = entry
    if event_name == "done":
        done_payload = payload
    elif event_name == "complete":
        complete_payload = payload
    else:
        return
    _idempotency_cache[key] = (trace_id, ts, done_payload, complete_payload, text)


def _idempotency_record_token(req: ChatRequest, payload: dict) -> None:
    if not req.client_msg_id:
        return
    key = (req.archive_id, req.group_id, req.user_id, req.client_msg_id)
    entry = _idempotency_cache.get(key)
    if not entry:
        return
    trace_id, ts, done_payload, complete_payload, text = entry
    chunk = str(payload.get("text") or "")
    if not chunk:
        return
    _idempotency_cache[key] = (
        trace_id, ts, done_payload, complete_payload, (text + chunk)[-20000:],
    )


async def _ensure_environment_persona(archive_id: str, persona_id: str) -> None:
    if await archive_dao.get_persona_full(archive_id):
        return
    pf = persona_files.load_persona(persona_id or "environment")
    if pf is None:
        pf = persona_files.load_persona("environment")
    if pf:
        await archive_dao.upsert_persona(archive_id, pf.content)


# Executable deliverables are rejected directly instead of using a user-confirmation flow.
# A warning/confirmation prompt is poor UX for roleplay and ineffective against malicious files.
# The delivery endpoint below enforces the file policy at download time.
# Shared streaming endpoint. A non-empty current_dir enables environment mode.
@router.post("/stream")
async def chat_stream(req: ChatRequest):
    env_ctx: EnvironmentContext | None = None
    env_project: dict | None = None
    if req.current_dir.strip():
        try:
            env_project = await resolve_environment_project(
                user_id=req.user_id,
                current_dir=req.current_dir,
                project_id=req.project_id,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        await _ensure_environment_persona(env_project["archive_id"], req.persona_id)
        req = ChatRequest(
            archive_id=env_project["archive_id"],
            group_id=env_project["group_id"],
            user_id=req.user_id,
            user_name=req.user_name,
            message=req.message,
            client_msg_id=req.client_msg_id,
            attached_file_ids=req.attached_file_ids,
        )
        env_ctx = EnvironmentContext(
            root_dir=env_project["root_dir"],
            archive_id=env_project["archive_id"],
            group_id=env_project["group_id"],
            user_id=req.user_id,
            project_key=env_project["project_key"],
            project_name=env_project.get("project_name", ""),
        )
    else:
        if not req.archive_id or not req.group_id:
            raise HTTPException(
                status_code=422,
                detail="archive_id and group_id are required when current_dir is empty",
            )
        if not await archive_dao.get_archive(req.archive_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")

        active_archive = await bot_config.get_active_archive(req.group_id)
        if active_archive and active_archive != req.archive_id:
            log.warning(
                "archive_id mismatch: client sent %s but group %s active is %s",
                req.archive_id, req.group_id, active_archive,
            )
            debug.log(
                "archive.mismatch",
                f"client sent archive={req.archive_id} but active={active_archive}",
            )
            if settings.strict_active_archive:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "archive_mismatch",
                        "message": "request archive_id is not the active archive for this group",
                        "active_archive_id": active_archive,
                        "requested_archive_id": req.archive_id,
                    },
                )

    if req.attached_file_ids:
        attached = await bot_file_store.describe_attached_files(
            req.archive_id,
            req.group_id,
            req.attached_file_ids,
        )
        prefix = bot_file_store.attachment_prefix(attached)
        if prefix:
            req = ChatRequest(
                archive_id=req.archive_id,
                group_id=req.group_id,
                user_id=req.user_id,
                user_name=req.user_name,
                message=f"{prefix}\n\n{req.message}",
                client_msg_id=req.client_msg_id,
                current_dir=req.current_dir,
                project_id=req.project_id,
                persona_id=req.persona_id,
                attached_file_ids=req.attached_file_ids,
            )
            debug.log(
                "chat.attachments.injected",
                f"attached_files={len(attached)} ids={','.join(f['id'] for f in attached)}",
            )

    duplicate_trace = _idempotency_check(req)
    if duplicate_trace is not None:
        debug.set_trace_id(duplicate_trace)
        debug.log(
            "chat.duplicate",
            f"client_msg_id={req.client_msg_id!r} reused trace={duplicate_trace}",
        )
        _key = (req.archive_id, req.group_id, req.user_id, req.client_msg_id)

        async def _replay():
            meta_payload = {"trace_id": duplicate_trace, "duplicate": True}
            if env_project is not None:
                meta_payload["environment"] = env_project
            yield {
                "event": "meta",
                "data": json.dumps(meta_payload, ensure_ascii=False),
            }
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                entry = _idempotency_cache.get(_key)
                if entry and entry[2] is not None and entry[3] is not None:
                    break
                await asyncio.sleep(0.1)
            entry = _idempotency_cache.get(_key) or (duplicate_trace, 0, None, None, "")
            _, _, done_p, complete_p, cached_text = entry
            suppress_cached_text = bool(
                isinstance(done_p, dict)
                and done_p.get("voice_reply")
                and done_p.get("_suppress_text")
            )
            if cached_text and not suppress_cached_text:
                yield {
                    "event": "token",
                    "data": json.dumps(
                        {"text": cached_text, "duplicate": True}, ensure_ascii=False,
                    ),
                }
            elif cached_text and suppress_cached_text:
                debug.log(
                    "chat.duplicate.voice_text_suppressed",
                    "duplicate replay skipped cached text because original done event delivered voice",
                )
            if done_p is not None:
                yield {
                    "event": "done",
                    "data": json.dumps(
                        {**done_p, "duplicate": True}, ensure_ascii=False,
                    ),
                }
            yield {
                "event": "complete",
                "data": json.dumps(
                    {**(complete_p or {}), "trace_id": duplicate_trace, "duplicate": True},
                    ensure_ascii=False,
                ),
            }
        return EventSourceResponse(_replay(), headers=SSE_RESPONSE_HEADERS)

    trace_id = uuid.uuid4().hex[:16]
    _idempotency_register(req, trace_id)
    guard = get_group_guard()

    try:
        await guard.acquire(
            req.archive_id, req.group_id, req.user_id, trace_id,
            user_name=req.user_name,
        )
    except GroupBusyError as e:
        debug.set_trace_id(trace_id)
        debug.log(
            "user.busy",
            f"rejected: archive={req.archive_id} group={req.group_id} "
            f"user={req.user_id} holder={e.holder_trace}",
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "user_busy",
                "message": str(e),
                "active_trace_id": e.holder_trace,
                "trace_id": trace_id,
            },
        )

    debug.set_trace_id(trace_id)
    debug.log(
        "user.acquired",
        f"archive={req.archive_id} group={req.group_id} user={req.user_id}",
    )

    def _record_and_format_event(event_name: str, payload: dict) -> dict:
        if event_name == "progress" and isinstance(payload, dict):
            kind = payload.get("kind") or payload.get("round") or "progress"
            payload = {
                "kind": kind,
                "message": payload.get("message") or str(kind),
                **payload,
            }
        if event_name in ("done", "complete") and isinstance(payload, dict):
            _idempotency_record_event(req, event_name, payload)
            if event_name == "done" and payload.get("files"):
                _record_delivered_files_background(
                    req.archive_id,
                    req.group_id,
                    payload.get("files"),
                )
        elif event_name == "token" and isinstance(payload, dict):
            _idempotency_record_token(req, payload)
        elif event_name == "intermediate_reply" and isinstance(payload, dict):
            payload = {
                "kind": "intermediate_reply",
                "message": payload.get("message") or "",
                **payload,
            }
        return {
            "event": event_name,
            "data": json.dumps(payload, ensure_ascii=False),
        }

    async def event_gen():
        event_queue: asyncio.Queue | None = asyncio.Queue() if env_ctx is not None else None
        orch_task = None
        env_task = None
        dream_sup = None
        interrupt_state = {"loaded": False, "payloads": [], "seen": set()}

        def _shared_interrupt_payloads() -> list[dict]:
            fresh = _pop_interrupt_payloads(
                req.archive_id, req.group_id, req.user_id,
            )
            if fresh:
                for item in fresh:
                    payload = dict(item)
                    client_msg_id = str(payload.get("client_msg_id") or "").strip()
                    signature = client_msg_id or repr((
                        payload.get("kind"),
                        payload.get("source"),
                        payload.get("message"),
                        payload.get("meta"),
                    ))
                    if signature in interrupt_state["seen"]:
                        continue
                    interrupt_state["seen"].add(signature)
                    interrupt_state["payloads"].append(payload)
                interrupt_state["loaded"] = True
            elif not interrupt_state["loaded"]:
                interrupt_state["loaded"] = True
            return [dict(item) for item in interrupt_state["payloads"]]

        def _shared_interrupt_messages() -> list[str]:
            return [
                str(item.get("message") or "").strip()
                for item in _shared_interrupt_payloads()
                if str(item.get("kind") or "user") == "user" and str(item.get("message") or "").strip()
            ]
        try:
            try:
                from app.core.dream import supervisor as dream_sup
                if hasattr(dream_sup, "mark_main_request_start"):
                    dream_sup.mark_main_request_start()
            except Exception:
                dream_sup = None
            if env_ctx is None:
                async for event_name, payload in orchestrate(
                    req,
                    trace_id=trace_id,
                    interrupt_messages_getter=_shared_interrupt_messages,
                    interrupt_payloads_getter=_shared_interrupt_payloads,
                ):
                    yield _record_and_format_event(event_name, payload)
            else:
                yield {
                    "event": "meta",
                    "data": json.dumps({"trace_id": trace_id, "environment": env_project}, ensure_ascii=False),
                }
                with runtime_context("environment", env_ctx), environment_event_sink(
                    event_queue,
                    archive_id=req.archive_id,
                    group_id=req.group_id,
                    user_id=req.user_id,
                    trace_id=trace_id,
                ):
                    agen = orchestrate(
                        req,
                        trace_id=trace_id,
                        interrupt_messages_getter=_shared_interrupt_messages,
                        interrupt_payloads_getter=_shared_interrupt_payloads,
                    ).__aiter__()
                    orch_task = asyncio.create_task(agen.__anext__())
                    env_task = asyncio.create_task(event_queue.get())
                    while orch_task or env_task:
                        active = {t for t in (orch_task, env_task) if t is not None}
                        done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            try:
                                item = task.result()
                            except StopAsyncIteration:
                                if task is orch_task:
                                    orch_task = None
                                    if env_task is not None:
                                        env_task.cancel()
                                        env_task = None
                                break
                            if task is orch_task:
                                event_name, payload = item
                                yield _record_and_format_event(event_name, payload)
                                orch_task = asyncio.create_task(agen.__anext__())
                            else:
                                env_event, env_payload = item
                                yield {
                                    "event": env_event,
                                    "data": json.dumps(env_payload, ensure_ascii=False),
                                }
                                env_task = asyncio.create_task(event_queue.get())
        except asyncio.CancelledError:
            debug.log(
                "chat.stream_cancelled",
                f"client disconnected/cancelled trace={trace_id}",
            )
            try:
                await guard.signal_abort(
                    archive_id=req.archive_id,
                    group_id=req.group_id,
                    user_id=req.user_id,
                )
                debug.log(
                    "chat.stream_cancelled.abort_signalled",
                    f"cooperative abort signalled for cancelled stream trace={trace_id}",
                )
            except Exception as exc:
                debug.warn(
                    f"chat stream cancellation abort signal failed: {type(exc).__name__}: {exc}"
                )
            raise
        except Exception as e:
            log.exception("chat_stream failed")
            debug.error(f"chat stream failed: {type(e).__name__}: {e}")
            yield {
                "event": "error",
                "data": json.dumps(
                    {"code": "internal_error", "message": str(e)},
                    ensure_ascii=False,
                ),
            }
        finally:
            for task in (orch_task, env_task):
                if task is not None and not task.done():
                    task.cancel()
            cleanup_tasks = [t for t in (orch_task, env_task) if t is not None]
            if cleanup_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*cleanup_tasks, return_exceptions=True),
                        timeout=5.0,
                    )
                except asyncio.CancelledError:
                    # Client disconnect cancellation must not skip guard.release below.
                    debug.log(
                        "chat.cleanup_cancelled",
                        f"cleanup wait cancelled; releasing guard trace={trace_id}",
                    )
                except asyncio.TimeoutError:
                    debug.warn(f"chat cleanup timed out; releasing guard trace={trace_id}")
            try:
                release_task = asyncio.create_task(guard.release(
                    req.archive_id, req.group_id, req.user_id, trace_id,
                ))
                released = await asyncio.shield(release_task)
            except asyncio.CancelledError:
                released = await release_task
            except Exception as e:
                log.exception("chat_stream guard release failed")
                debug.error(f"user.release failed: {type(e).__name__}: {e}")
            else:
                debug.log(
                    "user.released",
                    f"released={released} archive={req.archive_id} "
                    f"group={req.group_id} user={req.user_id}",
                )
            if dream_sup is not None and hasattr(dream_sup, "mark_main_request_done"):
                try:
                    dream_sup.mark_main_request_done()
                except Exception:
                    pass

    return EventSourceResponse(event_gen(), headers=SSE_RESPONSE_HEADERS)


@router.post("/interrupt_message")
async def interrupt_message(req: InterruptMessageRequest) -> dict:
    guard = get_group_guard()
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
    active = await guard.is_busy(archive_id, group_id, user_id)
    if active:
        queue_req = req.model_copy(update={"archive_id": archive_id, "group_id": group_id, "user_id": user_id})
        _push_interrupt_message(queue_req)
        stage = guard.get_stage(archive_id, group_id, user_id) if hasattr(guard, "get_stage") else ""
        signaled = await guard.signal_abort(
            archive_id=archive_id,
            group_id=group_id,
            user_id=user_id,
        )
        debug.log(
            "chat.interrupt_message.abort",
            f"active={active} signaled={signaled} stage={stage or 'none'} archive={archive_id} group={group_id} user={user_id}",
        )
        return {
            "ok": active,
            "queued": True,
            "aborted": signaled,
            "stage": stage,
            "reason": "" if signaled else ("queued_no_preempt" if stage in {"round3", "round2_5"} else "not_preempted"),
        }
    return {"ok": active}


@router.post("/files/{archive_id}/{group_id}/upload")
async def upload_bot_file(
    archive_id: str,
    group_id: str,
    request: Request,
    filename: str,
    user_id: str = "",
    user_name: str = "",
) -> dict:
    """Upload a local frontend file into the bot file area.

    This endpoint intentionally accepts a raw request body instead of multipart
    so the local frontend does not add a new python-multipart runtime dependency.
    """
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    content = await request.body()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty upload")
    try:
        item = await bot_file_store.save_uploaded_file(
            archive_id=archive_id,
            group_id=group_id,
            user_id=user_id,
            user_name=user_name,
            filename=filename,
            content_type=request.headers.get("content-type", ""),
            source=BytesIO(content),
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return {"ok": True, "file": item}


@router.get("/files/{archive_id}/{group_id}")
async def list_bot_files(archive_id: str, group_id: str) -> dict:
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    return {
        "ok": True,
        "archive_id": archive_id,
        "group_id": group_id,
        "items": await bot_file_store.list_bot_files(archive_id, group_id),
    }


@router.get("/artifacts/{archive_id}/{group_id}")
async def list_workspace_artifacts(archive_id: str, group_id: str) -> dict:
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    return {
        "ok": True,
        "archive_id": archive_id,
        "group_id": group_id,
        "items": await bot_artifacts.list_delivered_files(archive_id, group_id),
    }


@router.delete("/files/{archive_id}/{group_id}/{file_id}")
async def delete_bot_file(archive_id: str, group_id: str, file_id: str) -> dict:
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    ok = await bot_file_store.mark_bot_file_deleted(archive_id, group_id, file_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    return {"ok": True, "file_id": file_id}


# Active requests and monitoring endpoints.
@router.get("/active")
async def list_active() -> dict:
    # Active holder list.
    holders = await get_group_guard().active_holders()
    return {
        "items": [
            {
                "archive_id": k[0], "group_id": k[1], "user_id": k[2],
                "trace_id": v,
            }
            for k, v in holders.items()
        ]
    }


# Interrupt and monitor helpers.


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


def _sse(event_name: str, payload: dict) -> dict:
    return {"event": event_name, "data": json.dumps(payload, ensure_ascii=False)}


@router.get("/monitor")
async def monitor_chat(
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
async def active_chat_commands(
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
async def monitor_history(
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


@router.get("/runs/{trace_id}")
async def chat_run_snapshot(
    trace_id: str,
    archive_id: str = "",
    group_id: str = "",
    user_id: str = "",
    limit: int = 500,
) -> dict:
    return await env_monitor.run_snapshot(
        trace_id,
        archive_id=archive_id,
        group_id=group_id,
        user_id=user_id,
        limit=limit,
    )


@router.post("/commands/{command_id}/abort")
async def abort_chat_command(command_id: str) -> dict:
    return await env_monitor.abort_command(command_id)


@router.post("/abort")
async def abort_chat(body: dict):
    # Signal abort for the active (archive_id, group_id, user_id) task.
    guard = get_group_guard()
    user_id = body.get("user_id")
    archive_id = body.get("archive_id")
    group_id = body.get("group_id")
    current_dir = str(body.get("current_dir") or "").strip()
    project_id = str(body.get("project_id") or "").strip()
    if current_dir:
        if not user_id:
            raise HTTPException(
                status_code=422,
                detail="missing required fields: user_id",
            )
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
    missing = [
        name for name, val in
        (("archive_id", archive_id), ("group_id", group_id), ("user_id", user_id))
        if not val
    ]
    if missing:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=422,
            detail=f"missing required fields: {', '.join(missing)}",
        )
    # Log abort requests so interrupt routing is diagnosable.
    debug.log(
        "chat.abort.received",
        f"archive={archive_id} group={group_id} user={user_id}",
    )
    ok = await guard.signal_abort(
        archive_id=archive_id,
        group_id=group_id,
        user_id=user_id,
    )
    stage = guard.get_stage(archive_id, group_id, user_id) if hasattr(guard, "get_stage") else ""
    if not ok:
        debug.log("chat.abort.done", f"ok={ok} stage={stage or 'none'}")
        return {"ok": False, "reason": "no_active_task" if not stage else f"stage={stage}"}
    # signal_abort affects the current task; stop mode also suppresses immediate follow-up work.
    guard.enter_stop_mode(
        archive_id=archive_id,
        group_id=group_id,
        user_id=user_id,
        duration_sec=20.0,
    )
    debug.log("chat.abort.done", f"ok={ok} stage={stage or 'none'} stop_mode_entered=20s")
    response = {"ok": ok}
    if stage:
        response["stage"] = stage
    return response


@router.get("/stage")
async def get_chat_stage(archive_id: str, group_id: str, user_id: str):
    """查询当前 (archive, group, user) 的 chat 处理阶段。
    返回值: {"stage": "" | "round1" | "round2" | "round3"}
    供 bridge 决定 abort 是否安全(round3 期间不打断流式回复)。
    """
    guard = get_group_guard()
    stage = guard.get_stage(archive_id, group_id, user_id)
    return {"stage": stage}


# Workspace file download endpoint.


@router.get("/files/{archive_id}/{group_id}/{filename:path}")
async def download_file(archive_id: str, group_id: str, filename: str, workspace_token: str = ""):
    # Download an allowed generated workspace file.
    # Prefer the persistent workspace; fall back to active/temp workspaces.
    group_key = f"{archive_id}:{group_id}"
    persistent = ws_tool.get_persistent_workspace_path(archive_id, group_id)

    candidates: list[str] = []
    seen_candidates: set[str] = set()
    def _add_candidate(path: str | None) -> None:
        if not path or not os.path.isdir(path):
            return
        key = os.path.normcase(os.path.abspath(path))
        if key in seen_candidates:
            return
        seen_candidates.add(key)
        candidates.append(path)

    registered = ws_tool.get_registered_workspaces(group_key)
    if workspace_token:
        registered = [
            ws_dir for ws_dir in registered
            if ws_tool.token_matches_workspace(ws_dir, workspace_token)
        ]
        for ws_dir in registered:
            _add_candidate(ws_dir)
        _add_candidate(persistent)
    else:
        _add_candidate(persistent)
        for ws_dir in registered:
            _add_candidate(ws_dir)
    # Include .temp when deliverables have not yet been promoted.
    if persistent and not workspace_token:
        temp_path = os.path.join(persistent, ".temp")
        _add_candidate(temp_path)

    if not candidates:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no active workspace for this group")

    # Resolve inside each candidate workspace and reject traversal.
    file_path = None
    ws_dir_used = None
    for cand in candidates:
        try:
            resolved = ws_tool._safe_resolve(cand, filename)
        except ValueError:
            continue
        try:
            Path(resolved).resolve().relative_to(Path(cand).resolve())
        except ValueError:
            continue
        if os.path.isfile(resolved) and not os.path.islink(resolved):
            file_path = resolved
            ws_dir_used = cand
            break

    if not file_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"file not found: {filename}")

    # Keep size and file policy checks below.
    if os.path.getsize(file_path) > MAX_DOWNLOAD_BYTES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "file too large")

    decision = classify_file_for_delivery(os.path.basename(file_path), file_path)
    if not decision.allowed:
        raise HTTPException(decision.status_code, decision.reason)

    return FileResponse(file_path, filename=os.path.basename(file_path))


@router.get("/file-preview/{archive_id}/{group_id}/{filename:path}")
async def preview_workspace_file(
    archive_id: str,
    group_id: str,
    filename: str,
    max_chars: int = 120000,
    workspace_token: str = "",
):
    group_key = f"{archive_id}:{group_id}"
    persistent = ws_tool.get_persistent_workspace_path(archive_id, group_id)
    candidates: list[str] = []
    seen_candidates: set[str] = set()
    def _add_candidate(path: str | None) -> None:
        if not path or not os.path.isdir(path):
            return
        key = os.path.normcase(os.path.abspath(path))
        if key in seen_candidates:
            return
        seen_candidates.add(key)
        candidates.append(path)

    registered = ws_tool.get_registered_workspaces(group_key)
    if workspace_token:
        registered = [
            ws_dir for ws_dir in registered
            if ws_tool.token_matches_workspace(ws_dir, workspace_token)
        ]
        for ws_dir in registered:
            _add_candidate(ws_dir)
        _add_candidate(persistent)
    else:
        _add_candidate(persistent)
        for ws_dir in registered:
            _add_candidate(ws_dir)
    for cand in candidates:
        try:
            resolved = ws_tool._safe_resolve(cand, filename)
            Path(resolved).resolve().relative_to(Path(cand).resolve())
        except ValueError:
            continue
        if os.path.isfile(resolved) and not os.path.islink(resolved):
            return preview_file(resolved, max_chars=max_chars)
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"file not found: {filename}")
