from __future__ import annotations

import json
import tempfile
from typing import Any

from app.llm.tools.output_spill import write_tool_output_spill

DEFAULT_MAX_RESULT_CHARS = 12000
DEFAULT_MAX_TEXT_FIELD_CHARS = 8000
DEFAULT_TEXT_FIELD_HEAD_CHARS = 3000
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

_TOOL_MAX_TEXT_FIELD_CHARS = {
    "bash": 6000,
    "workspace": 6000,
    "env_run": 6000,
    "env_read": 6000,
    "read_file": 6000,
}


def max_result_chars(tool_name: str) -> int:
    return _TOOL_MAX_RESULT_CHARS.get(tool_name, DEFAULT_MAX_RESULT_CHARS)


def max_text_field_chars(tool_name: str) -> int:
    return _TOOL_MAX_TEXT_FIELD_CHARS.get(tool_name, DEFAULT_MAX_TEXT_FIELD_CHARS)


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool, int]:
    original_chars = len(text)
    if original_chars <= max_chars:
        return text, False, original_chars
    marker = f"\n\n[tool result truncated: original_chars={original_chars}, max_chars={max_chars}]"
    keep = max(0, max_chars - len(marker))
    return text[:keep] + marker, True, original_chars


_TEXT_FIELD_KEYS = (
    "content", "contents", "file_content", "file_contents", "raw_content",
    "raw_text", "text", "preview", "stdout", "stderr", "output", "result",
    "report", "summary", "details", "detail", "error", "errors",
    "error_result", "error_output", "message", "exception", "exceptions",
    "diagnostics", "diagnostic", "warning", "warnings", "traceback",
    "tracebacks", "logs", "log", "partial_stdout", "partial_stderr",
    "body", "diff",
)
_TEXT_FIELD_KEY_SET = set(_TEXT_FIELD_KEYS)


def _has_long_text_field(value: Any, field_limit: int) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if (
                isinstance(key, str)
                and key in _TEXT_FIELD_KEY_SET
                and isinstance(item, str)
                and len(item) > field_limit
            ):
                return True
            if isinstance(item, (dict, list)) and _has_long_text_field(item, field_limit):
                return True
    elif isinstance(value, list):
        return any(_has_long_text_field(item, field_limit) for item in value)
    return False


def _spill_full_result(
    *,
    tool_name: str,
    result: str,
    spill_root: str | None,
) -> str:
    return write_tool_output_spill(
        root_dir=spill_root or tempfile.gettempdir(),
        tool_name=tool_name,
        label="full_result",
        text=result,
        extension=".json",
    )


def _truncation_policy(saved_path: str) -> str:
    return (
        "A real tool result exceeded the model-visible budget and was truncated before entering "
        f"model context. The full result was saved at `{saved_path}` (`full_result_saved_path`); "
        "only the head excerpt is shown here. Treat the saved path as a recovery source for named missing details, "
        "folded evidence, or explicit display/quote needs; do not reread it just to confirm that this same result existed.\n"
        "真实工具结果过长，完整内容已保存；当前上下文只显示头部摘录。保存路径用于命名缺口恢复或显式展示/引用，不必仅为确认同一结果存在而重读。"
    )


