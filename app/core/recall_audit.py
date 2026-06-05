from __future__ import annotations

import re
from typing import Any


def recall_audit_recall_used(
    plan: Any, base_msgs: list[dict], *, debug: Any, trace_id: str = "?"
) -> None:
    sys_text = ""
    for msg in base_msgs:
        if msg.get("role") == "system":
            sys_text += str(msg.get("content") or "")
    if not sys_text:
        return

    plan_text = (plan.intent or "") + " " + " ".join(plan.key_points or [])
    if not plan_text.strip():
        debug.log("recall_audit.empty_plan", "plan no text to check")
        return

    entities = set()
    for match in re.finditer(r'[一-鿿]{2,15}', sys_text):
        entities.add(match.group(0))
    for match in re.finditer(r'[A-Za-z][\w.\-]{3,29}', sys_text):
        entities.add(match.group(0))

    noise = {
        "system", "User", "user", "content", "role", "history", "memory",
        "warm_user_index", "cold_group_topk", "群组文件", "群组知识库",
        "温记忆", "冷记忆", "user_facts", "topk", "今天", "昨天", "应该",
        "是的", "不是", "可以", "不可以", "需要", "没有", "可能", "或者",
        "那个", "这个", "什么", "哪个",
    }
    entities -= noise

    matched = []
    for entity in entities:
        if entity in plan_text:
            matched.append(entity)
            if len(matched) >= 5:
                break

    if matched:
        debug.log(
            "recall_audit.used",
            f"plan referenced {len(matched)} entity(ies) from system: {matched[:3]}",
        )
        return

    debug.log(
        "recall_audit.unused",
        f"⚠️ needs_recall=true but plan references NO system entities; "
        f"checked {min(len(entities), 200)} entities",
    )
