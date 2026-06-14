from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.core import debug
from app.llm.tools import workspace as ws_tool


MAX_CACHE_CHARS = 80_000
MAX_RETURN_CHARS = 60_000
MAX_ENTRY_CHARS = 24_000

_LOCK = threading.RLock()
_CONTINUED_TRACES: set[str] = set()
_EXPANDED_SCHEMA_TOOLS: dict[str, set[str]] = {}
_SLIM_TOOL_VIEW_CACHE: dict[str, list[dict[str, Any]]] = {}
_SLIM_SINGLE_TOOL_CACHE: dict[str, dict[str, Any]] = {}

_MAX_TOOL_DESCRIPTION_CHARS = 120
_MAX_PROPERTY_DESCRIPTION_CHARS = 70


def _sanitize_model_visible_toolchain_text(value: Any, max_chars: int = 220) -> str:
    text = str(value or "").replace("\n", " ")
    replacements = (
        ("_helpers_shared/", "work material/"),
        ("internal_shared/", "work material/"),
        ("_delegate_", "processing_record_"),
        ("internal_run_", "processing_record_"),
        ("delegate", "processing_records"),
        ("helper reports", "available evidence"),
        ("helper report", "available evidence"),
        ("background_work", "processing_records"),
        ("background work", "processing records"),
        ("helper_producer_self_verified", "output_self_verified"),
        ("producer_self_verified", "output_self_verified"),
        ("main process", "coordinator"),
        ("main thread", "coordinator"),
        ("helpers", "processing records"),
        ("helper", "processing record"),
        ("producer-owned", "generated"),
        ("producer owned", "generated"),
        ("producer evidence", "evidence"),
        ("producers", "processing records"),
        ("producer", "processing record"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    text = re.sub(r"\bbackground_work\b", "processing_records", text, flags=re.IGNORECASE)
    text = re.sub(r"\bbackground\s+(?:tasks?|work|producers?|branches?)\b", "processing records", text, flags=re.IGNORECASE)
    text = re.sub(r"\bhelpers\b", "processing records", text, flags=re.IGNORECASE)
    text = re.sub(r"\bhelper\b", "processing record", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:helper|producer)[-_ ]owned\b", "generated", text, flags=re.IGNORECASE)
    text = re.sub(r"\bproducer\s+evidence\b", "evidence", text, flags=re.IGNORECASE)
    text = re.sub(r"\bmain\s+process\b", "coordinator", text, flags=re.IGNORECASE)
    text = re.sub(r"\bmain\s+thread\b", "coordinator", text, flags=re.IGNORECASE)
    text = re.sub(r"\bproducers\b", "processing records", text, flags=re.IGNORECASE)
    text = re.sub(r"\bproducer\b", "processing record", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    keep_suffixes: list[str] = []
    if "output_self_verified" in text:
        keep_suffixes.append("output_self_verified")
    suffix = ""
    if keep_suffixes:
        suffix = " " + " ".join(dict.fromkeys(keep_suffixes))
    if suffix and len(suffix) < max_chars:
        return text[: max_chars - len(suffix)].rstrip() + suffix
    return text[:max_chars]


def _safe_segment(value: str) -> str:
    value = str(value or "default").strip() or "default"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:120]


def _cache_dir() -> Path:
    root = ws_tool._get_workspace_root()
    path = root / ".toolchain_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(archive_id: str, group_id: str, user_id: str) -> Path:
    name = "__".join(_safe_segment(x) for x in (archive_id, group_id, user_id))
    return _cache_dir() / f"{name}.json"


def _read_cache(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "entries": []}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"version": 1, "entries": []}
        entries = data.get("entries")
        if not isinstance(entries, list):
            data["entries"] = []
        return data
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "entries": []}


