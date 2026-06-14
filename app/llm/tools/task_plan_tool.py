from __future__ import annotations

import json
import re
from typing import Any

from app.core import debug


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    item = str(value).strip()
    return [item] if item else []


def _optional_string_list(args: dict, key: str) -> list[str] | None:
    if key not in args:
        return None
    return _string_list(args.get(key))


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
        "plan_markers": dict(getattr(ctx, "plan_markers", None) or {}),
        "role_label": str(getattr(ctx, "role_label", "") or ""),
    }


def _compact_string_list(value: Any, *, max_items: int = 6, max_chars: int = 220) -> list[str]:
    items = _string_list(value)[:max_items]
    return [item[:max_chars] for item in items]


def _compact_contract_for_tool_result(contract: dict[str, Any]) -> dict[str, Any]:
    """Return current task contract facts without echoing retained-history blocks."""
    if not isinstance(contract, dict):
        return {}
    compact: dict[str, Any] = {}
    for key in ("task_id", "goal", "current_stage"):
        value = contract.get(key)
        if value not in (None, "", [], {}):
            compact[key] = str(value)[:500] if isinstance(value, str) else value
    for key in ("acceptance", "evidence_required", "deliverables", "risks"):
        values = _compact_string_list(contract.get(key), max_items=8, max_chars=260)
        if values:
            compact[key] = values
    retained = contract.get("retained_prior_contract_items")
    if isinstance(retained, dict) and retained:
        compact["retained_prior_counts"] = {
            key: len(value)
            for key, value in retained.items()
            if isinstance(value, list) and value
        }
        compact["retained_prior_samples"] = {
            key: _compact_string_list(value, max_items=3, max_chars=180)
            for key, value in retained.items()
            if isinstance(value, list) and value
        }
    added = contract.get("added_contract_items")
    if isinstance(added, dict) and added:
        compact["added_contract_items"] = {
            key: _compact_string_list(value, max_items=4, max_chars=180)
            for key, value in added.items()
            if isinstance(value, list) and value
        }
    return compact


def _compact_agent_state_status(status: dict[str, Any], *, include_latest_contract: bool = True) -> dict[str, Any]:
    """Return the model-visible task ledger facts without echoing the full ledger."""
    if not isinstance(status, dict):
        return {}

    contracts = status.get("contracts") if isinstance(status.get("contracts"), list) else []
    latest_contract = contracts[-1] if contracts and isinstance(contracts[-1], dict) else {}
    compact_contract = _compact_contract_for_tool_result(latest_contract)

    def _compact_records(key: str, *, limit: int = 5) -> list[dict[str, Any]]:
        records = status.get(key) if isinstance(status.get(key), list) else []
        out: list[dict[str, Any]] = []
        for record in records[-limit:]:
            if not isinstance(record, dict):
                continue
            out.append({
                k: record.get(k)
                for k in ("task_id", "source", "kind", "status", "summary", "path", "state", "request_id")
                if record.get(k) not in (None, [], "")
            })
        return out

    summary = {
        "counts": {
            "contracts": len(contracts),
            "evidence_recent": len(status.get("evidence_recent") or []),
            "artifacts_ready": len(status.get("artifacts_ready") or []),
            "blocked_work": len(status.get("blocked_work") or []),
            "resource_requests": len(status.get("resource_requests") or []),
        },
        "freshness": status.get("freshness") or {},
        "recent_evidence": _compact_records("evidence_recent"),
        "ready_artifacts": _compact_records("artifacts_ready"),
        "blocked_work": _compact_records("blocked_work"),
        "resource_requests": _compact_records("resource_requests"),
    }
    if include_latest_contract and compact_contract:
        summary["latest_contract"] = compact_contract
    return {k: v for k, v in summary.items() if v not in (None, [], {}, "")}