def _budget_json_object(
    obj: Any,
    max_chars: int,
    *,
    saved_path: str | None = None,
    spill_root: str | None = None,
    tool_name: str = "tool",
    field_max_chars: int = DEFAULT_MAX_TEXT_FIELD_CHARS,
    field_head_chars: int = DEFAULT_TEXT_FIELD_HEAD_CHARS,
) -> str | None:
    if not isinstance(obj, dict):
        return None
    raw = json.dumps(obj, ensure_ascii=False)
    long_text_keys = [
        key for key in _TEXT_FIELD_KEYS
        if isinstance(obj.get(key), str) and len(str(obj.get(key))) > field_max_chars
    ]
    has_nested_long_text = _has_long_text_field(obj, field_max_chars)
    if len(raw) <= max_chars and not long_text_keys and not has_nested_long_text:
        return raw

    budgeted = dict(obj)
    if saved_path:
        budgeted["tool_result_truncated"] = True
        budgeted["output_truncated"] = True
        budgeted["full_result_saved_path"] = saved_path
        budgeted["full_result_original_chars"] = len(raw)
        budgeted["visible_excerpt_policy"] = _truncation_policy(saved_path)
    field_saved_paths: dict[str, str] = {}

    def field_saved_path(key: str, text: str) -> str | None:
        if not saved_path:
            return None
        existing = budgeted.get(f"{key}_full_saved_path")
        if isinstance(existing, str) and existing:
            return existing
        if key not in field_saved_paths:
            field_saved_paths[key] = write_tool_output_spill(
                root_dir=spill_root or tempfile.gettempdir(),
                tool_name=tool_name,
                label=key,
                text=text,
                extension=".txt",
            )
        return field_saved_paths[key]

    def _budget_nested_text_fields(value: Any, path: tuple[str, ...] = ()) -> Any:
        if isinstance(value, dict):
            nested = dict(value)
            for key, item in list(value.items()):
                key_text = str(key)
                if (
                    isinstance(key, str)
                    and key in _TEXT_FIELD_KEY_SET
                    and isinstance(item, str)
                    and len(item) > field_max_chars
                ):
                    label = "_".join((*path, key_text)) or key_text
                    nested[key], _, original_chars = _truncate_text(item, field_head_chars)
                    nested.setdefault("truncated", True)
                    nested["output_truncated"] = True
                    nested.setdefault("original_chars", original_chars)
                    nested[f"{key}_truncated"] = True
                    nested[f"{key}_original_chars"] = original_chars
                    field_path = field_saved_path(label, item)
                    if field_path:
                        nested[f"{key}_full_saved_path"] = field_path
                        nested[f"{key}_excerpt_policy"] = (
                            f"Only the head excerpt is shown; full text was saved at `{field_path}` "
                            f"(`{key}_full_saved_path`)."
                        )
                elif isinstance(item, (dict, list)):
                    nested[key] = _budget_nested_text_fields(item, (*path, key_text))
            return nested
        if isinstance(value, list):
            return [
                _budget_nested_text_fields(item, (*path, str(idx)))
                if isinstance(item, (dict, list)) else item
                for idx, item in enumerate(value)
            ]
        return value

    for key in _TEXT_FIELD_KEYS:
        value = budgeted.get(key)
        if isinstance(value, str):
            if key not in long_text_keys and len(raw) <= max_chars:
                continue
            original_value = value
            budgeted[key], _, original_chars = _truncate_text(original_value, field_head_chars)
            budgeted.setdefault("truncated", True)
            budgeted["output_truncated"] = True
            existing_original = int(budgeted.get(f"{key}_original_chars") or 0)
            budgeted.setdefault("original_chars", max(original_chars, existing_original))
            budgeted[f"{key}_truncated"] = True
            budgeted[f"{key}_original_chars"] = max(original_chars, existing_original)
            field_path = field_saved_path(key, original_value)
            if field_path:
                budgeted[f"{key}_full_saved_path"] = field_path
                budgeted[f"{key}_excerpt_policy"] = (
                    f"Only the head excerpt is shown; full text was saved at `{field_path}` "
                    f"(`{key}_full_saved_path`)."
                )
    budgeted = _budget_nested_text_fields(budgeted)
    compact = json.dumps(budgeted, ensure_ascii=False)
    if len(compact) <= max_chars:
        return compact

    for key in _TEXT_FIELD_KEYS:
        value = budgeted.get(key)
        if isinstance(value, str) and len(value) > 1000:
            original_value = value
            budgeted[key], _, original_chars = _truncate_text(original_value, 1000)
            budgeted.setdefault("truncated", True)
            budgeted["output_truncated"] = True
            existing_original = int(budgeted.get(f"{key}_original_chars") or 0)
            budgeted.setdefault("original_chars", max(original_chars, existing_original))
            budgeted[f"{key}_truncated"] = True
            budgeted[f"{key}_original_chars"] = max(
                existing_original,
                original_chars,
            )
            field_path = field_saved_path(key, original_value)
            if field_path:
                budgeted[f"{key}_full_saved_path"] = field_path
                budgeted[f"{key}_excerpt_policy"] = (
                    f"Only the head excerpt is shown; full text was saved at `{field_path}` "
                    f"(`{key}_full_saved_path`)."
                )
    compact = json.dumps(budgeted, ensure_ascii=False)
    if len(compact) <= max_chars:
        return compact

    compact, _, original_chars = _truncate_text(raw, max_chars)
    wrapped = {
        "ok": obj.get("ok", True),
        "content": compact,
        "truncated": True,
        "output_truncated": True,
        "original_chars": original_chars,
    }
    if saved_path:
        wrapped.update({
            "tool_result_truncated": True,
            "full_result_saved_path": saved_path,
            "full_result_original_chars": len(raw),
            "visible_excerpt_policy": _truncation_policy(saved_path),
        })
    return json.dumps(wrapped, ensure_ascii=False)


def apply_result_budget(
    tool_name: str,
    result: str,
    max_chars: int | None = None,
    *,
    spill_root: str | None = None,
    field_max_chars: int | None = None,
    field_head_chars: int = DEFAULT_TEXT_FIELD_HEAD_CHARS,
) -> str:
    limit = max_chars or max_result_chars(tool_name)
    field_limit = field_max_chars or max_text_field_chars(tool_name)
    parsed_obj: Any | None = None
    has_long_text_field = False
    try:
        parsed_obj = json.loads(result)
        if isinstance(parsed_obj, dict):
            has_long_text_field = _has_long_text_field(parsed_obj, field_limit)
    except Exception:
        parsed_obj = None
    if len(result) <= limit and not has_long_text_field:
        return result
    saved_path = _spill_full_result(
        tool_name=tool_name,
        result=result,
        spill_root=spill_root,
    )

    if parsed_obj is None:
        content_saved_path = write_tool_output_spill(
            root_dir=spill_root or tempfile.gettempdir(),
            tool_name=tool_name,
            label="content",
            text=result,
            extension=".txt",
        )
        text, _, original_chars = _truncate_text(result, limit)
        return json.dumps(
            {
                "ok": True,
                "content": text,
                "truncated": True,
                "original_chars": original_chars,
                "content_truncated": True,
                "content_original_chars": original_chars,
                "content_full_saved_path": content_saved_path,
                "tool_result_truncated": True,
                "output_truncated": True,
                "full_result_saved_path": saved_path,
                "full_result_original_chars": original_chars,
                "visible_excerpt_policy": _truncation_policy(saved_path),
            },
            ensure_ascii=False,
        )

    obj = parsed_obj
    budgeted = _budget_json_object(
        obj,
        limit,
        saved_path=saved_path,
        spill_root=spill_root,
        tool_name=tool_name,
        field_max_chars=field_limit,
        field_head_chars=field_head_chars,
    )
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
            "content_truncated": True,
            "content_original_chars": original_chars,
            "content_full_saved_path": saved_path,
            "tool_result_truncated": True,
            "output_truncated": True,
            "full_result_saved_path": saved_path,
            "full_result_original_chars": len(raw),
            "visible_excerpt_policy": _truncation_policy(saved_path),
        },
        ensure_ascii=False,
    )