def _write_cache(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _compact_entries(entries: list[dict[str, Any]], max_chars: int = MAX_CACHE_CHARS) -> list[dict[str, Any]]:
    clean: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        if len(text) > MAX_ENTRY_CHARS:
            text = text[: MAX_ENTRY_CHARS - 220] + "\n\n[entry truncated by prior-work cache]"
        clean.append({
            "trace_id": str(entry.get("trace_id") or "")[:64],
            "created_at": float(entry.get("created_at") or time.time()),
            "source": str(entry.get("source") or "round2")[:40],
            "text": text,
        })

    total = sum(len(e["text"]) for e in clean)
    if total <= max_chars:
        return clean

    kept: list[dict[str, Any]] = []
    budget = max_chars
    for entry in reversed(clean):
        size = len(entry["text"])
        if size <= budget or not kept:
            kept.append(entry)
            budget -= min(size, budget)
        else:
            break
    kept.reverse()

    dropped = max(0, len(clean) - len(kept))
    if dropped:
        folded = {
            "trace_id": "compressed",
            "created_at": time.time(),
            "source": "cache_compaction",
            "text": (
                f"[prior work cache compaction]\n"
                f"Older {dropped} cached prior-work entr{'y' if dropped == 1 else 'ies'} "
                f"were omitted to keep the continuation context under {max_chars} chars. "
                "Use the retained recent entries as the authoritative continuation context."
            ),
        }
        kept.insert(0, folded)
    return kept


def _tool_call_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    items = message.get("tool_calls") or []
    return [item for item in items if isinstance(item, dict)]


def summarize_messages(
    messages: list[dict[str, Any]],
    *,
    user_message: str = "",
    max_chars: int = MAX_ENTRY_CHARS,
    trace_id: str = "",
) -> str:
    pending: dict[str, tuple[str, str]] = {}
    skip_ids: set[str] = set()
    lines: list[str] = []
    retained_any = False

    if user_message:
        lines.append("[current user request]")
        lines.append(str(user_message).strip()[:1200])
        lines.append("")
    lines.append("[current prior-work summary]")

    step = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            tool_calls = _tool_call_items(message)
            if not tool_calls:
                plan_summary = _summarize_assistant_plan_content(message.get("content"))
                if plan_summary:
                    retained_any = True
                    lines.append(plan_summary)
            for tc in tool_calls:
                tcid = str(tc.get("id") or "")
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                name = str(fn.get("name") or "?")
                args_raw = fn.get("arguments") or "{}"
                if name == "continue_toolchain":
                    if tcid:
                        skip_ids.add(tcid)
                    continue
                if isinstance(args_raw, str):
                    args_preview = args_raw.replace("\n", " ")[:260]
                else:
                    args_preview = str(args_raw).replace("\n", " ")[:260]
                if tcid:
                    pending[tcid] = (name, args_preview)
                step += 1
                retained_any = True
                display_name = "processing_records" if name == "delegate" else name
                lines.append(
                    f"{step}. call {display_name}: "
                    f"{_sanitize_model_visible_toolchain_text(args_preview, 260)}"
                )
        elif role == "tool":
            tcid = str(message.get("tool_call_id") or "")
            if tcid in skip_ids:
                continue
            name, _args = pending.pop(tcid, ("?", ""))
            content = message.get("content") or ""
            result_summary = _summarize_tool_result(content)
            display_name = "processing_records" if name == "delegate" else name
            lines.append(f"   -> {display_name}: {result_summary}")

    if step == 0 and not retained_any:
        lines.append("(no retained tool calls)")

    state_summary = _summarize_agent_state(trace_id)
    if state_summary:
        lines.append("")
        lines.append("[structured agent state]")
        lines.extend(state_summary)

    text = "\n".join(lines).strip()
    if len(text) > max_chars:
        return text[: max_chars - 180] + "\n\n[current prior-work summary truncated]"
    return text


def _summarize_assistant_plan_content(content: Any) -> str:
    raw = str(content or "").strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except Exception:
        return ""
    if not isinstance(parsed, dict):
        return ""

    bits: list[str] = []
    intent = parsed.get("intent")
    if intent:
        bits.append(f"intent={_sanitize_model_visible_toolchain_text(intent, 220)}")
    deliverables = parsed.get("deliverables")
    if isinstance(deliverables, list):
        clean = [
            _sanitize_model_visible_toolchain_text(item, 160)
            for item in deliverables[:12]
            if str(item or "").strip()
        ]
        if clean:
            bits.append("deliverables=" + json.dumps(clean, ensure_ascii=False))
    key_points = parsed.get("key_points")
    if isinstance(key_points, list):
        clean_points = [
            _sanitize_model_visible_toolchain_text(item, 180)
            for item in key_points[:6]
            if str(item or "").strip()
        ]
        if clean_points:
            bits.append("key_points=" + " | ".join(clean_points))
    if not bits:
        return ""
    return "assistant_plan: " + " | ".join(bits)


def _summarize_agent_state(trace_id: str) -> list[str]:
    if not trace_id:
        return []
    try:
        from app.core import agent_state

        status = agent_state.structured_status(trace_id)
    except Exception:
        return []
    lines: list[str] = []
    contracts = status.get("contracts") or []
    if contracts:
        parts = []
        for item in contracts[-5:]:
            goal = str(item.get("goal") or "").replace("\n", " ")[:180]
            stage = str(item.get("current_stage") or "")
            parts.append(f"{item.get('task_id') or '?'}:{stage}:{goal}")
        lines.append("contracts=" + _sanitize_model_visible_toolchain_text(" | ".join(parts), 900))
    evidence = status.get("verified_evidence_recent") or []
    if evidence:
        parts = []
        for item in evidence[-8:]:
            summary = str(item.get("summary") or "").replace("\n", " ")[:160]
            parts.append(f"{item.get('task_id') or item.get('source') or '?'}:{summary}")
        lines.append("verified_evidence=" + _sanitize_model_visible_toolchain_text(" | ".join(parts), 900))
    artifacts = status.get("artifacts_ready") or []
    if artifacts:
        parts = []
        for item in artifacts[-12:]:
            parts.append(f"{item.get('path') or '?'}({item.get('type') or 'file'})")
        lines.append("ready_artifacts=" + _sanitize_model_visible_toolchain_text(", ".join(parts), 900))
    blocked = status.get("blocked_work") or status.get("blocked_helpers") or []
    if blocked:
        parts = []
        for item in blocked[-8:]:
            needed = ",".join(str(x) for x in (item.get("needed_outputs") or [])) or "unspecified"
            parts.append(f"{item.get('blocked_task_id') or '?'}->{item.get('requested_kind') or '?'}:{needed}")
        lines.append("blocked_work=" + _sanitize_model_visible_toolchain_text(" | ".join(parts), 900))
    ready = status.get("ready_to_resume_work") or status.get("ready_to_resume_helpers") or []
    if ready:
        parts = []
        for item in ready[-8:]:
            paths = ",".join(str(x) for x in (item.get("satisfied_by") or [])) or "ready"
            parts.append(f"{item.get('blocked_task_id') or '?'}:{paths}")
        lines.append("ready_to_resume_work=" + _sanitize_model_visible_toolchain_text(" | ".join(parts), 900))
    return lines


def _summarize_tool_result(content: Any) -> str:
    raw = content if isinstance(content, str) else str(content)
    try:
        parsed = json.loads(raw)
    except Exception:
        return raw.replace("\n", " ")[:500]
    if not isinstance(parsed, dict):
        return str(parsed).replace("\n", " ")[:500]
    parts: list[str] = []
    if "ok" in parsed:
        parts.append(f"ok={parsed.get('ok')}")
    if parsed.get("error"):
        parts.append(f"error={str(parsed.get('error'))[:220]}")

    source_attr = parsed.get("source_attribution")
    if isinstance(source_attr, dict):
        attr_bits: list[str] = []
        for key in (
            "scope",
            "filename",
            "uploader_name",
            "uploader_id",
            "current_user_relation",
        ):
            val = source_attr.get(key)
            if val not in (None, ""):
                attr_bits.append(f"{key}={str(val).replace(chr(10), ' ')[:80]}")
        if source_attr.get("current_user_match") is not None:
            attr_bits.append(f"current_user_match={source_attr.get('current_user_match')}")
        if attr_bits:
            parts.append("source_attribution=[" + "; ".join(attr_bits) + "]")

    for key in ("action", "summary", "note", "path", "stdout", "test_summary"):
        val = parsed.get(key)
        if val:
            parts.append(f"{key}={_sanitize_model_visible_toolchain_text(val, 220)}")
    if parsed.get("results") and isinstance(parsed["results"], list):
        work_bits: list[str] = []
        for item in parsed["results"][:6]:
            if isinstance(item, dict):
                tid = item.get("task_id") or "?"
                status = item.get("status") or item.get("terminal_reason") or "?"
                report = _sanitize_model_visible_toolchain_text(
                    item.get("report") or item.get("summary") or "",
                    120,
                )
                work_bits.append(f"{tid}:{status}:{report}")
        if work_bits:
            parts.append("processing_records=[" + "; ".join(work_bits) + "]")
    if not parts:
        parts.append(raw.replace("\n", " ")[:500])
    return " | ".join(parts)[:900]


def append_round(
    *,
    archive_id: str,
    group_id: str,
    user_id: str,
    trace_id: str,
    messages: list[dict[str, Any]],
    user_message: str = "",
) -> dict[str, Any]:
    text = summarize_messages(messages, user_message=user_message, trace_id=trace_id)
    path = _cache_path(archive_id, group_id, user_id)
    with _LOCK:
        data = _read_cache(path)
        entries = list(data.get("entries") or [])
        entries.append({
            "trace_id": trace_id,
            "created_at": time.time(),
            "source": "round2",
            "text": text,
        })
        data["version"] = 1
        data["entries"] = _compact_entries(entries)
        _write_cache(path, data)
        size = sum(len(str(e.get("text") or "")) for e in data["entries"])
    debug.log(
        "toolchain_cache.append",
        f"entries={len(data['entries'])} chars={size}",
        {"path": str(path), "trace_id": trace_id},
    )
    return {"entries": len(data["entries"]), "chars": size, "path": str(path)}


def continue_chain(
    *,
    archive_id: str,
    group_id: str,
    user_id: str,
    trace_id: str,
    reason: str = "",
    max_chars: int | None = None,
) -> dict[str, Any]:
    max_chars = max(1_000, min(int(max_chars or MAX_RETURN_CHARS), MAX_RETURN_CHARS))
    path = _cache_path(archive_id, group_id, user_id)
    with _LOCK:
        if trace_id in _CONTINUED_TRACES:
            return {
                "ok": False,
                "error": "toolchain_already_continued_this_round",
                "hint": (
                    "The previous toolchain cache has already been attached for this round. Continue from the "
                    "attached context and current tool evidence; do not call continue_toolchain again until a new "
                    "round begins.\n\n"
                    "本轮旧工具链已经接入；继续使用当前上下文和工具证据，下一轮再续链。"
                ),
            }
        _CONTINUED_TRACES.add(trace_id)
        data = _read_cache(path)
        entries = _compact_entries(list(data.get("entries") or []))
        text_parts: list[str] = []
        for idx, entry in enumerate(entries, start=1):
            created_at = entry.get("created_at") or 0
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(created_at)))
            text_parts.append(
                f"## cached prior work {idx} ({ts}, trace={entry.get('trace_id') or '?'})\n"
                f"{entry.get('text') or ''}"
            )
        combined = "\n\n".join(text_parts).strip()
        if len(combined) > max_chars:
            combined = combined[-max_chars:]
            combined = "[leading cached prior-work text omitted by max_chars]\n" + combined
        _write_cache(path, {"version": 1, "entries": []})

    debug.log(
        "toolchain_cache.continue",
        f"returned_entries={len(entries)} returned_chars={len(combined)} cleared=1",
        {"path": str(path), "reason": reason[:300], "trace_id": trace_id},
    )
    return {
        "ok": True,
        "action": "continue_toolchain",
        "entries": len(entries),
        "chars": len(combined),
        "cache_cleared": True,
        "reason": reason[:500],
        "continued_toolchain_prefix": combined or "(no cached prior work was available)",
        "next_step": (
            "Treat continued_toolchain_prefix as prior tool evidence. "
            "Use it as the starting point, then continue the current task. "
            "This tool is no longer available in this round.\n\n"
            "续链内容作为既有工具证据；本轮后续不能再次调用续链工具。"
        ),
    }


