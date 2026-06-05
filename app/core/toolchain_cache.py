from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from app.core import debug
from app.llm.tools import workspace as ws_tool


MAX_CACHE_CHARS = 80_000
MAX_RETURN_CHARS = 60_000
MAX_ENTRY_CHARS = 24_000

_LOCK = threading.RLock()
_CONTINUED_TRACES: set[str] = set()


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
            text = text[: MAX_ENTRY_CHARS - 220] + "\n\n[entry truncated by toolchain cache]"
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
                f"[toolchain cache compaction]\n"
                f"Older {dropped} cached toolchain entr{'y' if dropped == 1 else 'ies'} "
                f"were omitted to keep the continuation cache under {max_chars} chars. "
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

    if user_message:
        lines.append("[current user request]")
        lines.append(str(user_message).strip()[:1200])
        lines.append("")
    lines.append("[current toolchain summary]")

    step = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            for tc in _tool_call_items(message):
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
                lines.append(f"{step}. call {name}: {args_preview}")
        elif role == "tool":
            tcid = str(message.get("tool_call_id") or "")
            if tcid in skip_ids:
                continue
            name, _args = pending.pop(tcid, ("?", ""))
            content = message.get("content") or ""
            result_summary = _summarize_tool_result(content)
            lines.append(f"   -> {name}: {result_summary}")

    if step == 0:
        lines.append("(no retained tool calls)")

    state_summary = _summarize_agent_state(trace_id)
    if state_summary:
        lines.append("")
        lines.append("[structured agent state]")
        lines.extend(state_summary)

    text = "\n".join(lines).strip()
    if len(text) > max_chars:
        return text[: max_chars - 180] + "\n\n[current toolchain summary truncated]"
    return text


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
        lines.append("contracts=" + " | ".join(parts))
    evidence = status.get("verified_evidence_recent") or []
    if evidence:
        parts = []
        for item in evidence[-8:]:
            summary = str(item.get("summary") or "").replace("\n", " ")[:160]
            parts.append(f"{item.get('task_id') or item.get('source') or '?'}:{summary}")
        lines.append("verified_evidence=" + " | ".join(parts))
    artifacts = status.get("artifacts_ready") or []
    if artifacts:
        parts = []
        for item in artifacts[-12:]:
            parts.append(f"{item.get('path') or '?'}({item.get('type') or 'file'})")
        lines.append("ready_artifacts=" + ", ".join(parts))
    blocked = status.get("blocked_helpers") or []
    if blocked:
        parts = []
        for item in blocked[-8:]:
            needed = ",".join(str(x) for x in (item.get("needed_outputs") or [])) or "unspecified"
            parts.append(f"{item.get('blocked_task_id') or '?'}->{item.get('requested_kind') or '?'}:{needed}")
        lines.append("blocked_helpers=" + " | ".join(parts))
    ready = status.get("ready_to_resume_helpers") or []
    if ready:
        parts = []
        for item in ready[-8:]:
            paths = ",".join(str(x) for x in (item.get("satisfied_by") or [])) or "ready"
            parts.append(f"{item.get('blocked_task_id') or '?'}:{paths}")
        lines.append("ready_to_resume=" + " | ".join(parts))
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
    for key in ("action", "summary", "note", "path", "stdout", "test_summary"):
        val = parsed.get(key)
        if val:
            parts.append(f"{key}={str(val).replace(chr(10), ' ')[:220]}")
    if parsed.get("results") and isinstance(parsed["results"], list):
        helper_bits: list[str] = []
        for item in parsed["results"][:6]:
            if isinstance(item, dict):
                tid = item.get("task_id") or "?"
                status = item.get("status") or item.get("terminal_reason") or "?"
                report = str(item.get("report") or item.get("summary") or "")[:120]
                helper_bits.append(f"{tid}:{status}:{report}")
        if helper_bits:
            parts.append("helpers=[" + "; ".join(helper_bits) + "]")
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
                f"## cached toolchain {idx} ({ts}, trace={entry.get('trace_id') or '?'})\n"
                f"{entry.get('text') or ''}"
            )
        combined = "\n\n".join(text_parts).strip()
        if len(combined) > max_chars:
            combined = combined[-max_chars:]
            combined = "[leading cached toolchain text omitted by max_chars]\n" + combined
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
        "continued_toolchain_prefix": combined or "(no cached toolchain was available)",
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


def filter_tools_for_trace(tools: list[dict[str, Any]], trace_id: str | None = None) -> list[dict[str, Any]]:
    """Return the stable tool schema list for this trace.

    The continuation tool is single-use, but hiding it after use changes the
    tool-array prefix and hurts provider prefix-cache reuse. The execution path
    in continue_chain remains the authority: repeated calls return a structured
    toolchain_already_continued_this_round error.

    续链工具保持 schema 稳定；重复调用由执行层返回错误。
    """
    return tools
