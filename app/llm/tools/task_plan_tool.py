from __future__ import annotations

import json
from typing import Any

from app.core import debug


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    item = str(value).strip()
    return [item] if item else []


def _thread_snapshot() -> dict[str, Any]:
    try:
        from app.core.core_processes import get_current_thread_context
        ctx = get_current_thread_context()
    except Exception:
        ctx = None
    if ctx is None:
        return {
            "user_message": "",
            "plan_intent": "",
            "plan_key_points": [],
            "plan_deliverables": [],
            "role_label": "",
        }
    return {
        "user_message": str(getattr(ctx, "user_message", "") or ""),
        "plan_intent": str(getattr(ctx, "plan_intent", "") or ""),
        "plan_key_points": list(getattr(ctx, "plan_key_points", None) or []),
        "plan_deliverables": list(getattr(ctx, "plan_deliverables", None) or []),
        "role_label": str(getattr(ctx, "role_label", "") or ""),
    }


async def handle_task_plan(args: dict) -> str:
    """Maintain the active task snapshot visible to the main process."""
    from app.core import agent_state
    from app.core.core_processes import update_thread_plan

    args = args or {}
    trace_id = debug.current_trace_id() or str(args.get("trace_id") or "default")
    action = str(args.get("action") or "status").strip().lower()

    if action == "status":
        return json.dumps(
            {
                "ok": True,
                "thread_plan": _thread_snapshot(),
                "agent_state": agent_state.structured_status(trace_id),
            },
            ensure_ascii=False,
        )

    if action != "update":
        return json.dumps(
            {"ok": False, "error": f"unknown task_plan action: {action}", "valid_actions": ["status", "update"]},
            ensure_ascii=False,
        )

    current = _thread_snapshot()
    goal = str(args.get("goal") or "").strip()
    key_points = _string_list(args.get("key_points"))
    deliverables = _string_list(args.get("deliverables"))
    current_stage = str(args.get("current_stage") or "").strip()
    reason = str(args.get("reason") or "").strip()

    if not any([goal, key_points, deliverables, current_stage, reason]):
        return json.dumps(
            {"ok": False, "error": "task_plan update requires at least one active-task fact"},
            ensure_ascii=False,
        )

    await update_thread_plan(
        intent=goal,
        key_points=key_points if key_points else None,
        deliverables=deliverables if deliverables else None,
    )

    effective_goal = goal or current.get("plan_intent") or current.get("user_message") or "current active task"
    acceptance = _string_list(args.get("acceptance"))
    evidence_required = _string_list(args.get("evidence_required"))
    risks = _string_list(args.get("risks"))
    if reason:
        evidence_required = evidence_required or []
        evidence_required.append(f"task_plan update reason: {reason[:500]}")

    contract = agent_state.upsert_task_contract(
        trace_id=trace_id,
        task_id="main",
        goal=effective_goal[:1200],
        acceptance=acceptance or None,
        evidence_required=evidence_required or None,
        deliverables=deliverables or None,
        risks=risks or None,
        current_stage=current_stage or "task_plan_updated",
    )

    debug.log(
        "task_plan.update",
        "active task plan updated by model-visible tool",
        {
            "goal": effective_goal[:300],
            "key_points": key_points[:8],
            "deliverables": deliverables[:8],
            "current_stage": current_stage,
            "reason": reason[:300],
        },
    )

    return json.dumps(
        {
            "ok": True,
            "thread_plan": _thread_snapshot(),
            "contract": contract,
            "agent_state": agent_state.structured_status(trace_id),
        },
        ensure_ascii=False,
    )