def trace_has_continued(trace_id: str | None = None) -> bool:
    trace_id = trace_id or debug.current_trace_id()
    return bool(trace_id and trace_id in _CONTINUED_TRACES)


def reset_trace(trace_id: str | None = None) -> None:
    trace_id = trace_id or debug.current_trace_id()
    if not trace_id:
        return
    with _LOCK:
        _CONTINUED_TRACES.discard(trace_id)
        _EXPANDED_SCHEMA_TOOLS.pop(trace_id, None)


def mark_tool_schema_retry(tool_name: str, reason: str = "", trace_id: str | None = None) -> None:
    """Expand one tool schema on the next LLM turn after an argument/schema error."""
    name = str(tool_name or "").strip()
    trace_id = trace_id or debug.current_trace_id()
    if not name or not trace_id:
        return
    with _LOCK:
        expanded = _EXPANDED_SCHEMA_TOOLS.setdefault(trace_id, set())
        expanded.add(name)
    debug.log(
        "toolchain_cache.schema_expand",
        f"expanded full schema for {name!r} after tool error",
        {"trace_id": trace_id, "reason": str(reason or "")[:300]},
    )


def mark_tool_schema_success(tool_name: str, trace_id: str | None = None) -> None:
    """Remove transient full-schema expansion after a successful call."""
    clear_tool_schema_retry(tool_name, trace_id, reason="successful call")