def _evidence_handoff_hint(evidence_required: list[str] | None, acceptance: list[str] | None) -> str | None:
    text = " ".join([*(evidence_required or []), *(acceptance or [])]).lower()
    coding_markers = (
        "source", "code", "pytest", "test", "failing", "failure", "diagnos",
        "compile", "build", "bug", "traceback",
    )
    if not any(marker in text for marker in coding_markers):
        return None
    return (
        "Fact: task_plan evidence_required records final evidence needs, not a main-thread collection requirement. "
        "For coding/debugging, a code helper can satisfy source reading, failure diagnosis, edits, and test iteration through input_files and acceptance_checks; "
        "the main thread can keep path routing, env_diff/env_apply, and acceptance evidence summaries compact.\n\n"
        "事实：task_plan 的 evidence_required 是最终证据需求，不表示必须由主进程收集；代码调试证据可由 code helper 产出。"
    )


_ORDER_INSENSITIVE_RE = re.compile(
    r"\b(?:any order|order (?:does not|doesn't) matter|order may vary|unordered|order-insensitive)\b|"
    r"(?:任意顺序|顺序不(?:影响|重要|敏感)|顺序无关)",
    re.IGNORECASE,
)


def _exact_reference_order_conflict_hint(contract: dict[str, Any], latest_items: list[str]) -> str | None:
    acceptance_items = [str(item or "") for item in (contract.get("acceptance") or [])]
    exact_facts = [
        item for item in acceptance_items
        if "Exact reference file fact:" in item and "line order" in item
    ][:2]
    if not exact_facts:
        return None
    latest_text = "\n".join(str(item or "") for item in latest_items)
    if not _ORDER_INSENSITIVE_RE.search(latest_text):
        return None
    return (
        "Fact: the latest task_plan update contains order-insensitive language, while retained main-task facts include "
        "an exact reference file where line order, delimiters, visible text, and trailing blank lines are acceptance "
        "facts when a verifier compares against that file. Compare these facts before delegating or finalizing; if no "
        "current verifier evidence says order is ignored, preserve the reference-file order in helper instructions and checks.\n\n"
        "事实：最新 task_plan 含“任意顺序/顺序不重要”语义，但已保留精确参考文件事实；若没有验证器证据说明忽略顺序，应在 helper 指令和验收中保留参考文件顺序。"
    )


_BOUNDARY_FACT_RE = re.compile(
    r"\b(?:tight|partial|partly|workaround|fallback|ambiguous|ambiguity|risk|risky|"
    r"not[- ]friendly|non[- ]friendly|not\s+[a-z0-9_-]*friendly|"
    r"[a-z0-9_]*(?:friendly|allowed|safe|supported)\s*[:=]\s*false|"
    r"false\s+in\s+source|source-level\s+false|does not fit|doesn't fit|missing assumption)\b|"
    r"(?:紧|部分符合|变通|不友好|不适合|风险|有风险|不确定|缺失假设)",
    re.IGNORECASE,
)

_SOURCE_ASSUMPTION_FACT_RE = re.compile(
    r"(?:\$\s*\d+(?:\.\d+)?\s*(?:/|per)\s*(?:night|day|hour|item|person)|"
    r"\b\d+\s*(?:night|nights|day|days|hour|hours|items|people|persons)\b|"
    r"\bx\s*\d+\b|\b\d+\s*x\b|"
    r"\bcost_usd\b|\bsource[- ]provided\b|\bsource\s+(?:cost|duration|count|scope)|"
    r"(?:每晚|每人|每项|晚|天|小时|源数据|来源数据|成本|时长|数量|范围))",
    re.IGNORECASE,
)


def _retained_constraint_fact_risks(key_points: list[str]) -> list[str]:
    """Promote fragile source/constraint facts into retained contract risks.

    This does not decide the final answer. It keeps facts that are commonly
    lost by later plan rewrites visible as compare-before-finalize evidence.
    """
    retained: list[str] = []
    seen: set[str] = set()
    for item in key_points or []:
        text = str(item or "").strip()
        if not text:
            continue
        label = ""
        if _BOUNDARY_FACT_RE.search(text):
            label = "Retained constraint boundary fact from task_plan key_points"
        elif _SOURCE_ASSUMPTION_FACT_RE.search(text) and any(
            marker in text.lower()
            for marker in ("budget", "cost", "$", "night", "day", "duration", "count", "scope", "source", "cost_usd")
        ):
            label = "Retained source assumption fact from task_plan key_points"
        if not label:
            continue
        fact = f"{label}: {text[:500]}"
        if fact in seen:
            continue
        seen.add(fact)
        retained.append(fact)
        if len(retained) >= 8:
            break
    return retained


