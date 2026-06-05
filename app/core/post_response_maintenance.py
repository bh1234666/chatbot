from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from app.core.bg_tasks import schedule
from app.core.recovery_log import write_recovery_jsonl
from app.core.user_profile_maintenance import bg_user_profile_update
from app.memory import group_messages as gm
from app.memory import hot
import app.memory.kb as kb_mem
from app.schemas.api import ChatRequest, ResponsePlan

log = logging.getLogger(__name__)

_BOT_LOG_BLOCK_RE = re.compile(r"\s*<bot_log>.*?</bot_log>\s*", re.DOTALL)


def visible_assistant_text(text: str) -> str:
    return _BOT_LOG_BLOCK_RE.sub("", text or "").strip()


async def post_response_maintenance(
    *,
    req: ChatRequest,
    user_message: str,
    assistant_message: str,
    tendencies: dict[str, float],
    plan: ResponsePlan,
    trace_id: str,
    generated_files: list[tuple[str, str]] | None = None,
    workspace_dir: str = "",
    progress_messages: list[str] | None = None,
    finalize_and_compress: Any,
    debug: Any | None = None,
) -> None:
    del tendencies, plan
    speaker = req.user_name or req.user_id
    assistant_visible = visible_assistant_text(assistant_message) or assistant_message
    hot_write_ok = False
    gm_write_ok = False
    try:
        try:
            await hot.append_user_turn(
                archive_id=req.archive_id,
                group_id=req.group_id,
                user_id=req.user_id,
                user_content=user_message,
                assistant_content=assistant_message,
            )
        except Exception:
            log.exception("[%s] append_user_turn failed; will write fallback jsonl", trace_id)
        else:
            hot_write_ok = True
            if debug is not None:
                debug.log("memory.hot.user.write", "appended one turn (sync)")

        gm_user_ok = False
        gm_bot_ok = False
        try:
            await gm.append_message(
                archive_id=req.archive_id,
                group_id=req.group_id,
                user_id=req.user_id,
                user_name=speaker,
                content=user_message,
                addressed_bot=True,
            )
            gm_user_ok = True
        except Exception:
            log.exception("[%s] append group_messages (user msg) failed", trace_id)
        try:
            await gm.append_message(
                archive_id=req.archive_id,
                group_id=req.group_id,
                user_id=None,
                user_name="机器人",
                content=assistant_visible,
                addressed_bot=False,
            )
            gm_bot_ok = True
        except Exception:
            log.exception("[%s] append group_messages (bot msg) failed", trace_id)
        if gm_user_ok and gm_bot_ok:
            gm_write_ok = True
            if debug is not None:
                debug.log("memory.group_messages.write", "appended user msg + bot reply (sync)")
    except Exception:
        log.exception("[%s] sync writes raised unexpectedly", trace_id)

    if not hot_write_ok or not gm_write_ok:
        try:
            await write_recovery_jsonl(
                archive_id=req.archive_id,
                group_id=req.group_id,
                user_id=req.user_id,
                speaker=speaker,
                user_message=user_message,
                assistant_message=assistant_message,
                trace_id=trace_id,
                hot_write_ok=hot_write_ok,
                gm_write_ok=gm_write_ok,
                debug=debug,
            )
        except Exception:
            log.exception("[%s] recovery jsonl write also failed; data lost", trace_id)

    if generated_files and workspace_dir:
        schedule(
            kb_mem.index_generated_files(
                archive_id=req.archive_id,
                group_id=req.group_id,
                workspace_dir=workspace_dir,
                generated_files=generated_files,
                user_message=user_message,
                bot_response=assistant_visible,
            ),
            name="orch.index_files",
        )

    schedule(
        finalize_and_compress(
            archive_id=req.archive_id,
            group_id=req.group_id,
            user_id=req.user_id,
            speaker=speaker,
            user_message=user_message,
            assistant_message=assistant_message,
            progress_messages=progress_messages or [],
            trace_id=trace_id,
        ),
        name="orch.finalize_compress",
    )
    schedule(
        bg_user_profile_update(
            archive_id=req.archive_id,
            user_id=req.user_id,
            user_message=user_message,
            assistant_message=assistant_message,
            trace_id=trace_id,
            debug=debug,
        ),
        name="orch.user_profile",
    )