def clear_tool_schema_retry(
    tool_name: str,
    trace_id: str | None = None,
    *,
    reason: str = "",
) -> None:
    """Remove transient full-schema expansion for a tool."""
    name = str(tool_name or "").strip()
    trace_id = trace_id or debug.current_trace_id()
    if not name or not trace_id:
        return
    with _LOCK:
        expanded = _EXPANDED_SCHEMA_TOOLS.get(trace_id)
        if not expanded or name not in expanded:
            return
        expanded.discard(name)
        if not expanded:
            _EXPANDED_SCHEMA_TOOLS.pop(trace_id, None)
    debug.log(
        "toolchain_cache.schema_expand_cleared",
        f"cleared full schema expansion for {name!r}",
        {"trace_id": trace_id, "reason": str(reason or "")[:200]},
    )


def expanded_schema_tools(trace_id: str | None = None) -> set[str]:
    trace_id = trace_id or debug.current_trace_id()
    if not trace_id:
        return set()
    with _LOCK:
        return set(_EXPANDED_SCHEMA_TOOLS.get(trace_id, set()))


def _compact_description(text: Any, limit: int) -> str:
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return ""
    first_sentence = re.split(r"(?<=[.!?。！？])\s+", raw, maxsplit=1)[0].strip()
    compact = first_sentence or raw
    if len(compact) > limit:
        compact = compact[: max(0, limit - 1)].rstrip() + "…"
    return compact


