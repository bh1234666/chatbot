from __future__ import annotations

import json
from typing import Any

_TASK_MGMT_TOOLS = {"delegate", "spawn_helper", "wait_helper"}


def _tool_call_id(tool_call: Any) -> str | None:
    return tool_call.get("id") if isinstance(tool_call, dict) else getattr(tool_call, "id", None)


def _tool_call_name(tool_call: Any) -> str | None:
    fn = tool_call.get("function") if isinstance(tool_call, dict) else getattr(tool_call, "function", None)
    if not fn:
        return None
    return fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)


def repair_tool_call_pairing(msgs: list[dict], *, debug: Any | None = None) -> int:
    if not msgs:
        return 0

    injections: list[tuple[int, str, str]] = []
    fixed = 0
    for index, message in enumerate(msgs):
        if message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            continue

        immediate_ids: set[str] = set()
        insert_pos = index + 1
        while insert_pos < len(msgs) and msgs[insert_pos].get("role") == "tool":
            tool_call_id = msgs[insert_pos].get("tool_call_id")
            if tool_call_id:
                immediate_ids.add(str(tool_call_id))
            insert_pos += 1

        late_tool_indices: list[int] = []
        orphan_ids = []
        valid_tool_calls = []
        orphan_task_mgmt: list[tuple[str, str]] = []
        for tool_call in tool_calls:
            tool_call_id = _tool_call_id(tool_call)
            if tool_call_id and tool_call_id in immediate_ids:
                valid_tool_calls.append(tool_call)
            elif tool_call_id:
                late_idx = next(
                    (
                        i for i in range(insert_pos, len(msgs))
                        if msgs[i].get("role") == "tool"
                        and str(msgs[i].get("tool_call_id") or "") == str(tool_call_id)
                    ),
                    None,
                )
                if late_idx is not None:
                    late_tool_indices.append(late_idx)
                    valid_tool_calls.append(tool_call)
                else:
                    orphan_ids.append(tool_call_id)
                    tool_name = _tool_call_name(tool_call)
                    if tool_name in _TASK_MGMT_TOOLS:
                        orphan_task_mgmt.append((tool_call_id, tool_name))

        if late_tool_indices:
            moved_messages = [msgs[i] for i in sorted(set(late_tool_indices))]
            for late_idx in sorted(set(late_tool_indices), reverse=True):
                msgs.pop(late_idx)
            for offset, moved in enumerate(moved_messages):
                msgs.insert(insert_pos + offset, moved)
            fixed += 1
            if debug is not None:
                debug.log(
                    "llm.tools.repair_pairing.moved",
                    f"assistant msg at idx {index}: moved {len(moved_messages)} late "
                    "tool result(s) next to their tool_calls",
                )

        if not orphan_ids:
            continue

        if debug is not None:
            debug.log(
                "llm.tools.repair_pairing",
                f"assistant msg at idx {index}: {len(orphan_ids)} orphan tool_call(s) "
                f"({orphan_ids}), {len(valid_tool_calls)} valid",
            )

        for orphan_id, orphan_name in orphan_task_mgmt:
            injections.append((index, orphan_id, orphan_name))
            orphan_ids.remove(orphan_id)
            for tool_call in tool_calls:
                if _tool_call_id(tool_call) == orphan_id:
                    valid_tool_calls.append(tool_call)
                    break

        if not orphan_ids:
            if valid_tool_calls:
                message["tool_calls"] = valid_tool_calls
            continue

        if valid_tool_calls:
            message["tool_calls"] = valid_tool_calls
            fixed += 1
        else:
            content = message.get("content", "")
            if isinstance(content, str) and content.strip():
                message.pop("tool_calls", None)
                fixed += 1
            else:
                message["role"] = "system"
                message["content"] = "[已移除: 孤立的 tool_calls 消息]"
                message.pop("tool_calls", None)
                fixed += 1

    for insert_after_idx, tool_call_id, tool_name in sorted(injections, key=lambda item: -item[0]):
        synthetic_msg = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "_synthetic_repair": True,
            "content": json.dumps(
                {
                    "ok": False,
                    "_synthetic_repair": True,
                    "terminal_reason": "protocol_repair_required",
                    "note": (
                        f"The original result for this {tool_name} call was missing from the current message chain. "
                        "Treat this synthetic result only as a protocol repair marker: inspect the current workspace, "
                        "`_env/...` files, and `.helper_*_full_report.txt` evidence before deciding whether to resume "
                        "the same task_id or continue from existing artifacts.\n\n"
                        "这是协议修复标记；先检查现有文件和 helper 报告，再决定是否同 task_id 续作。"
                    ),
                },
                ensure_ascii=False,
            ),
        }
        msgs.insert(insert_after_idx + 1, synthetic_msg)
        fixed += 1
        if debug is not None:
            debug.log(
                "llm.tools.repair_pairing.synthetic",
                f"injected synthetic tool result for orphan {tool_name} "
                f"(tool_call_id={tool_call_id}) after assistant msg at idx {insert_after_idx}",
            )

    if fixed and debug is not None:
        debug.log(
            "llm.tools.repair_pairing.done",
            f"repaired {fixed} assistant message(s) with orphan tool_calls",
        )
    return fixed