async def handle_task_plan(args: dict) -> str:
    """Maintain the active task snapshot visible to the main process."""
    from app.core import agent_state
    from app.core.core_processes import update_thread_plan

    args = args or {}
    trace_id = debug.current_trace_id() or str(args.get("trace_id") or "default")
    action = str(args.get("action") or "status").strip().lower()

    if action == "status":
        status = agent_state.structured_status(trace_id)
        return json.dumps(
            {
                "ok": True,
                "thread_plan": _thread_snapshot(),
                "agent_state_summary": _compact_agent_state_status(status, include_latest_contract=True),
                "agent_state_full_status_fact": (
                    "Full structured ledger remains available through agent_state(action='status') when the current "
                    "decision needs exact older evidence or resource records."
                ),
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
    acceptance = _optional_string_list(args, "acceptance")
    evidence_required = _optional_string_list(args, "evidence_required")
    risks = _optional_string_list(args, "risks")
    retained_constraint_risks = _retained_constraint_fact_risks(key_points)
    if retained_constraint_risks:
        existing_risks = list(risks or [])
        for item in retained_constraint_risks:
            if item not in existing_risks:
                existing_risks.append(item)
        risks = existing_risks

    if not any([goal, key_points, deliverables, current_stage, reason, acceptance, evidence_required, risks]):
        return json.dumps(
            {"ok": False, "error": "task_plan update requires at least one active-task fact"},
            ensure_ascii=False,
        )

    await update_thread_plan(
        intent=goal,
        key_points=key_points if key_points else None,
        deliverables=deliverables if deliverables else None,
        current_stage=current_stage,
        acceptance=acceptance,
        evidence_required=evidence_required,
        markers={
            "source": "task_plan.update",
            "reason_present": bool(reason),
            "risks_count": len(risks or []),
        },
    )

    effective_goal = goal or current.get("plan_intent") or current.get("user_message") or "current active task"
    if reason:
        evidence_required = list(evidence_required or [])
        evidence_required.append(f"task_plan update reason: {reason[:500]}")

    contract = agent_state.upsert_task_contract(
        trace_id=trace_id,
        task_id="main",
        goal=effective_goal[:1200],
        acceptance=acceptance,
        evidence_required=evidence_required,
        deliverables=deliverables if "deliverables" in args else None,
        risks=risks,
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

    status = agent_state.structured_status(trace_id)
    result = {
        "ok": True,
        "thread_plan": _thread_snapshot(),
        "contract": _compact_contract_for_tool_result(contract),
        "contract_update_fact": (
            "Existing acceptance/evidence/deliverable facts for the same task_id are retained in agent_state "
            "when omitted or not repeated by this update. If retained_prior_contract_items is present, compare "
            "those prior facts with the revised plan before finalizing or delegating."
        ),
        "agent_state_summary": _compact_agent_state_status(status, include_latest_contract=False),
        "agent_state_full_status_fact": (
            "Full structured ledger remains available through agent_state(action='status') when the current "
            "decision needs exact older evidence or resource records."
        ),
    }
    if retained_constraint_risks:
        result["retained_constraint_boundary_facts"] = retained_constraint_risks[:6]
        result["retained_constraint_boundary_policy"] = (
            "These are prior source or constraint facts retained for comparison. They do not decide the answer; "
            "compare them against the latest evidence before finalizing a feasibility, budget, risk, or requirement-fit reply.\n"
            "这些是保留的源数据/约束事实；不替模型决策，终稿前需与最新证据比较。"
        )
    conflict_hint = _exact_reference_order_conflict_hint(
        contract,
        [
            *key_points,
            *(deliverables or []),
            *(acceptance or []),
            *(evidence_required or []),
            current_stage,
            reason,
        ],
    )
    if conflict_hint:
        result["contract_conflict_facts"] = [conflict_hint]
    hint = _evidence_handoff_hint(evidence_required, acceptance)
    if conflict_hint:
        hint = (conflict_hint + ("\n\n" + hint if hint else ""))
    if hint:
        result["next_action_instruction"] = hint
    return json.dumps(result, ensure_ascii=False)