def _slim_parameters_schema(schema: Any) -> Any:
    """Preserve callable shape while trimming model-visible prose."""
    if not isinstance(schema, dict):
        return schema
    cloned = deepcopy(schema)

    def _walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if "description" in node:
            desc = _compact_description(node.get("description"), _MAX_PROPERTY_DESCRIPTION_CHARS)
            if desc:
                node["description"] = desc
            else:
                node.pop("description", None)
        for key in ("properties", "$defs", "definitions"):
            children = node.get(key)
            if isinstance(children, dict):
                for child in children.values():
                    _walk(child)
        for key in ("items", "additionalProperties"):
            _walk(node.get(key))
        for key in ("anyOf", "oneOf", "allOf"):
            variants = node.get(key)
            if isinstance(variants, list):
                for child in variants:
                    _walk(child)

    _walk(cloned)
    return cloned


def _slim_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    cloned = deepcopy(tool)
    fn = cloned.get("function")
    if not isinstance(fn, dict):
        return cloned
    name = str(fn.get("name") or "tool")
    desc = _compact_description(fn.get("description"), _MAX_TOOL_DESCRIPTION_CHARS)
    fn["description"] = desc or f"{name}: available tool. Use its parameters exactly."
    if "parameters" in fn:
        fn["parameters"] = _slim_parameters_schema(fn.get("parameters"))
    return cloned


