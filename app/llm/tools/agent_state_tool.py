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


async def handle_agent_state(args: dict) -> str:
    """Main-thread ledger operations for contracts, evidence, artifacts, and resources."""
    from app.core import agent_state

    args = args or {}
    trace_id = debug.current_trace_id() or str(args.get("trace_id") or "default")
    action = str(args.get("action") or "status").strip().lower()

    if action == "status":
        return json.dumps({"ok": True, **agent_state.structured_status(trace_id)}, ensure_ascii=False)

    if action == "upsert_contract":
        task_id = str(args.get("task_id") or "main").strip()
        goal = str(args.get("goal") or "").strip()
        if not task_id:
            return json.dumps({"ok": False, "error": "upsert_contract requires task_id"}, ensure_ascii=False)
        current_stage = str(args.get("current_stage") or "").strip()
        existing_contract = next(
            (
                record for record in agent_state.structured_status(trace_id).get("contracts", [])
                if str(record.get("task_id") or "") == task_id
            ),
            None,
        )
        if not goal and existing_contract:
            goal = str(existing_contract.get("goal") or "").strip()
        if not goal:
            return json.dumps({"ok": False, "error": "upsert_contract requires task_id and goal"}, ensure_ascii=False)
        record = agent_state.upsert_task_contract(
            trace_id=trace_id,
            task_id=task_id,
            goal=goal,
            acceptance=_string_list(args.get("acceptance")),
            evidence_required=_string_list(args.get("evidence_required")),
            deliverables=_string_list(args.get("deliverables")),
            risks=_string_list(args.get("risks")),
            current_stage=current_stage,
        )
        return json.dumps({"ok": True, "contract": record, "status": agent_state.structured_status(trace_id)}, ensure_ascii=False)

    if action == "add_evidence":
        source = str(args.get("source") or "").strip()
        summary = str(args.get("summary") or "").strip()
        status = str(args.get("status") or agent_state.EVIDENCE_PARTIAL).strip()
        if not source or not summary:
            return json.dumps({"ok": False, "error": "add_evidence requires source and summary"}, ensure_ascii=False)
        record = agent_state.add_evidence(
            trace_id=trace_id,
            source=source,
            status=status,
            summary=summary,
            task_id=str(args.get("task_id") or ""),
            kind=str(args.get("kind") or ""),
            data=args.get("data") if isinstance(args.get("data"), dict) else None,
        )
        return json.dumps({"ok": True, "evidence": record, "status": agent_state.structured_status(trace_id)}, ensure_ascii=False)

    if action == "register_artifact":
        path = str(args.get("path") or "").strip()
        if not path:
            return json.dumps({"ok": False, "error": "register_artifact requires path"}, ensure_ascii=False)
        raw_status = str(args.get("status") or agent_state.ARTIFACT_INTERMEDIATE).strip()
        artifact_status = {
            "ready": agent_state.ARTIFACT_READY,
            "verified": agent_state.ARTIFACT_READY,
            "failed": agent_state.ARTIFACT_FAILED,
            "failed_artifact": agent_state.ARTIFACT_FAILED,
            "partial": agent_state.ARTIFACT_INTERMEDIATE,
            "intermediate": agent_state.ARTIFACT_INTERMEDIATE,
        }.get(raw_status, raw_status)
        record = agent_state.register_artifact(
            trace_id=trace_id,
            path=path,
            artifact_type=str(args.get("artifact_type") or args.get("kind") or "file"),
            created_by=str(args.get("created_by") or args.get("task_id") or "main"),
            status=artifact_status,
            verified_by=str(args.get("verified_by") or ""),
            evidence_ids=_string_list(args.get("evidence_ids")),
            metadata=args.get("metadata") if isinstance(args.get("metadata"), dict) else None,
        )
        return json.dumps({"ok": True, "artifact": record, "status": agent_state.structured_status(trace_id)}, ensure_ascii=False)

    if action == "add_resource_task":
        request_id = str(args.get("request_id") or "").strip()
        task_id = str(args.get("resource_task_id") or args.get("task_id") or "").strip()
        if not request_id or not task_id:
            return json.dumps({"ok": False, "error": "add_resource_task requires request_id and resource_task_id"}, ensure_ascii=False)
        record = agent_state.add_resource_task(trace_id, request_id, task_id)
        return json.dumps({"ok": bool(record), "resource_request": record, "status": agent_state.structured_status(trace_id)}, ensure_ascii=False)

    if action == "update_resource_request":
        request_id = str(args.get("request_id") or "").strip()
        state = str(args.get("status") or "").strip()
        if not request_id or not state:
            return json.dumps({"ok": False, "error": "update_resource_request requires request_id and status"}, ensure_ascii=False)
        record = agent_state.update_resource_request(
            trace_id=trace_id,
            request_id=request_id,
            state=state,
            reason=str(args.get("reason") or ""),
            satisfied_by=_string_list(args.get("satisfied_by")),
        )
        return json.dumps({"ok": bool(record), "resource_request": record, "status": agent_state.structured_status(trace_id)}, ensure_ascii=False)

    return json.dumps({
        "ok": False,
        "error": f"unknown agent_state action: {action}",
        "valid_actions": [
            "status",
            "upsert_contract",
            "add_evidence",
            "register_artifact",
            "add_resource_task",
            "update_resource_request",
        ],
    }, ensure_ascii=False)
