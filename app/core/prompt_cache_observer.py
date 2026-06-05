"""Prompt prefix-cache diagnostics.

This module only observes request structure. It must not normalize or mutate
the messages/tools sent to the model.

缓存观测只做结构哈希和字节估算，不改变模型实际输入。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _stable_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)


def _leading_system_count(messages: list[dict[str, Any]]) -> int:
    count = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            break
        count += 1
    return count


def _leading_system_text(messages: list[dict[str, Any]]) -> str:
    count = _leading_system_count(messages)
    if count <= 0:
        return ""
    return "\n\n--- system message boundary ---\n\n".join(
        _message_text(message) for message in messages[:count]
    )


def _split_system_static_dynamic(system_text: str) -> tuple[str, str]:
    """Split known dynamic system sections from the stable prefix."""
    dynamic_headers = (
        "\n\n## Shared Long-Term Memory",
        "\n\n## Current Speaker Long-Term Memory",
        "\n\n## Shared Knowledge Base",
        "\n\n## Shared Warm Memory Index",
        "\n\n## Current Speaker Warm Memory Index",
        "\n\n## Shared Cold Memory Index",
        "\n\n## Shared Files",
        "\n\n## Recent Visual Inputs",
        "\n\n## Unavailable Visual Inputs",
        "\n\n## Other Participants Still Interacting",
        "\n\n## Recent Activity",
        "\n\n## Recent Shared Messages",
        "\n\n## Previous Analysis",
        "\n\n## Current Workspace (.temp) Snapshot",
        "\n\n## Recent execution records",
        "\n\n## Current Time",
    )
    positions = [system_text.find(header) for header in dynamic_headers if system_text.find(header) >= 0]
    if not positions:
        return system_text, ""
    split_at = min(positions)
    return system_text[:split_at], system_text[split_at:]


def _tool_schema_summary(tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for index, tool in enumerate(tools or []):
        function = tool.get("function", {}) if isinstance(tool, dict) else {}
        items.append(
            {
                "index": index,
                "type": tool.get("type") if isinstance(tool, dict) else None,
                "name": function.get("name"),
                "description": function.get("description"),
                "parameters": function.get("parameters"),
            }
        )
    return {"count": len(items), "items": items}


def _messages_summary(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            payload = str(message)
            summary.append(
                {
                    "index": index,
                    "role": "unknown",
                    "bytes": len(payload.encode("utf-8")),
                    "hash": _sha16(payload.encode("utf-8")),
                }
            )
            continue
        text = _message_text(message)
        entry = {
            "index": index,
            "role": str(message.get("role", "")),
            "bytes": len(text.encode("utf-8")),
            "hash": _sha16(text.encode("utf-8")),
        }
        if message.get("name"):
            entry["name"] = str(message.get("name"))
        summary.append(entry)
    return summary


def _section_label(line: str) -> str | None:
    stripped = line.strip()
    if not stripped.startswith("#"):
        return None
    marks = len(stripped) - len(stripped.lstrip("#"))
    if marks < 2:
        return None
    title = stripped[marks:].strip()
    if not title:
        return None
    return f"{'#' * min(marks, 6)} {title[:120]}"


def _section_summary(text: str, *, prefix: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current_label = f"{prefix}:preamble"
    current_lines: list[str] = []

    def flush() -> None:
        segment = "\n".join(current_lines)
        if not segment:
            return
        data = segment.encode("utf-8")
        sections.append(
            {
                "label": current_label,
                "bytes": len(data),
                "hash": _sha16(data),
            }
        )

    for line in text.splitlines():
        label = _section_label(line)
        if label is not None:
            flush()
            current_label = f"{prefix}:{label}"
            current_lines = [line]
        else:
            current_lines.append(line)
    flush()
    return sections


def _message_section_summaries(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    leading_system_count = _leading_system_count(messages)
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", ""))
        if role == "system" and index < leading_system_count:
            continue
        for section in _section_summary(_message_text(message), prefix=f"msg{index}.{role}"):
            rows.append(section)
    return rows


def _hash_chain(segments: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    current = b""
    for label, data in segments:
        current = hashlib.sha256(current + data).digest()
        chain.append(
            {
                "label": label,
                "bytes": len(data),
                "hash": current.hex()[:16],
                "segment_hash": _sha16(data),
            }
        )
    return chain


def describe_prompt_cache_input(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return deterministic prompt-cache diagnostics for one LLM request."""
    leading_system_count = _leading_system_count(messages)
    system_text = _leading_system_text(messages)
    system_static, system_dynamic = _split_system_static_dynamic(system_text)
    tool_summary = _tool_schema_summary(tools)
    tool_bytes = _stable_json_bytes(tool_summary)
    messages_meta = _messages_summary(messages)
    messages_bytes = _stable_json_bytes(messages)

    dynamic_message_bytes = 0
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "system" or index >= leading_system_count:
            dynamic_message_bytes += len(_message_text(message).encode("utf-8"))

    system_static_bytes = len(system_static.encode("utf-8"))
    system_dynamic_bytes = len(system_dynamic.encode("utf-8"))
    cacheable_prefix_bytes = system_static_bytes + len(tool_bytes)

    return {
        "system_prompt_hash": _sha16(system_text.encode("utf-8")),
        "system_static_hash": _sha16(system_static.encode("utf-8")),
        "system_dynamic_hash": _sha16(system_dynamic.encode("utf-8")),
        "tool_schema_hash": _sha16(tool_bytes),
        "messages_hash": _sha16(messages_bytes),
        "prompt_static_bytes": system_static_bytes + len(tool_bytes),
        "prompt_dynamic_bytes": system_dynamic_bytes + dynamic_message_bytes,
        "system_static_bytes": system_static_bytes,
        "system_dynamic_bytes": system_dynamic_bytes,
        "tool_schema_bytes": len(tool_bytes),
        "cacheable_prefix_bytes": cacheable_prefix_bytes,
        "message_count": len(messages),
        "leading_system_count": leading_system_count,
        "tool_count": tool_summary["count"],
        "messages": messages_meta,
        "system_sections": _section_summary(system_text, prefix="system"),
        "message_sections": _message_section_summaries(messages),
        "hash_chain": _hash_chain(
            [
                ("system_static", system_static.encode("utf-8")),
                ("tool_schema", tool_bytes),
                ("system_dynamic", system_dynamic.encode("utf-8")),
                ("messages", messages_bytes),
            ]
        ),
    }