def _tool_name(tool: dict[str, Any]) -> str:
    fn = tool.get("function") if isinstance(tool, dict) else None
    return str((fn or {}).get("name") or "")


def _tools_content_cache_key(tools: list[dict[str, Any]]) -> str:
    try:
        payload = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        payload = repr(tools)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def _tool_content_cache_key(tool: dict[str, Any]) -> str:
    try:
        payload = json.dumps(tool, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        payload = repr(tool)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def tool_schema_retry_guidance(
    tools: list[dict[str, Any]],
    trace_id: str | None = None,
) -> str:
    """Build a transient full-schema hint without changing the tools array.

    This text is meant to be appended only to the single LLM request that needs
    it, not stored in the durable message history. That keeps the prefix-critical
    `system_static + tool_schema` cache segments stable while still giving the
    model full local facts for a retry.

    仅作为单次动态尾部提示注入；不改变 tools schema 前缀。
    """
    expanded = expanded_schema_tools(trace_id)
    if not expanded or not tools:
        return ""
    selected: list[dict[str, Any]] = []
    for tool in tools:
        if _tool_name(tool) in expanded:
            selected.append(tool)
    if not selected:
        return ""
    payload = {
        "reason": "Previous call(s) for these tools had argument/schema-shaped errors.",
        "scope": "Use these full schemas only to repair the next call. The normal tool list is unchanged.",
        "tools": selected,
    }
    return (
        "## Tool Schema Retry Facts\n"
        "The previous tool result for the listed tool(s) reported an argument, JSON, validation, action, or schema-shaped error. "
        "For this retry only, here are the full schemas for those tool(s). Use them as factual reference for the next tool call; "
        "the callable tool set and normal compact schema remain unchanged.\n\n"
        "工具 schema 临时事实：仅用于修正下一次调用；正常工具列表仍保持紧凑稳定。\n\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def filter_tools_for_trace(tools: list[dict[str, Any]], trace_id: str | None = None) -> list[dict[str, Any]]:
    """Return the stable compact schema view for this trace.

    Default calls keep every tool available but trim long descriptive prose.
    Full-schema retry facts are injected separately through
    tool_schema_retry_guidance(), as a dynamic one-call message overlay, so this
    function does not change the prefix-critical tool schema hash.

    默认保留全部工具能力,只压缩说明文字；参数/必填/枚举结构仍保留。
    """
    if not tools:
        return tools
    key_base = _tools_content_cache_key(tools)
    cached = _SLIM_TOOL_VIEW_CACHE.get(key_base)
    if cached is not None:
        return cached
    slim: list[dict[str, Any]] = []
    for tool in tools:
        tool_key = _tool_content_cache_key(tool)
        item = _SLIM_SINGLE_TOOL_CACHE.get(tool_key)
        if item is None:
            item = _slim_tool_schema(tool)
            _SLIM_SINGLE_TOOL_CACHE[tool_key] = item
        slim.append(item)
    _SLIM_TOOL_VIEW_CACHE[key_base] = slim
    return slim
