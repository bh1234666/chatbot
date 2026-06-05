from __future__ import annotations

import json
from typing import Any

DEFAULT_MAX_RESULT_CHARS = 12000
_TOOL_MAX_RESULT_CHARS = {
    # Keep this aligned with chat_with_tools_loop's P44 budget. Registry-level
    # budgeting runs first, so a lower value here silently starves the main loop.
    "delegate": 50 * 1024,
    "read_skill": 20000,
    "wait_helper": 50 * 1024,
    "processes": 16000,
    "read_file": 16000,
    "search_across_files": 16000,
    "search_in_file": 16000,
    "workspace": 16000,
    "bash": 16000,
}


def max_result_chars(tool_name: str) -> int:
    return _TOOL_MAX_RESULT_CHARS.get(tool_name, DEFAULT_MAX_RESULT_CHARS)


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool, int]:
    original_chars = len(text)
    if original_chars <= max_chars:
        return text, False, original_chars
    marker = f"\n\n[tool result truncated: original_chars={original_chars}, max_chars={max_chars}]"
    keep = max(0, max_chars - len(marker))
    return text[:keep] + marker, True, original_chars


def _budget_json_object(obj: Any, max_chars: int) -> str | None:
    if not isinstance(obj, dict):
        return None
    raw = json.dumps(obj, ensure_ascii=False)
    if len(raw) <= max_chars:
        return raw

    budgeted = dict(obj)
    for key in ("content", "text", "stdout", "stderr", "output", "result", "report"):
        value = budgeted.get(key)
        if isinstance(value, str):
            budgeted[key], _, original_chars = _truncate_text(value, max_chars // 2)
            budgeted.setdefault("truncated", True)
            budgeted.setdefault("original_chars", original_chars)
            compact = json.dumps(budgeted, ensure_ascii=False)
            if len(compact) <= max_chars:
                return compact

    compact, _, original_chars = _truncate_text(raw, max_chars)
    return json.dumps(
        {
            "ok": obj.get("ok", True),
            "content": compact,
            "truncated": True,
            "original_chars": original_chars,
        },
        ensure_ascii=False,
    )


def apply_result_budget(tool_name: str, result: str, max_chars: int | None = None) -> str:
    limit = max_chars or max_result_chars(tool_name)
    if len(result) <= limit:
        return result

    try:
        obj = json.loads(result)
    except Exception:
        text, _, original_chars = _truncate_text(result, limit)
        return json.dumps(
            {
                "ok": True,
                "content": text,
                "truncated": True,
                "original_chars": original_chars,
            },
            ensure_ascii=False,
        )

    budgeted = _budget_json_object(obj, limit)
    if budgeted is not None:
        return budgeted

    raw = json.dumps(obj, ensure_ascii=False)
    text, _, original_chars = _truncate_text(raw, limit)
    return json.dumps(
        {
            "ok": True,
            "content": text,
            "truncated": True,
            "original_chars": original_chars,
        },
        ensure_ascii=False,
    )
