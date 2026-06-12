"""Shared agent state for contracts, evidence, artifacts, and resource waits.

This module is intentionally small and framework-facing. LLM prompts can decide
what to do, but these ledgers keep task facts, helper blockers, and deliverables
in structured state so later stages do not have to infer them from prose.

保存任务契约、证据、产物和资源等待状态；让主进程按结构化事实决策。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import time
from typing import Any


_CONTRACT_LIMIT = 20
_EVIDENCE_LIMIT = 300
_ARTIFACT_LIMIT = 300
_RESOURCE_LIMIT = 100

EVIDENCE_VERIFIED = "verified"
EVIDENCE_PARTIAL = "partial"
EVIDENCE_FAILED = "failed"
EVIDENCE_STALE = "stale"
EVIDENCE_CONTRADICTED = "contradicted"

ARTIFACT_READY = "ready"
ARTIFACT_FAILED = "failed"
ARTIFACT_INTERMEDIATE = "intermediate"

RESOURCE_WAITING = "waiting_resource"
RESOURCE_READY = "ready_to_resume"
RESOURCE_REFUSED = "refused"
RESOURCE_CLOSED = "closed"
RESOURCE_FAILED = "failed"


@dataclass
class TaskContract:
    trace_id: str
    task_id: str
    goal: str
    acceptance: list[str] = field(default_factory=list)
    evidence_required: list[str] = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    current_stage: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "task_id": self.task_id,
            "goal": self.goal,
            "acceptance": list(self.acceptance),
            "evidence_required": list(self.evidence_required),
            "deliverables": list(self.deliverables),
            "risks": list(self.risks),
            "current_stage": self.current_stage,
            "created_at": round(self.created_at, 3),
            "updated_at": round(self.updated_at, 3),
        }


@dataclass
class EvidenceRecord:
    evidence_id: str
    trace_id: str
    source: str
    status: str
    summary: str
    task_id: str = ""
    kind: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "trace_id": self.trace_id,
            "task_id": self.task_id,
            "source": self.source,
            "kind": self.kind,
            "status": self.status,
            "summary": self.summary,
            "data": self.data,
            "created_at": round(self.created_at, 3),
        }


@dataclass
class ArtifactRecord:
    artifact_id: str
    trace_id: str
    path: str
    artifact_type: str
    created_by: str
    status: str = ARTIFACT_INTERMEDIATE
    verified_by: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "trace_id": self.trace_id,
            "path": self.path,
            "type": self.artifact_type,
            "created_by": self.created_by,
            "verified_by": self.verified_by,
            "status": self.status,
            "evidence_ids": list(self.evidence_ids),
            "metadata": self.metadata,
            "created_at": round(self.created_at, 3),
        }


@dataclass
class ResourceRequest:
    request_id: str
    trace_id: str
    blocked_task_id: str
    blocked_kind: str
    requested_kind: str
    needed_outputs: list[str] = field(default_factory=list)
    reason: str = ""
    wake_condition: dict[str, Any] = field(default_factory=dict)
    resume_instruction: str = ""
    resource_task_ids: list[str] = field(default_factory=list)
    satisfied_by: list[str] = field(default_factory=list)
    state: str = RESOURCE_WAITING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "blocked_task_id": self.blocked_task_id,
            "blocked_kind": self.blocked_kind,
            "requested_kind": self.requested_kind,
            "needed_outputs": list(self.needed_outputs),
            "reason": self.reason,
            "wake_condition": self.wake_condition,
            "resume_instruction": self.resume_instruction,
            "resource_task_ids": list(self.resource_task_ids),
            "satisfied_by": list(self.satisfied_by),
            "state": self.state,
            "created_at": round(self.created_at, 3),
            "updated_at": round(self.updated_at, 3),
        }


_contracts: dict[str, deque[TaskContract]] = {}
_evidence: dict[str, deque[EvidenceRecord]] = {}
_artifacts: dict[str, deque[ArtifactRecord]] = {}
_resources: dict[str, deque[ResourceRequest]] = {}
_id_counter = 0
_published_resource_events: dict[str, set[tuple[str, str]]] = {}


def _merge_unique(existing: list[str], incoming: list[str] | None, *, limit: int = 80) -> tuple[list[str], list[str]]:
    """Append new items without losing earlier task facts."""
    merged: list[str] = []
    seen: set[str] = set()
    for values in (existing or [], incoming or []):
        for source in values:
            item = str(source or "").strip()
            if not item or item in seen:
                continue
            seen.add(item)
            merged.append(item)
            if len(merged) >= limit:
                break
        if len(merged) >= limit:
            break
    added = [item for item in merged if item not in set(existing or [])]
    return merged, added


def _is_retained_prior_note(item: str) -> bool:
    return str(item or "").strip().startswith("Retained prior ")


def _retained_prior_note_prefix(field_name: str) -> str:
    return f"Retained prior {field_name} not repeated in latest update:"


def _new_id(prefix: str) -> str:
    global _id_counter
    _id_counter += 1
    return f"{prefix}_{int(time.time() * 1000):x}_{_id_counter:x}"


def _append_limited(store: dict[str, deque], trace_id: str, item: Any, limit: int) -> None:
    if trace_id not in store:
        store[trace_id] = deque(maxlen=limit)
    store[trace_id].append(item)


def reset_trace(trace_id: str) -> None:
    """Clear all structured state for one trace. Mainly used by tests."""
    _contracts.pop(trace_id, None)
    _evidence.pop(trace_id, None)
    _artifacts.pop(trace_id, None)
    _resources.pop(trace_id, None)
    _published_resource_events.pop(trace_id, None)


def upsert_task_contract(
    *,
    trace_id: str,
    task_id: str,
    goal: str,
    acceptance: list[str] | None = None,
    evidence_required: list[str] | None = None,
    deliverables: list[str] | None = None,
    risks: list[str] | None = None,
    current_stage: str = "",
    merge: bool = True,
) -> dict[str, Any]:
    records = _contracts.setdefault(trace_id, deque(maxlen=_CONTRACT_LIMIT))
    for record in records:
        if record.task_id == task_id:
            record.goal = goal or record.goal
            retained: dict[str, list[str]] = {}
            added: dict[str, list[str]] = {}
            if merge:
                old_acceptance = list(record.acceptance)
                old_evidence = list(record.evidence_required)
                old_deliverables = list(record.deliverables)
                old_risks = [item for item in record.risks if not _is_retained_prior_note(item)]
                record.acceptance, added_acceptance = _merge_unique(record.acceptance, acceptance, limit=100)
                record.evidence_required, added_evidence = _merge_unique(record.evidence_required, evidence_required, limit=120)
                record.deliverables, added_deliverables = _merge_unique(record.deliverables, deliverables, limit=80)
                record.risks, added_risks = _merge_unique(record.risks, risks, limit=80)
                if acceptance is not None:
                    retained_acceptance = [item for item in old_acceptance if item not in set(acceptance or [])]
                    if retained_acceptance:
                        retained["acceptance"] = retained_acceptance
                if evidence_required is not None:
                    retained_evidence = [item for item in old_evidence if item not in set(evidence_required or [])]
                    if retained_evidence:
                        retained["evidence_required"] = retained_evidence
                if deliverables is not None:
                    retained_deliverables = [item for item in old_deliverables if item not in set(deliverables or [])]
                    if retained_deliverables:
                        retained["deliverables"] = retained_deliverables
                if risks is not None:
                    retained_risks = [item for item in old_risks if item not in set(risks or [])]
                    if retained_risks:
                        retained["risks"] = retained_risks
                if added_acceptance:
                    added["acceptance"] = added_acceptance
                if added_evidence:
                    added["evidence_required"] = added_evidence
                if added_deliverables:
                    added["deliverables"] = added_deliverables
                if added_risks:
                    added["risks"] = added_risks
                for field_name, items in retained.items():
                    if field_name == "risks":
                        continue
                    prefix = _retained_prior_note_prefix(field_name)
                    note = (
                        f"{prefix} "
                        + "; ".join(items[:6])
                    )
                    record.risks = [
                        item for item in record.risks
                        if not str(item or "").startswith(prefix)
                    ]
                    record.risks.append(note[:1000])
                    record.risks = record.risks[-80:]
            else:
                record.acceptance = list(acceptance or record.acceptance)
                record.evidence_required = list(evidence_required or record.evidence_required)
                record.deliverables = list(deliverables or record.deliverables)
                record.risks = list(risks or record.risks)
            record.current_stage = current_stage or record.current_stage
            record.updated_at = time.time()
            public = record.to_public_dict()
            if retained:
                public["retained_prior_contract_items"] = retained
            if added:
                public["added_contract_items"] = added
            return public
    record = TaskContract(
        trace_id=trace_id,
        task_id=task_id,
        goal=goal,
        acceptance=list(acceptance or []),
        evidence_required=list(evidence_required or []),
        deliverables=list(deliverables or []),
        risks=list(risks or []),
        current_stage=current_stage,
    )
    records.append(record)
    return record.to_public_dict()


def add_evidence(
    *,
    trace_id: str,
    source: str,
    status: str,
    summary: str,
    task_id: str = "",
    kind: str = "",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in {
        EVIDENCE_VERIFIED,
        EVIDENCE_PARTIAL,
        EVIDENCE_FAILED,
        EVIDENCE_STALE,
        EVIDENCE_CONTRADICTED,
    }:
        status = EVIDENCE_PARTIAL
    record = EvidenceRecord(
        evidence_id=_new_id("ev"),
        trace_id=trace_id,
        source=source,
        status=status,
        summary=summary[:1000],
        task_id=task_id,
        kind=kind,
        data=data or {},
    )
    _append_limited(_evidence, trace_id, record, _EVIDENCE_LIMIT)
    return record.to_public_dict()


def register_artifact(
    *,
    trace_id: str,
    path: str,
    artifact_type: str,
    created_by: str,
    status: str = ARTIFACT_INTERMEDIATE,
    verified_by: str = "",
    evidence_ids: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in {ARTIFACT_READY, ARTIFACT_FAILED, ARTIFACT_INTERMEDIATE}:
        status = ARTIFACT_INTERMEDIATE
    record = ArtifactRecord(
        artifact_id=_new_id("art"),
        trace_id=trace_id,
        path=path,
        artifact_type=artifact_type,
        created_by=created_by,
        status=status,
        verified_by=verified_by,
        evidence_ids=list(evidence_ids or []),
        metadata=metadata or {},
    )
    _append_limited(_artifacts, trace_id, record, _ARTIFACT_LIMIT)
    _match_resource_requests(trace_id)
    if record.status == ARTIFACT_READY:
        _publish_artifact_ready_event(trace_id, record)
    return record.to_public_dict()


def register_helper_result(trace_id: str, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Record a completed helper result as evidence, artifacts, and resource state."""
    terminal = str(result.get("terminal_reason") or "").lower()
    ok = bool(result.get("ok"))
    outputs_check = result.get("outputs_check") if isinstance(result.get("outputs_check"), dict) else {}
    outputs_complete = outputs_check.get("outputs_complete")
    producer_self_verified = outputs_check.get("producer_self_verified") is True
    resource_request = result.get("resource_required") if isinstance(result.get("resource_required"), dict) else None

    if terminal == "resource_required" or resource_request:
        status = EVIDENCE_PARTIAL
    elif ok and producer_self_verified:
        status = EVIDENCE_VERIFIED
    elif ok and outputs_complete is not False:
        status = EVIDENCE_PARTIAL
    else:
        status = EVIDENCE_FAILED

    summary = str(result.get("report") or terminal or ("ok" if ok else "failed"))[:1000]
    evidence = add_evidence(
        trace_id=trace_id,
        source="helper",
        status=status,
        summary=summary,
        task_id=task_id,
        kind=str(result.get("kind") or ""),
        data={
            "terminal_reason": terminal,
            "ok": ok,
            "outputs_complete": outputs_complete,
            "producer_self_verified": producer_self_verified,
            "outputs_missing": outputs_check.get("outputs_missing"),
            "quality_warnings": outputs_check.get("quality_warnings") or [],
        },
    )

    for file_item in result.get("files") or []:
        path = ""
        if isinstance(file_item, str):
            path = file_item
        elif isinstance(file_item, dict):
            path = str(
                file_item.get("rel_path")
                or file_item.get("path")
                or file_item.get("name")
                or file_item.get("local_path")
                or ""
            )
        if not path:
            continue
        artifact_type = _guess_artifact_type(path)
        artifact_status = ARTIFACT_READY if ok and producer_self_verified else ARTIFACT_INTERMEDIATE
        register_artifact(
            trace_id=trace_id,
            path=path,
            artifact_type=artifact_type,
            created_by=task_id,
            status=artifact_status,
            verified_by="helper_producer_self_verified" if producer_self_verified else "",
            evidence_ids=[evidence["evidence_id"]],
            metadata={"terminal_reason": terminal, "producer_self_verified": producer_self_verified},
        )

    if terminal == "resource_required" or resource_request:
        register_resource_request(
            trace_id=trace_id,
            blocked_task_id=task_id,
            blocked_kind=str(result.get("kind") or ""),
            request=resource_request or {},
        )
    return evidence


