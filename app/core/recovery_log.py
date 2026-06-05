from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)


def _safe_segment(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(value))[:32]


async def write_recovery_jsonl(
    *,
    archive_id: str,
    group_id: str,
    user_id: str,
    speaker: str,
    user_message: str,
    assistant_message: str,
    trace_id: str,
    hot_write_ok: bool,
    gm_write_ok: bool,
    debug: Any | None = None,
) -> None:
    if settings.workspace_root:
        recov_root = Path(settings.workspace_root).parent / "recovery"
    else:
        proj_root = Path(__file__).resolve().parent.parent.parent
        recov_root = proj_root / "data" / "recovery"
    try:
        recov_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.error("[%s] recovery dir mkdir failed: %s", trace_id, recov_root)
        return

    fname = f"{_safe_segment(archive_id)}_{_safe_segment(group_id)}_{_safe_segment(user_id)}.jsonl"
    fpath = recov_root / fname
    record = {
        "trace_id": trace_id,
        "ts": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",  # naive-UTC,输出与原 utcnow() 逐字符一致、无 Deprecation
        "archive_id": archive_id,
        "group_id": group_id,
        "user_id": user_id,
        "user_name": speaker,
        "user_message": user_message,
        "assistant_message": assistant_message,
        "hot_write_ok": hot_write_ok,
        "gm_write_ok": gm_write_ok,
    }
    try:
        with open(fpath, "a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
        if debug is not None:
            debug.log(
                "memory.recovery.jsonl",
                f"wrote fallback record to {fpath.name} "
                f"(hot_ok={hot_write_ok} gm_ok={gm_write_ok})",
            )
        log.warning(
            "[%s] DB write fallback to %s — manually re-import when DB recovers",
            trace_id,
            fpath,
        )
    except OSError as exc:
        log.error("[%s] recovery jsonl IO failed: %s", trace_id, exc)
