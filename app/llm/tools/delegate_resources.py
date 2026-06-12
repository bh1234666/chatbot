"""Helper resource request parsing and prompt construction."""
from __future__ import annotations

import json


def _parse_main_resource_request(tool_result: str | dict | None) -> dict | None:
    """Return structured resource request from helper-only request_resource."""
    if tool_result is None:
        return None
    if isinstance(tool_result, dict):
        data = tool_result
    else:
        try:
            data = json.loads(str(tool_result))
        except Exception:
            return None
    if (
        not isinstance(data, dict)
        or not data.get("requires_main_resource")
        or data.get("action") != "request_resource"
    ):
        return None
    resource_kind = str(
        data.get("resource_kind")
        or data.get("matching_helper_kind")
        or data.get("suggested_helper_kind")
        or "code"
    )
    return {
        "requires_main_resource": True,
        "resource_kind": resource_kind,
        "matching_helper_kind": str(data.get("matching_helper_kind") or resource_kind),
        "suggested_helper_kind": str(data.get("suggested_helper_kind") or resource_kind),
        "blocked_reason": data.get("blocked_reason"),
        "blocked_path": data.get("blocked_path"),
        "blocked_kind": data.get("blocked_kind"),
        "resource_resolution_facts": data.get("resource_resolution_facts"),
        "observed_recovery_options": data.get("observed_recovery_options") or [],
        "main_thread_action": data.get("main_thread_action"),
        "needed_outputs": data.get("needed_outputs") or [],
        "resume_instruction": data.get("resume_instruction"),
        "error": data.get("error"),
    }


def _resource_task_prompt(
    *,
    blocked_task_id: str,
    blocked_kind: str,
    resource_request: dict,
) -> str:
    kind = str(
        resource_request.get("matching_helper_kind")
        or resource_request.get("resource_kind")
        or resource_request.get("suggested_helper_kind")
        or "code"
    )
    reason = str(resource_request.get("blocked_reason") or resource_request.get("error") or "resource missing").strip()
    needed_outputs = [
        str(x).strip()
        for x in (resource_request.get("needed_outputs") or [])
        if str(x).strip()
    ][:20]
    needed_text = ", ".join(needed_outputs) if needed_outputs else "(no explicit filenames were provided; produce clear reusable files or evidence)"
    from app.llm import aux_prompts as _aux
    return _aux.RESOURCE_HELPER_DISPATCH_TEMPLATE.format(
        blocked_task_id=blocked_task_id,
        blocked_kind=blocked_kind,
        reason=reason,
        kind=kind,
        needed_text=needed_text,
    )