def register_helper_resource_request(
    *,
    trace_id: str,
    task_id: str,
    helper_kind: str = "",
    request: dict[str, Any] | None = None,
    report: str = "",
) -> dict[str, Any]:
    """Record an in-progress helper freeze before the final helper result exists."""
    request = request or {}
    add_evidence(
        trace_id=trace_id,
        source="helper_resource_request",
        status=EVIDENCE_PARTIAL,
        summary=(report or request.get("blocked_reason") or request.get("reason") or "resource required")[:1000],
        task_id=task_id,
        kind=helper_kind,
        data={
            "terminal_reason": "resource_required",
            "ok": False,
            "resource_kind": request.get("resource_kind") or request.get("suggested_helper_kind") or request.get("kind"),
            "needed_outputs": request.get("needed_outputs") or [],
        },
    )
    return register_resource_request(
        trace_id=trace_id,
        blocked_task_id=task_id,
        blocked_kind=helper_kind,
        request=request,
    )


def register_resource_request(
    *,
    trace_id: str,
    blocked_task_id: str,
    blocked_kind: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    records = _resources.setdefault(trace_id, deque(maxlen=_RESOURCE_LIMIT))
    requested_kind = str(
        request.get("resource_kind")
        or request.get("suggested_helper_kind")
        or request.get("kind")
        or "code"
    )
    needed_outputs = [
        str(x).strip()
        for x in (request.get("needed_outputs") or [])
        if str(x).strip()
    ][:20]
    for record in reversed(records):
        if (
            record.blocked_task_id == blocked_task_id
            and record.state in {RESOURCE_WAITING, RESOURCE_READY}
            and record.needed_outputs == needed_outputs
            and record.requested_kind == requested_kind
        ):
            record.reason = str(request.get("blocked_reason") or request.get("reason") or record.reason)[:500]
            record.resume_instruction = str(request.get("resume_instruction") or record.resume_instruction)[:1000]
            record.updated_at = time.time()
            _match_resource_requests(trace_id)
            return record.to_public_dict()
    record = ResourceRequest(
        request_id=_new_id("res"),
        trace_id=trace_id,
        blocked_task_id=blocked_task_id,
        blocked_kind=blocked_kind,
        requested_kind=requested_kind,
        needed_outputs=needed_outputs,
        reason=str(request.get("blocked_reason") or request.get("reason") or request.get("error") or "")[:500],
        wake_condition={
            "artifact_ready": needed_outputs,
            "resume_instruction": str(request.get("resume_instruction") or "")[:1000],
        },
        resume_instruction=str(request.get("resume_instruction") or "")[:1000],
    )
    records.append(record)
    _match_resource_requests(trace_id)
    _publish_resource_event(trace_id, "helper_blocked", record)
    return record.to_public_dict()


def add_resource_task(trace_id: str, request_id: str, task_id: str) -> dict[str, Any] | None:
    for record in _resources.get(trace_id, ()):
        if record.request_id != request_id:
            continue
        if task_id not in record.resource_task_ids:
            record.resource_task_ids.append(task_id)
        record.updated_at = time.time()
        return record.to_public_dict()
    return None


def update_resource_request(
    *,
    trace_id: str,
    request_id: str,
    state: str,
    reason: str = "",
    satisfied_by: list[str] | None = None,
) -> dict[str, Any] | None:
    """Update a resource wait after the main process decides to refuse or close it."""
    state_aliases = {
        "ready": RESOURCE_READY,
        "ready_to_resume": RESOURCE_READY,
        "waiting": RESOURCE_WAITING,
        "waiting_resource": RESOURCE_WAITING,
        "refused": RESOURCE_REFUSED,
        "closed": RESOURCE_CLOSED,
        "failed": RESOURCE_FAILED,
    }
    state = state_aliases.get(str(state or "").strip().lower(), state)
    if state not in {RESOURCE_WAITING, RESOURCE_READY, RESOURCE_REFUSED, RESOURCE_CLOSED, RESOURCE_FAILED}:
        state = RESOURCE_WAITING
    for record in _resources.get(trace_id, ()):
        if record.request_id != request_id:
            continue
        record.state = state
        if reason:
            record.reason = str(reason)[:500]
        if satisfied_by is not None:
            record.satisfied_by = [str(x).strip() for x in satisfied_by if str(x).strip()][:20]
        record.updated_at = time.time()
        _publish_resource_event(
            trace_id,
            "helper_ready_to_resume" if state == RESOURCE_READY else f"helper_resource_{state}",
            record,
        )
        return record.to_public_dict()
    return None


def list_contracts(trace_id: str) -> list[dict[str, Any]]:
    return [x.to_public_dict() for x in _contracts.get(trace_id, ())]


def list_evidence(trace_id: str, *, status: str | None = None, last_n: int = 20) -> list[dict[str, Any]]:
    items = list(_evidence.get(trace_id, ()))
    if status:
        items = [x for x in items if x.status == status]
    return [x.to_public_dict() for x in items[-last_n:]]


def list_artifacts(trace_id: str, *, status: str | None = None, last_n: int = 50) -> list[dict[str, Any]]:
    items = list(_artifacts.get(trace_id, ()))
    if status:
        items = [x for x in items if x.status == status]
    return [x.to_public_dict() for x in items[-last_n:]]


def list_resource_requests(trace_id: str, *, state: str | None = None) -> list[dict[str, Any]]:
    _match_resource_requests(trace_id)
    items = list(_resources.get(trace_id, ()))
    if state:
        items = [x for x in items if x.state == state]
    return [x.to_public_dict() for x in items]


def ready_to_resume(trace_id: str) -> list[dict[str, Any]]:
    return list_resource_requests(trace_id, state=RESOURCE_READY)


def structured_status(trace_id: str) -> dict[str, Any]:
    contracts = list_contracts(trace_id)
    evidence_recent = list_evidence(trace_id, last_n=20)
    artifacts_recent = list_artifacts(trace_id, last_n=50)
    latest_contract_at = max((float(x.get("updated_at") or 0) for x in contracts), default=0.0)
    latest_evidence_at = max((float(x.get("created_at") or 0) for x in evidence_recent), default=0.0)
    latest_artifact_at = max((float(x.get("created_at") or 0) for x in artifacts_recent), default=0.0)
    latest_fact_at = max(latest_evidence_at, latest_artifact_at)
    return {
        "contracts": contracts,
        "freshness": {
            "latest_contract_updated_at": round(latest_contract_at, 3) if latest_contract_at else None,
            "latest_evidence_at": round(latest_evidence_at, 3) if latest_evidence_at else None,
            "latest_artifact_at": round(latest_artifact_at, 3) if latest_artifact_at else None,
            "latest_fact_after_contract": bool(latest_fact_at and latest_contract_at and latest_fact_at > latest_contract_at),
        },
        "evidence_recent": evidence_recent,
        "verified_evidence_recent": list_evidence(trace_id, status=EVIDENCE_VERIFIED, last_n=20),
        "artifacts_ready": list_artifacts(trace_id, status=ARTIFACT_READY, last_n=50),
        "artifacts_recent": artifacts_recent,
        "resource_requests": list_resource_requests(trace_id),
        "blocked_helpers": list_resource_requests(trace_id, state=RESOURCE_WAITING),
        "ready_to_resume_helpers": ready_to_resume(trace_id),
    }


def _match_resource_requests(trace_id: str) -> None:
    requests = list(_resources.get(trace_id, ()))
    if not requests:
        return
    ready_artifacts = [
        x for x in _artifacts.get(trace_id, ())
        if x.status == ARTIFACT_READY
    ]
    ready_paths = {x.path.replace("\\", "/").lower(): x for x in ready_artifacts}
    ready_basenames = {x.path.replace("\\", "/").split("/")[-1].lower(): x for x in ready_artifacts}
    now = time.time()
    for request in requests:
        if request.state not in {RESOURCE_WAITING, RESOURCE_READY}:
            continue
        needed = [x.replace("\\", "/").lower() for x in request.needed_outputs]
        if not needed:
            continue
        matched: list[str] = []
        for item in needed:
            record = ready_paths.get(item) or ready_basenames.get(item.split("/")[-1])
            if record is None:
                break
            matched.append(record.path)
        else:
            request.satisfied_by = matched
            request.state = RESOURCE_READY
            request.updated_at = now
            _publish_resource_event(trace_id, "helper_ready_to_resume", request)


def _guess_artifact_type(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in {"docx", "md", "txt", "pdf"}:
        return "report"
    if ext in {"png", "jpg", "jpeg", "webp", "svg"}:
        return "chart" if "chart" in path.lower() else "image"
    if ext in {"wav", "mp3", "flac", "m4a"}:
        return "audio"
    if ext in {"csv", "json", "xlsx", "tsv"}:
        return "data"
    if ext in {"py", "js", "ts", "html", "css", "c", "cpp", "h", "rs", "go", "java"}:
        return "code"
    return "file"


def _publish_resource_event(trace_id: str, kind: str, request: ResourceRequest) -> None:
    key = (kind, request.request_id)
    emitted = _published_resource_events.setdefault(trace_id, set())
    if key in emitted:
        return
    emitted.add(key)
    try:
        from app.core.environment_events import publish_workflow_event

        publish_workflow_event({
            "kind": kind,
            "trace_id": trace_id,
            "task_id": request.blocked_task_id,
            "helper_kind": request.blocked_kind,
            "requested_kind": request.requested_kind,
            "request_id": request.request_id,
            "status": "blocked" if kind == "helper_blocked" else "ready_to_resume",
            "needed_outputs": list(request.needed_outputs),
            "satisfied_by": list(request.satisfied_by),
            "resume_instruction": request.resume_instruction,
        })
    except Exception:
        pass


def _publish_artifact_ready_event(trace_id: str, record: ArtifactRecord) -> None:
    try:
        from app.core.environment_events import publish_workflow_event

        publish_workflow_event({
            "kind": "artifact_ready",
            "trace_id": trace_id,
            "artifact_id": record.artifact_id,
            "path": record.path,
            "artifact_type": record.artifact_type,
            "created_by": record.created_by,
            "verified_by": record.verified_by,
            "status": record.status,
        })
    except Exception:
        pass
