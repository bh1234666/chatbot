"""Delegate tool action handlers and helper-only compatibility APIs."""
from __future__ import annotations

import hashlib
import re


def _sync_delegate_globals() -> None:
    from app.llm.tools import delegate as _delegate
    globals().update({
        name: value
        for name, value in vars(_delegate).items()
        if not name.startswith("__") and name not in {
            "_spawn_helpers_only",
            "_handle_delegate_spawn_async",
            "_handle_delegate_status",
            "_peek_all_pending_results",
            "_handle_delegate_poll",
            "_handle_delegate_collect",
            "_handle_delegate_wait_any",
            "handle_delegate",
            "_dynamic_wait_loop",
            "_handle_main_kill_helper",
            "handle_spawn_helper",
            "handle_wait_helper",
            "_log_delegate_start_event",
        }
    })


def _task_prompt_for_helper(task: dict) -> str:
    """Return the canonical helper request envelope for this task."""
    try:
        from app.llm.tools.delegate_framework import format_helper_request_envelope
        return format_helper_request_envelope(task)
    except Exception:
        return str(task.get("prompt") or "")


def _effective_delegate_trace_id(trace_id: str, helper_specs: list[dict] | None = None) -> str:
    """Return a real trace id, or a per-call fallback when no trace context exists."""
    base = str(trace_id or debug.current_trace_id() or "").strip()
    if base and base != "unknown":
        return base
    parts = []
    for spec in helper_specs or []:
        if not isinstance(spec, dict):
            continue
        parts.append("|".join([
            str(spec.get("task_id") or ""),
            str(spec.get("kind") or spec.get("helper_kind") or ""),
            str(spec.get("mode") or ""),
            str(spec.get("prompt") or "")[:300],
        ]))
    digest = hashlib.sha1("\n".join(parts).encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"delegate-call-{digest or 'empty'}"


_TOP_LEVEL_TASK_FIELDS = (
    "task_id",
    "prompt",
    "kind",
    "mode",
    "framework",
    "dispatch_reason",
    "resume",
    "fork_from",
    "input_files",
    "expected_outputs",
    "acceptance_checks",
    "helper_think",
)


def _normalize_top_level_delegate_task_args(args: dict) -> bool:
    """Wrap a single top-level helper spec into tasks=[...].

    This repairs a schema-shape error without changing the model's task
    decision: task_id/prompt/kind/output/check fields are preserved exactly and
    only moved under the canonical `tasks` array.
    """
    if not isinstance(args, dict):
        return False
    if args.get("tasks"):
        return False
    if not any(args.get(key) not in (None, "", [], {}) for key in ("task_id", "prompt")):
        return False
    task = {
        key: args.get(key)
        for key in _TOP_LEVEL_TASK_FIELDS
        if key in args and args.get(key) not in (None, "", [], {})
    }
    if not task:
        return False
    args["tasks"] = [task]
    args["_normalized_top_level_task"] = True
    return True


def _publish_helper_blocked_event(
    payload: dict,
    helper_specs: list[dict],
    *,
    trace_id: str,
    archive_id: str = "",
    group_id: str = "",
    user_id: str = "",
) -> None:
    """Publish a visible workflow event when preflight blocks helper startup."""
    try:
        from app.core.environment_events import publish_workflow_event

        tasks = []
        for spec in helper_specs or []:
            if not isinstance(spec, dict):
                continue
            tasks.append({
                "task_id": spec.get("task_id") or "",
                "helper_kind": spec.get("kind") or spec.get("helper_kind") or "",
                "mode": spec.get("mode") or "",
            })
        first = tasks[0] if tasks else {}
        reason = (
            payload.get("reason")
            or payload.get("error")
            or payload.get("error_kind")
            or "helper preflight guard blocked startup"
        )
        publish_workflow_event({
            "kind": "helper_blocked",
            "status": "blocked",
            "trace_id": trace_id or "",
            "archive_id": archive_id,
            "group_id": group_id,
            "user_id": user_id,
            "task_id": first.get("task_id") or "",
            "helper_kind": first.get("helper_kind") or "",
            "blocked_count": len(tasks),
            "blocked_tasks": tasks,
            "reason": str(reason)[:600],
            "error": payload.get("error") or payload.get("error_kind") or "",
            "title": "helper blocked before start",
            "description": str(payload.get("instruction") or reason)[:900],
        })
    except Exception:
        pass


def _attention_fact_from_guard_record(record: dict, *, source: str) -> dict:
    """Convert deterministic guard records into neutral facts for the LLM guard."""
    if not isinstance(record, dict):
        return {
            "kind": "guard_attention_fact",
            "source": source,
            "needs_attention": True,
            "raw": str(record)[:500],
        }
    fact = {
        "kind": "guard_attention_fact",
        "source": source,
        # Reassuring facts (e.g. legitimate candidate reuse) may set
        # needs_attention=False explicitly; default stays True.
        "needs_attention": bool(record.get("needs_attention", True)),
        "task_id": record.get("task_id") or "",
    }
    for key in (
        "reason",
        "current_kind",
        "mode_reason",
        "expected_outputs",
        "expected_outputs_count",
        "prompt_len",
        "signals",
        "details",
        "issue",
        "signals",
        "workspace_input_files",
        "workspace_input_count",
    ):
        value = record.get(key)
        if value not in (None, "", [], {}):
            fact[key] = value
    split_values = record.get("observed_split_boundary_names") or record.get("split_into")
    if split_values not in (None, "", [], {}):
        fact["observed_split_boundary_names"] = split_values
    observed_kind = record.get("observed_helper_kind_name") or record.get("suggested_kind")
    if observed_kind not in (None, "", [], {}):
        fact["observed_helper_kind_name"] = observed_kind
    observed_mode = record.get("observed_helper_mode_name") or record.get("suggested_mode")
    if observed_mode not in (None, "", [], {}):
        fact["observed_helper_mode_name"] = observed_mode
    return fact


def _explicit_workspace_input_file_facts(helper_specs: list[dict], main_workspace: str = "") -> list[dict]:
    """Return neutral facts for explicit input_files already present in the main workspace.

    Blocked direct-write candidates and other staged workspace evidence are not
    project-root files. When the main process explicitly passes such paths as
    input_files, the guard needs the existence fact before judging the handoff.

    显式 input_files 若已在主工作区存在，只向守卫陈述可用事实，不替守卫决策。
    """
    if not helper_specs or not main_workspace:
        return []
    try:
        from pathlib import Path

        workspace_root = Path(main_workspace).resolve()
    except Exception:
        return []

    records: list[dict] = []
    for spec in helper_specs:
        if not isinstance(spec, dict):
            continue
        raw_inputs = spec.get("input_files") or spec.get("source_files") or spec.get("transferred_files") or spec.get("files") or []
        if isinstance(raw_inputs, str):
            inputs = [raw_inputs]
        elif isinstance(raw_inputs, (list, tuple, set)):
            inputs = list(raw_inputs)
        else:
            inputs = []
        available: list[dict] = []
        for raw in inputs[:60]:
            norm = str(raw or "").replace("\\", "/").strip().strip("`\"'").lstrip("./")
            if not norm or norm.startswith("../") or "/../" in f"/{norm}/":
                continue
            try:
                candidate = (workspace_root / norm).resolve()
                candidate.relative_to(workspace_root)
            except Exception:
                continue
            if not candidate.is_file():
                continue
            try:
                size = candidate.stat().st_size
            except OSError:
                size = None
            available.append({
                "path": norm,
                "size_bytes": size,
                "workspace_relative": True,
            })
            if len(available) >= 20:
                break
        if not available:
            continue
        records.append({
            "task_id": spec.get("task_id") or "",
            "issue": "explicit_input_files_exist_in_main_workspace",
            "workspace_input_count": len(available),
            "workspace_input_files": available,
            "details": (
                "These explicit input_files already exist in the main workspace. They may be staged project copies, "
                "preserved candidates, or other workspace evidence; this is an availability fact for the guard."
            ),
        })
    return records


def _blocked_create_candidate_reuse_facts(helper_specs: list[dict]) -> list[dict]:
    """Return neutral guard facts for explicit blocked-create candidate reuse.

    A preserved `.blocked_creates/...` candidate passed via input_files is an
    availability and provenance fact for main-authored document content.

    候选文件经 input_files 显式复用属于正常恢复路径；给守卫一个中性事实而不是让它猜测来源。
    """
    facts: list[dict] = []
    for spec in helper_specs or []:
        if not isinstance(spec, dict):
            continue
        candidates = [
            str(x).replace("\\", "/")
            for x in (spec.get("input_files") or [])
            if ".blocked_creates/" in str(x).replace("\\", "/")
        ]
        if not candidates:
            continue
        facts.append({
            "kind": "guard_observation",
            "issue": "blocked_create_candidate_reuse",
            "needs_attention": False,
            "task_id": str(spec.get("task_id") or "").strip(),
            "workspace_input_files": candidates[:6],
            "details": (
                "These input_files are preserved candidates from an earlier blocked direct create. "
                "Their content was authored by the main workflow from evidence it had already read. "
                "This is an availability and provenance fact for guard judgment; the current task "
                "evidence and dispatch reason still determine whether the delegation should run."
            ),
        })
    return facts


_BROWSER_AUTOMATION_EVIDENCE_RE = re.compile(
    r"(?is)\b(playwright|puppeteer|selenium|chromium|chrome|firefox|webkit)\b"
    r"|(?:host[-\s]?browser|browser\s+automation|scripted\s+browser|page\.goto)"
    r"|(?:宿主浏览器|浏览器自动化)"
)


def _browser_automation_kind_facts(helper_specs: list[dict]) -> list[dict]:
    """Return neutral guard facts for browser evidence that needs runtime commands.

    This does not allow or block a delegation. It gives the guard the concrete
    capability fact so browser evidence collection is not mistaken for ordinary
    material reading merely because the final evidence is text.

    浏览器自动化取证需要命令/runtime 能力；这里只向守卫陈述事实。
    """
    facts: list[dict] = []
    for spec in helper_specs or []:
        if not isinstance(spec, dict):
            continue
        text = "\n".join(
            str(spec.get(key) or "")
            for key in ("prompt", "framework", "dispatch_reason")
        )
        checks = spec.get("acceptance_checks") or spec.get("checks") or []
        if isinstance(checks, (list, tuple, set)):
            text += "\n" + "\n".join(str(item or "") for item in checks)
        else:
            text += "\n" + str(checks or "")
        if not _BROWSER_AUTOMATION_EVIDENCE_RE.search(text):
            continue
        facts.append({
            "kind": "guard_observation",
            "issue": "browser_automation_evidence_capability",
            "needs_attention": False,
            "task_id": str(spec.get("task_id") or "").strip(),
            "current_kind": str(spec.get("kind") or "").strip(),
            "details": (
                "This helper envelope explicitly asks for browser/host-browser or Playwright/Puppeteer/Selenium/"
                "Chromium-style evidence. That evidence needs browser automation or command runtime capability, "
                "so kind='code' can be appropriate even when the extracted facts are textual. Plain HTTP fetches, "
                "source reads, or docs-file reads are separate evidence types unless the active task accepts them."
            ),
        })
    return facts


def _attach_guard_attention_facts(helper_specs: list[dict], *, trace_id: str = "", main_workspace: str = "") -> list[dict]:
    """Attach non-LLM deterministic quality facts before the LLM guard judges."""
    if not helper_specs:
        return helper_specs
    _sync_delegate_globals()
    _deterministic_splits = _deterministic_source_read_split_recommendations(helper_specs)
    _deterministic_kinds = _deterministic_kind_recommendations(helper_specs)
    _compact_text_splits = _deterministic_compact_text_bundle_split_observations(helper_specs)
    try:
        _compact_text_owner_facts = _deterministic_compact_text_owner_observations(helper_specs)
    except Exception as exc:
        debug.log("delegate.guard_attention.compact_owner_failed", f"{type(exc).__name__}: {exc}")
        _compact_text_owner_facts = []
    _workspace_input_facts = _explicit_workspace_input_file_facts(helper_specs, main_workspace)
    _candidate_reuse_facts = _blocked_create_candidate_reuse_facts(helper_specs)
    _browser_automation_facts = _browser_automation_kind_facts(helper_specs)
    _same_batch_overlaps = _deterministic_same_batch_output_overlap_observations(helper_specs)
    _recent_overlaps = []
    _ready_artifact_overlaps = []
    if trace_id:
        try:
            _recent_overlaps = _deterministic_recent_output_overlap_observations(
                helper_specs,
                recent_records=_get_completion_ledger(trace_id, last_n=16),
            )
        except Exception as exc:
            debug.log("delegate.guard_attention.recent_overlap_failed", f"{type(exc).__name__}: {exc}")
            _recent_overlaps = []
        try:
            from app.core import agent_state as _agent_state
            _state = _agent_state.structured_status(trace_id)
            _ready_artifact_overlaps = _deterministic_ready_artifact_overlap_observations(
                helper_specs,
                ready_artifacts=list(_state.get("artifacts_ready") or []),
            )
        except Exception as exc:
            debug.log("delegate.guard_attention.ready_artifact_overlap_failed", f"{type(exc).__name__}: {exc}")
            _ready_artifact_overlaps = []
    if not (
        _deterministic_splits
        or _deterministic_kinds
        or _compact_text_splits
        or _compact_text_owner_facts
        or _workspace_input_facts
        or _candidate_reuse_facts
        or _browser_automation_facts
        or _same_batch_overlaps
        or _recent_overlaps
        or _ready_artifact_overlaps
    ):
        return helper_specs
    by_tid = {
        str(spec.get("task_id") or "").strip(): spec
        for spec in helper_specs
        if isinstance(spec, dict)
    }
    batch_target = helper_specs[0]
    for rec in _deterministic_splits:
        fact = _attention_fact_from_guard_record(rec, source="deterministic_split_check")
        target = by_tid.get(str(fact.get("task_id") or "").strip()) or batch_target
        target.setdefault("guard_observations", []).append(fact)
    for rec in _deterministic_kinds:
        fact = _attention_fact_from_guard_record(rec, source="deterministic_kind_check")
        target = by_tid.get(str(fact.get("task_id") or "").strip()) or batch_target
        target.setdefault("guard_observations", []).append(fact)
    for rec in _compact_text_splits:
        fact = _attention_fact_from_guard_record(rec, source="compact_text_material_bundle_check")
        target = by_tid.get(str(fact.get("task_id") or "").strip()) or batch_target
        target.setdefault("guard_observations", []).append(fact)
    for rec in _compact_text_owner_facts:
        fact = _attention_fact_from_guard_record(rec, source="compact_text_owner_shape_check")
        target = by_tid.get(str(fact.get("task_id") or "").strip()) or batch_target
        target.setdefault("guard_observations", []).append(fact)
    for rec in _workspace_input_facts:
        fact = _attention_fact_from_guard_record(rec, source="workspace_input_file_availability")
        target = by_tid.get(str(fact.get("task_id") or "").strip()) or batch_target
        target.setdefault("guard_observations", []).append(fact)
    for rec in _candidate_reuse_facts:
        fact = _attention_fact_from_guard_record(rec, source="blocked_create_candidate_reuse_check")
        target = by_tid.get(str(fact.get("task_id") or "").strip()) or batch_target
        target.setdefault("guard_observations", []).append(fact)
    for rec in _browser_automation_facts:
        fact = _attention_fact_from_guard_record(rec, source="browser_automation_capability_check")
        target = by_tid.get(str(fact.get("task_id") or "").strip()) or batch_target
        target.setdefault("guard_observations", []).append(fact)
    for rec in _same_batch_overlaps:
        fact = _attention_fact_from_guard_record(rec, source="same_batch_output_overlap_check")
        target = by_tid.get(str(fact.get("task_id") or "").strip()) or batch_target
        target.setdefault("guard_observations", []).append(fact)
    for rec in _recent_overlaps:
        fact = _attention_fact_from_guard_record(rec, source="recent_output_overlap_check")
        target = by_tid.get(str(fact.get("task_id") or "").strip()) or batch_target
        target.setdefault("guard_observations", []).append(fact)
    for rec in _ready_artifact_overlaps:
        fact = _attention_fact_from_guard_record(rec, source="ready_artifact_overlap_check")
        target = by_tid.get(str(fact.get("task_id") or "").strip()) or batch_target
        target.setdefault("guard_observations", []).append(fact)
    return helper_specs


def _model_visible_warning_fact(record: dict) -> dict:
    """Return a non-prescriptive warning fact for model-visible outputs."""
    if not isinstance(record, dict):
        return {"issue": "warning", "details": str(record)[:800]}
    fact: dict = {}
    for key, value in record.items():
        if value in (None, "", [], {}):
            continue
        if key in {"suggested_action", "suggestion"}:
            continue
        if key in {"suggested_kind", "observed_helper_kind_name"}:
            fact["observed_helper_kind_name"] = value
            continue
        if key in {"suggested_mode", "observed_helper_mode_name"}:
            fact["observed_helper_mode_name"] = value
            continue
        if key in {"split_into", "observed_split_boundary_names"}:
            fact["observed_split_boundary_names"] = value
            continue
        if key == "should_split":
            continue
        fact[key] = value
    return fact


def _model_visible_warning_facts(records: list[dict]) -> list[dict]:
    """Strip symbolic recommendations before returning warnings to the LLM."""
    return [_model_visible_warning_fact(record) for record in (records or [])]


def _keep_guard_result_after_fact_attachment(guard_result, helper_specs: list[dict]):
    """Return the LLM guard result unchanged.

    Deterministic checks are allowed to attach neutral guard_observations before
    the LLM guard runs. They must not become split/kind/framework decisions by
    themselves; only the guard LLM may produce a hard planning intervention.

    符号化检测只给守卫事实，不能自行生成硬拦截结论。
    """
    return guard_result


def _non_tts_guard_specs(helper_specs: list[dict]) -> list[dict]:
    """Return helper specs that still need the generic delegation guard.

    TTS helpers are an execution channel for a voice route already selected by
    the main process/route model. Running the generic task-quality guard again
    inside that channel adds latency and can turn a simple synthesis request
    into a second authorization/persona review. Wrong attempts to synthesize
    speech through code/external engines are still guarded before they become
    TTS work because their helper kind is not `tts`.
    """
    return [
        spec
        for spec in (helper_specs or [])
        if str((spec or {}).get("kind") or (spec or {}).get("helper_kind") or "").strip().lower() != "tts"
    ]


def _guard_specs_brief(helper_specs: list[dict], *, limit: int = 6) -> list[dict]:
    out: list[dict] = []
    for spec in (helper_specs or [])[:limit]:
        if not isinstance(spec, dict):
            continue
        out.append({
            "task_id": spec.get("task_id"),
            "kind": spec.get("kind") or spec.get("helper_kind"),
            "mode": spec.get("mode"),
        })
    return out


async def _run_hard_pair_preflight_guard(
    args: dict,
    helper_specs: list[dict],
    trace_id: str,
    *,
    main_workspace: str = "",
    archive_id: str = "",
    group_id: str = "",
    user_id: str = "",
) -> dict | None:
    """Run the delegation guard before starting expensive hard helpers.

    Ordinary easy batches keep the existing parallel guard path. Hard-paired
    batches and explicit hard helpers spend higher model budget, so broad or
    wrong-kind work should be corrected before expensive helpers start.

    hard 资源批次先过同一套 guard；普通 easy 批次仍保持并行守卫，避免拖慢常规路径。
    """
    _sync_delegate_globals()
    _paired_task_map = args.get("_paired_task_map") or {}
    helper_specs_for_guard = _non_tts_guard_specs(helper_specs)
    if helper_specs and not helper_specs_for_guard:
        debug.log(
            "delegate.hard_resource.preflight_guard.tts_skipped",
            f"skip generic hard-resource guard for {len(helper_specs)} tts helper(s); "
            "tts route authorization already happened before helper start",
            {"tasks": _guard_specs_brief(helper_specs)},
        )
        return None
    _has_pairing = bool(_paired_task_map) or any(s.get("paired_with") for s in helper_specs_for_guard)
    _has_hard_helper = any(str(s.get("mode") or "").strip().lower() == "hard" for s in helper_specs_for_guard)
    if not (_has_pairing or _has_hard_helper) or not helper_specs_for_guard:
        return None
    try:
        _guard_specs = _non_tts_guard_specs(args.get("_guard_task_specs") or helper_specs_for_guard)
        if not _guard_specs:
            debug.log(
                "delegate.hard_resource.preflight_guard.tts_skipped",
                "skip generic hard-resource guard after filtering tts helper specs",
                {"tasks": _guard_specs_brief(helper_specs)},
            )
            return None
        _guard_specs = [
            {
                "task_id": s.get("task_id"),
                "kind": s.get("kind", "code"),
                "mode": s.get("mode", "easy"),
                "prompt": s.get("prompt", ""),
                "framework": s.get("framework", ""),
                "dispatch_reason": s.get("dispatch_reason", ""),
                "input_files": s.get("input_files", []),
                "expected_outputs": s.get("expected_outputs", []),
                "acceptance_checks": s.get("acceptance_checks", []),
                "guard_observations": list(s.get("guard_observations") or []),
            }
            for s in _guard_specs
        ]
        _guard_specs = _attach_guard_attention_facts(
            _guard_specs,
            trace_id=trace_id,
            main_workspace=main_workspace,
        )
        _guard_result = await _persona_consent_guard(
            _current_persona_excerpt.get(""),
            _current_user_message.get(""),
            _guard_specs,
        )
        _guard_result = _keep_guard_result_after_fact_attachment(_guard_result, _guard_specs)
        from app.llm.tools.delegate_wait import _build_guard_intervention
        _effective_trace = _effective_delegate_trace_id(trace_id, _guard_specs)
        _payload = await _build_guard_intervention(
            _guard_result,
            trace_id=_effective_trace,
            cancel_helpers=False,
            helper_specs=_guard_specs,
        )
        if _payload is not None:
            _payload["helpers_initially_spawned"] = 0
            _payload["preflight_guard"] = True
            _publish_helper_blocked_event(
                _payload,
                _guard_specs,
                trace_id=_effective_trace,
                archive_id=archive_id,
                group_id=group_id,
                user_id=user_id,
            )
            debug.log(
                "delegate.hard_resource.preflight_blocked",
                f"blocked hard helper batch before helper start: {_payload.get('error')}",
            )
            return _payload
        debug.log(
            "delegate.hard_resource.preflight_passed",
            f"guard passed for {len(_guard_specs)} primary task(s) before hard helper start",
            {"tasks": _guard_specs_brief(_guard_specs)},
        )
        return None
    except Exception as exc:
        debug.log("delegate.hard_resource.preflight_failed", f"{type(exc).__name__}: {exc}")
        return None


async def _run_delegate_preflight_guard(
    args: dict,
    helper_specs: list[dict],
    trace_id: str,
    *,
    main_workspace: str = "",
    archive_id: str = "",
    group_id: str = "",
    user_id: str = "",
) -> dict | None:
    """Run the same delegation guard before helpers are started.

    The wait-loop guard still exists as a fallback, but split/kind/persona
    interventions are much cheaper and cleaner before any helper stream opens.

    所有 helper 启动前先过同一套守卫；避免先拉起 helper 再返回拆分/类型错误。
    """
    _sync_delegate_globals()
    if not helper_specs:
        return None
    if all(bool(s.get("resume")) for s in helper_specs):
        debug.log(
            "delegate.preflight_guard.resume_recovery_skipped",
            f"skip preflight guard for {len(helper_specs)} resume helper(s); "
            "resume attempts are recovery decisions guided by granularity warnings",
        )
        return None
    try:
        helper_specs_for_guard = _non_tts_guard_specs(helper_specs)
        if not helper_specs_for_guard:
            debug.log(
                "delegate.preflight_guard.tts_skipped",
                f"skip generic task-quality guard for {len(helper_specs)} tts helper(s); "
                "tts route authorization already happened before helper start",
                {"tasks": _guard_specs_brief(helper_specs)},
            )
            return None
        _guard_specs = _non_tts_guard_specs(args.get("_guard_task_specs") or helper_specs_for_guard)
        if not _guard_specs:
            debug.log(
                "delegate.preflight_guard.tts_skipped",
                "skip generic task-quality guard after filtering tts helper specs",
                {"tasks": _guard_specs_brief(helper_specs)},
            )
            return None
        _guard_specs = [
            {
                "task_id": s.get("task_id"),
                "kind": s.get("kind", "code"),
                "mode": s.get("mode", "easy"),
                "prompt": s.get("prompt", ""),
                "framework": s.get("framework", ""),
                "dispatch_reason": s.get("dispatch_reason", ""),
                "input_files": s.get("input_files", []),
                "expected_outputs": s.get("expected_outputs", []),
                "acceptance_checks": s.get("acceptance_checks", []),
                "guard_observations": list(s.get("guard_observations") or []),
            }
            for s in _guard_specs
        ]
        _guard_specs = _attach_guard_attention_facts(
            _guard_specs,
            trace_id=trace_id,
            main_workspace=main_workspace,
        )
        _guard_result = await _persona_consent_guard(
            _current_persona_excerpt.get(""),
            _current_user_message.get(""),
            _guard_specs,
        )
        _guard_result = _keep_guard_result_after_fact_attachment(_guard_result, _guard_specs)
        from app.llm.tools.delegate_wait import _build_guard_intervention
        _effective_trace = _effective_delegate_trace_id(trace_id, _guard_specs)
        _payload = await _build_guard_intervention(
            _guard_result,
            trace_id=_effective_trace,
            cancel_helpers=False,
            helper_specs=_guard_specs,
        )
        if _payload is not None:
            _payload["helpers_initially_spawned"] = 0
            _payload["preflight_guard"] = True
            _publish_helper_blocked_event(
                _payload,
                _guard_specs,
                trace_id=_effective_trace,
                archive_id=archive_id,
                group_id=group_id,
                user_id=user_id,
            )
            debug.log(
                "delegate.preflight_guard.blocked",
                f"blocked helper batch before helper start: {_payload.get('error')}",
            )
            return _payload
        debug.log(
            "delegate.preflight_guard.passed",
            f"guard passed for {len(_guard_specs)} primary task(s) before helper start",
            {"tasks": _guard_specs_brief(_guard_specs)},
        )
        return None
    except Exception as exc:
        debug.log("delegate.preflight_guard.failed", f"{type(exc).__name__}: {exc}")
        return None


def _auto_fetch_environment_workspace_refs(main_workspace: str, tasks: list[dict]) -> dict:
    """Populate main `_env/` copies for existing project files referenced by env helpers.

    Helpers receive a copied workspace, not direct access to the environment project
    directory. If the main task mentions a directory such as `_env/src/pkg`, prefetch
    the existing files beneath it so helper reads and indexes have the same view the
    main thread saw through environment tools.
    """
    try:
        from app.core.runtime_mode import current_environment, is_environment_mode
        if not is_environment_mode():
            return {"fetched": [], "skipped": []}
        env = current_environment()
        if env is None:
            return {"fetched": [], "skipped": []}
        from app.core.filesystem import FileRegistry, stage_project_file
        from app.llm.tools import environment_resources as _env_resources
        import os as _os
        import re as _re
        from pathlib import Path as _Path

        project_root = _Path(env.root_dir).resolve()
        workspace_root = _Path(main_workspace).resolve()
        file_registry = FileRegistry.load(
            scope_id=f"env:{project_root}",
            workspace_root=workspace_root,
            project_root=project_root,
        )
        refs: set[str] = set()
        explicit_refs: set[str] = set()
        context_refs: set[str] = set()
        visual_doc_exts = _env_resources.VISUAL_DOC_EXTS
        code_text_exts = _env_resources.CODE_TEXT_EXTS
        project_root_token = str(project_root).replace("\\", "/")
        known_exts = tuple(code_text_exts + visual_doc_exts)
        source_material_exts = _env_resources.SOURCE_MATERIAL_EXTS
        output_name_re = _re.compile(
            r"(?:evidence|summary|report|analysis|outline|contract|framework|draft|final|验证|报告|摘要|证据|清单)",
            _re.IGNORECASE,
        )

        def _add_explicit_ref(raw_ref: str, *, from_env_prefix: bool = False, from_task_field: bool = False) -> None:
            text = str(raw_ref or "").replace("\\", "/").strip().strip("`\"'")
            if not text:
                return
            text = text.rstrip(".,;:)]}")
            if text == "_env":
                return
            if text.startswith("_env/"):
                text = text[5:].lstrip("/")
            if project_root_token and text.lower().startswith(project_root_token.lower().rstrip("/") + "/"):
                text = text[len(project_root_token.rstrip("/")) + 1:]
            if (
                not text
                or text.startswith(("_helpers_shared/", "_delegate_", ".temp/", ".prev/"))
                or text.startswith("../")
                or "/../" in f"/{text}/"
                or (":" in text and not from_env_prefix)
            ):
                return
            source = (project_root / text).resolve()
            try:
                source.relative_to(project_root)
            except ValueError:
                return
            if source.is_file() or source.is_dir():
                refs.add(text)
                if from_task_field:
                    explicit_refs.add(text)

        for task in tasks:
            prompt = str(task.get("prompt") or "").replace("\\", "/")
            for field in ("input_files", "source_files", "transferred_files", "files"):
                raw_values = task.get(field) if isinstance(task, dict) else None
                if isinstance(raw_values, (list, tuple, set)):
                    for raw_ref in raw_values:
                        _add_explicit_ref(str(raw_ref), from_env_prefix=True, from_task_field=True)
                elif isinstance(raw_values, str):
                    _add_explicit_ref(raw_values, from_env_prefix=True, from_task_field=True)
            for match in _re.finditer(
                r"_env/([^`\"'<>|\r\n]+?\.(?:"
                + "|".join(known_exts)
                + r"))",
                prompt,
                flags=_re.IGNORECASE,
            ):
                _add_explicit_ref("_env/" + match.group(1), from_env_prefix=True)
            for match in _re.finditer(r"_env/([^\s`\"'<>|]+)", prompt):
                _add_explicit_ref("_env/" + match.group(1), from_env_prefix=True)
            for match in _re.finditer(
                r"(?<![\w.-])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\."
                rf"(?:{'|'.join(code_text_exts + visual_doc_exts)}))",
                prompt,
            ):
                _add_explicit_ref(match.group(1))
            for match in _re.finditer(r"_env/([^`\"'<>|\r\n]+?/) ", prompt + " "):
                _add_explicit_ref("_env/" + match.group(1), from_env_prefix=True)
            for match in _re.finditer(r"_env/([^`\"'<>|\r\n]+?)(?:下|中|内|目录|文件夹)", prompt):
                _add_explicit_ref("_env/" + match.group(1), from_env_prefix=True)
            if project_root_token:
                escaped_root = _re.escape(project_root_token.rstrip("/"))
                for match in _re.finditer(
                    escaped_root + r"/([^`\"'<>|]+?\.(?:"
                    + "|".join(code_text_exts + visual_doc_exts)
                    + r"))",
                    prompt,
                    flags=_re.IGNORECASE,
                ):
                    _add_explicit_ref(match.group(1), from_env_prefix=True)
            for match in _re.finditer(
                r"['\"`]([^'\"`<>|]+?\.(?:"
                + "|".join(visual_doc_exts)
                + r"))['\"`]",
                prompt,
                flags=_re.IGNORECASE,
            ):
                _add_explicit_ref(match.group(1))
            prompt_l = prompt.lower()
            try:
                scanned_dirs = 0
                for child in project_root.rglob("*"):
                    if scanned_dirs >= 2000:
                        break
                    if not child.is_dir():
                        continue
                    scanned_dirs += 1
                    rel = child.resolve().relative_to(project_root).as_posix()
                    if _env_resources._skip_rel(rel):
                        continue
                    rel_l = rel.lower()
                    if f"_env/{rel_l}" in prompt_l or _re.search(rf"(?<![\w./-]){_re.escape(rel_l)}(?![\w./-])", prompt_l):
                        _add_explicit_ref(rel, from_env_prefix=True)
            except OSError:
                pass

        # Environment helpers can only see project files that have been fetched
        # into `_env/`. Seed small project-level context so helpers do not waste
        # turns probing common files that were not explicitly referenced.
        for rel in (
            "pyproject.toml",
            "package.json",
            "requirements.txt",
            "README.md",
            "Makefile",
            "CMakeLists.txt",
        ):
            if (project_root / rel).is_file():
                context_refs.add(rel)
        for rel in tuple(refs):
            parent = _Path(rel).parent
            if str(parent) in ("", "."):
                continue
            for name in ("__init__.py", "README.md", "conftest.py"):
                candidate = parent / name
                if (project_root / candidate).is_file():
                    context_refs.add(candidate.as_posix())

        fetched: list[str] = []
        skipped: list[dict] = []
        max_auto_fetch_files = 120

        def _is_summarize_inventory_task() -> bool:
            return _env_resources.task_requested_inventory(tasks)

        wrote_inventory = False

        def _write_project_inventory() -> None:
            nonlocal wrote_inventory
            if not _is_summarize_inventory_task():
                return
            try:
                _env_resources.write_resource_manifest_files(project_root, workspace_root, tasks)
            except OSError as exc:
                skipped.append({"path": ".", "reason": f"inventory_walk_failed:{type(exc).__name__}"})
            wrote_inventory = True
            if "project_inventory.md" not in fetched:
                fetched.append("project_inventory.md")
            if ".resource_manifest.json" not in fetched:
                fetched.append(".resource_manifest.json")

        def _skip_rel(path: str) -> str | None:
            if path.startswith(".") or "/." in path:
                return "hidden_or_internal"
            if _Path(path).name.startswith("~$"):
                return "office_lock_file"
            parts = set(_Path(path).parts)
            if parts & {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".env_backups"}:
                return "generated_or_heavy"
            return None

        _write_project_inventory()

        # Prompts often contain file names copied from directory listings:
        # Chinese names, spaces, parentheses, and unquoted Office/image paths
        # do not fit the lightweight token regex above. Match against real
        # project files so helpers receive exact staged copies rather than
        # guessing shortened `_env/...` paths.
        for task in tasks:
            prompt_l = str(task.get("prompt") or "").replace("\\", "/").lower()
            if not prompt_l:
                continue
            try:
                scanned = 0
                for child in project_root.rglob("*"):
                    if scanned >= 5000:
                        break
                    if not child.is_file():
                        continue
                    scanned += 1
                    rel = child.resolve().relative_to(project_root).as_posix()
                    if _skip_rel(rel):
                        continue
                    ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
                    if ext not in known_exts:
                        continue
                    rel_l = rel.lower()
                    base_l = child.name.lower()
                    if (
                        rel_l in prompt_l
                        or f"_env/{rel_l}" in prompt_l
                        or base_l in prompt_l
                    ):
                        refs.add(rel)
            except OSError:
                pass

        def _fetch_one(rel_path: str) -> None:
            if len(fetched) >= max_auto_fetch_files:
                skipped.append({"path": rel_path, "reason": "auto_fetch_file_limit"})
                return
            workspace_copy = workspace_root / "_env" / rel_path
            if workspace_copy.exists():
                skipped.append({"path": rel_path, "reason": "workspace_copy_exists"})
                return
            try:
                stage_project_file(file_registry, rel_path)
                fetched.append(rel_path)
            except Exception as exc:
                skipped.append({"path": rel_path, "reason": f"stage_failed:{type(exc).__name__}"})

        for rel in sorted(context_refs | refs):
            if rel.startswith(".") or "/." in rel:
                skipped.append({"path": rel, "reason": "hidden_or_internal"})
                continue
            source = (project_root / rel).resolve()
            try:
                source.relative_to(project_root)
            except ValueError:
                skipped.append({"path": rel, "reason": "escape"})
                continue
            if source.is_dir():
                copied = 0
                explicit_dir = rel in explicit_refs
                for child in sorted(source.rglob("*")):
                    if not child.is_file():
                        continue
                    child_rel = child.resolve().relative_to(project_root).as_posix()
                    skip_reason = _skip_rel(child_rel)
                    if skip_reason:
                        skipped.append({"path": child_rel, "reason": skip_reason})
                        continue
                    ext = child_rel.rsplit(".", 1)[-1].lower() if "." in child_rel else ""
                    if ext not in source_material_exts:
                        skipped.append({"path": child_rel, "reason": "directory_source_type_skipped"})
                        continue
                    if output_name_re.search(_Path(child_rel).name) and not explicit_dir and child_rel not in explicit_refs:
                        skipped.append({"path": child_rel, "reason": "looks_like_generated_output"})
                        continue
                    _fetch_one(child_rel)
                    copied += 1
                    if copied >= 40 or len(fetched) >= max_auto_fetch_files:
                        skipped.append({"path": rel, "reason": "directory_fetch_limit"})
                        break
                if copied == 0:
                    skipped.append({"path": rel, "reason": "directory_no_source_files_fetched"})
                continue
            if not source.is_file():
                skipped.append({"path": rel, "reason": "source_missing"})
                continue
            skip_reason = _skip_rel(rel)
            if skip_reason:
                skipped.append({"path": rel, "reason": skip_reason})
                continue
            _fetch_one(rel)
        if wrote_inventory:
            try:
                _env_resources.write_resource_manifest_files(project_root, workspace_root, tasks)
            except OSError as exc:
                skipped.append({"path": ".", "reason": f"inventory_refresh_failed:{type(exc).__name__}"})
        return {"fetched": fetched, "skipped": skipped}
    except Exception as exc:
        try:
            log.warning("environment auto-fetch before delegate failed: %s", exc)
        except Exception:
            pass
        return {"fetched": [], "skipped": [{"path": "*", "reason": type(exc).__name__}]}


def _normalize_environment_output_paths_from_manifest(main_workspace: str, tasks: list[dict]) -> None:
    """Resolve bare project output basenames to unique staged `_env/...` paths.

    The manifest remains the path source of truth. This only normalizes an
    explicit helper output when the basename has one exact staged project match;
    ambiguous names stay unchanged so the helper can ask the main process for a
    clearer ownership contract.
    """
    try:
        from app.core.runtime_mode import is_environment_mode
        if not is_environment_mode():
            return
    except Exception:
        return
    try:
        from app.llm.tools.environment_resources import load_resource_manifest
        manifest = load_resource_manifest(main_workspace)
    except Exception:
        return
    resources = manifest.get("resources") if isinstance(manifest, dict) else None
    if not isinstance(resources, list) or not resources:
        return

    by_base: dict[str, list[str]] = {}
    for item in resources:
        if not isinstance(item, dict):
            continue
        staged = str(item.get("staged_path") or "").replace("\\", "/").lstrip("./")
        project_path = str(item.get("project_path") or "").replace("\\", "/").lstrip("./")
        candidate = staged if staged.startswith("_env/") else (f"_env/{project_path}" if project_path else "")
        if not candidate or candidate == "_env/":
            continue
        base_name = candidate.rsplit("/", 1)[-1].lower()
        by_base.setdefault(base_name, []).append(candidate)

    for task in tasks:
        if not isinstance(task, dict):
            continue
        outputs = task.get("expected_outputs")
        if not isinstance(outputs, list) or not outputs:
            continue
        normalized: list[str] = []
        changed = False
        seen: set[str] = set()
        for raw in outputs:
            out = str(raw or "").replace("\\", "/").strip().lstrip("./")
            if not out:
                continue
            replacement = out
            if "/" not in out and not out.startswith("_env/"):
                matches = by_base.get(out.lower(), [])
                unique = sorted(set(matches))
                if len(unique) == 1:
                    replacement = unique[0]
                    changed = changed or replacement != out
            if replacement not in seen:
                seen.add(replacement)
                normalized.append(replacement)
        if changed:
            try:
                from app.core import debug as _debug
                _debug.log(
                    "delegate.environment.expected_outputs.normalized",
                    (
                        f"task '{task.get('task_id', '?')}' expected_outputs normalized "
                        f"from {outputs} to {normalized} using _env resource manifest"
                    ),
                )
            except Exception:
                pass
            task["expected_outputs"] = normalized


def _annotate_source_count_hints_from_manifest(main_workspace: str, tasks: list[dict]) -> None:
    """Attach internal source-material counts for preflight read fan-out.

    Environment prompts often say "read all files in this directory" without
    listing every file. The resource manifest is the path source of truth, so
    use its category counts as a lightweight guard hint before helpers start.

    用资源清单给 guard 提供材料数量提示；仍由主进程按返回建议重新派发。
    """
    try:
        from app.core.runtime_mode import is_environment_mode
        if not is_environment_mode():
            return
    except Exception:
        return
    try:
        from app.llm.tools.environment_resources import load_resource_manifest
        manifest = load_resource_manifest(main_workspace)
    except Exception:
        return
    summary = manifest.get("summary") if isinstance(manifest, dict) else None
    category_counts = (summary or {}).get("category_counts") if isinstance(summary, dict) else None
    if not isinstance(category_counts, dict):
        return
    source_count = 0
    for key in ("office_pdf", "image", "text"):
        try:
            source_count += int(category_counts.get(key) or 0)
        except (TypeError, ValueError):
            pass
    if source_count < 6:
        return
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        kind = str(task.get("kind") or "").strip().lower()
        if kind not in {"code", "edit"}:
            continue
        prompt_l = str(task.get("prompt") or "").lower()
        if not any(marker in prompt_l for marker in (
            "all files", "all materials", "source material", "directory", "coverage",
            "read all", "summarize all", "整理", "所有文件", "全部文件", "所有材料",
            "全部材料", "当前目录", "目录", "分四科", "逐个", "每个文件",
        )):
            continue
        try:
            previous = int(task.get("_source_count_hint") or 0)
        except (TypeError, ValueError):
            previous = 0
        task["_source_count_hint"] = max(previous, source_count)


async def _spawn_helpers_only(
    cleaned_tasks: list[dict],
    *,
    main_workspace: str,
    archive_id: str,
    group_id: str,
    user_id: str,
    main_owner: str,
    user_lang_now: str,
    spawn_queue: asyncio.Queue,
    helper_think: bool = False,
    guard_args: dict | None = None,
) -> tuple[list[dict], asyncio.Event]:
    """Spawn helper tasks WITHOUT entering wait loop. Returns (specs, all_registered_event).

    Each spec dict: {task_id, task (asyncio.Task), proc_id, workspace}.
    all_registered_event is set after all helpers are registered in ProcessRegistry.
    Extracted from handle_delegate so spawn_async and spawn can share.
    """
    _sync_delegate_globals()

    from pathlib import Path as _Path

    base = _Path(main_workspace)
    user_tag = _user_workspace_tag(user_id)
    _env_fetch_stats = _auto_fetch_environment_workspace_refs(main_workspace, cleaned_tasks)
    if _env_fetch_stats.get("fetched"):
        debug.log(
            "delegate.environment.auto_fetch",
            f"fetched {len(_env_fetch_stats['fetched'])} _env refs before helper spawn",
            _env_fetch_stats,
        )
    _normalize_environment_output_paths_from_manifest(main_workspace, cleaned_tasks)
    _annotate_source_count_hints_from_manifest(main_workspace, cleaned_tasks)

    # Prepare workspace for each task
    initial_helper_specs: list[dict] = []
    for c in cleaned_tasks:
        target_ws = str(base / f"_delegate_{user_tag}_{c['task_id']}")

        # v2 三层隔离: resume 时旧 workspace 可能在 .prev/
        if c["resume"] and not os.path.isdir(target_ws):
            prev_base = os.path.join(os.path.dirname(str(base)), ".prev")
            prev_ws = os.path.join(prev_base, f"_delegate_{user_tag}_{c['task_id']}")
            if os.path.isdir(prev_ws):
                os.makedirs(target_ws, exist_ok=True)
                _copied_files = 0
                for _entry in os.listdir(prev_ws):
                    _src = os.path.join(prev_ws, _entry)
                    _dst = os.path.join(target_ws, _entry)
                    try:
                        if os.path.isfile(_src) or os.path.islink(_src):
                            shutil.copy2(_src, _dst)
                            _copied_files += 1
                        elif os.path.isdir(_src) and not _entry.startswith("_delegate_"):
                            shutil.copytree(_src, _dst, dirs_exist_ok=True)
                            _copied_files += sum(1 for _ in _Path(_dst).rglob("*") if _.is_file())
                    except OSError:
                        pass
                debug.log(
                    f"delegate.{c['task_id']}.prev_migrate",
                    f"resume: copied ~{_copied_files} files from .prev/ to .temp/",
                )

        if c["fork_from"]:
            src_tid = _sanitize_task_id(c["fork_from"], 0)
            src_ws = str(base / f"_delegate_{user_tag}_{src_tid}")
            if not os.path.isdir(src_ws):
                raise ValueError(f"fork_from 源 workspace 不存在: {src_tid}")
            sz = _dir_size(src_ws)
            if sz > _FORK_WORKSPACE_REJECT_BYTES:
                raise ValueError(f"fork_from 源工作区 {sz // 1024 // 1024}MB 太大,拒绝(>500MB)")
            if sz > _FORK_WORKSPACE_WARN_BYTES:
                debug.log("delegate.fork.large",
                          f"{c['task_id']} forking {sz // 1024 // 1024}MB from {src_tid}")
            clean_workspace_dir(target_ws)
            n_copied = await _fast_copy_workspace(src_ws, target_ws)
            _cap = enforce_workspace_capacity(
                target_ws,
                label=f"helper_fork:{c['task_id']}",
            )
            if not _cap.get("ok", True):
                raise ValueError(
                    f"fork_from 目标工作区超过容量上限: "
                    f"{_cap.get('after_bytes', 0) // 1024 // 1024}MB"
                )
            debug.log("delegate.fork.copied",
                      f"{c['task_id']}: copied {n_copied} files from {src_tid}")
            c["resume"] = True
        initial_helper_specs.append({**c, "workspace": target_ws})

    # Clean main workspace before spawn
    _cleaned = _clean_main_workspace_before_spawn(main_workspace)

    _preflight_guard_payload = await _run_delegate_preflight_guard(
        guard_args or {"_paired_task_map": {s["task_id"]: s.get("paired_with") for s in initial_helper_specs if s.get("paired_with")}},
        initial_helper_specs,
        debug.current_trace_id() or "unknown",
        main_workspace=main_workspace,
        archive_id=archive_id,
        group_id=group_id,
        user_id=user_id,
    )
    if _preflight_guard_payload is not None:
        raise ValueError(json.dumps(_preflight_guard_payload, ensure_ascii=False))


    _log_delegate_start_event(initial_helper_specs)

    try:
        await _record_task_contracts(debug.current_trace_id() or "unknown", initial_helper_specs)
    except Exception as exc:
        debug.log("delegate.contract_record_failed", f"{type(exc).__name__}: {exc}")

    n_active_server = await proc_registry().count_active_helpers()
    if n_active_server + len(initial_helper_specs) > _MAX_HELPERS:
        raise ValueError(
            f"服务端活跃 helper 将超过上限: {n_active_server}+{len(initial_helper_specs)}/{_MAX_HELPERS}"
        )
    n_active_agent = await proc_registry().count_active_helpers_for_trace(main_owner)
    if n_active_agent + len(initial_helper_specs) > _MAX_HELPERS_PER_AGENT:
        raise ValueError(
            f"单个智能体活跃 helper 将超过上限: "
            f"{n_active_agent}+{len(initial_helper_specs)}/{_MAX_HELPERS_PER_AGENT}"
        )

    # 2026-05-15 P98: 计算本批所有 expected_outputs (供 P35 排除 same-batch 兄弟)
    # 病因(实测排序论文 trace): sort_paper helper prompt 引用 sort_comparison_random.png,
    # 同批 sort_charts 会产此文件, 但 P35 不知道同批 sibling 的 output → 误警告"缺失"。
    _batch_sibling_outputs: set[str] = set()
    for _s in initial_helper_specs:
        for _o in (_s.get("expected_outputs") or []):
            if isinstance(_o, str) and _o.strip():
                _batch_sibling_outputs.add(_o)

    # Create helper tasks + register
    helper_specs_out: list[dict] = []
    all_registered = asyncio.Event()
    for spec in initial_helper_specs:
        per_helper_abort = asyncio.Event()
        register_done = asyncio.Event()
        # P98: 排除本 helper 自己的 expected_outputs, 只传其他兄弟的
        _own_outputs = set(spec.get("expected_outputs") or [])
        _siblings_only = _batch_sibling_outputs - _own_outputs
        task = asyncio.create_task(_run_one_helper(
            task_id=spec["task_id"],
            prompt=_task_prompt_for_helper(spec),
            main_workspace=main_workspace,
            helper_workspace=spec["workspace"],
            archive_id=archive_id,
            group_id=group_id,
            user_id=user_id,
            resume=spec["resume"],
            local_abort=per_helper_abort,
            wait_for_register=register_done,
            user_lang=user_lang_now,
            kind=spec.get("kind", "code"),
            mode=spec.get("mode", "normal"),  # 2026-05-12 P20
            helper_think=helper_think,
            input_files=spec.get("input_files") or [],
            expected_outputs=spec.get("expected_outputs") or [],  # 1.C
            write_scopes=spec.get("write_scopes") or spec.get("expected_outputs") or [],
            acceptance_checks=spec.get("acceptance_checks") or [],
            batch_sibling_outputs=_siblings_only,  # 2026-05-15 P98
        ))
        await _register_helper_with_autoclean(
            owner=main_owner,
            task=task,
            helper_task_id=spec["task_id"],
            helper_workspace=spec["workspace"],
            abort_event=per_helper_abort,
            description=f"main delegate: {spec['prompt'][:80]}",
            helper_kind=spec.get("kind", "code"),
            archive_id=archive_id,
            group_id=group_id,
            user_id=user_id,
        )
        register_done.set()
        proc_id = ""
        try:
            h = await proc_registry().find_helper_by_task_id(
                spec["task_id"], owner=main_owner,
            )
            if h is not None:
                proc_id = h.proc_id
        except Exception:
            pass
        helper_specs_out.append({
            "task_id": spec["task_id"],
            "task": task,
            "proc_id": proc_id,
            "workspace": spec["workspace"],
            "paired_with": spec.get("paired_with"),
        })

    all_registered.set()
    return helper_specs_out, all_registered


def _log_delegate_start_event(initial_helper_specs: list[dict]) -> None:
    """Log helper startup from one source for every spawn path.

    Keep this event centralized so preflight-blocked helpers are not reported
    as started and split spawn paths cannot drift in shape.

    helper 启动日志统一出口；只有守卫通过后的真实启动才记录。
    """
    debug.log(
        "delegate.start",
        f"{len(initial_helper_specs)} helpers",
        [{
            "task_id": s["task_id"],
            "resume": s.get("resume"),
            "fork_from": s.get("fork_from"),
            "kind": s.get("kind"),
        } for s in initial_helper_specs],
    )


async def _already_completed_delegate_response(
    *,
    trace_id: str,
    main_owner: str,
    main_workspace: str,
    task_ids: list[str],
    note: str,
) -> dict:
    """Return completed-task facts for duplicate delegate attempts.

    Duplicate task_id dispatch is usually a coordinator attention failure, not a
    new helper failure. Present recovered helper facts in the same shape as a
    clean delegate result when possible so the main model can continue from
    producer-owned evidence instead of treating the duplicate block as work to
    repair.
    """
    _sync_delegate_globals()
    recovered: list[dict] = []
    unrecovered: list[str] = []
    for tid in task_ids:
        try:
            result = await _recover_completed_result_for_collect(
                trace_id,
                tid,
                main_owner=main_owner,
                main_workspace=main_workspace,
            )
        except Exception:
            log.exception("duplicate completed result recovery failed for %s", tid)
            result = None
        if isinstance(result, dict) and result:
            result = dict(result)
            result.setdefault("task_id", tid)
            result.setdefault("recovered_from_duplicate_dispatch", True)
            recovered.append(result)
        else:
            unrecovered.append(tid)

    success_count = sum(
        1
        for result in recovered
        if result.get("ok") and not result.get("interrupted") and not result.get("stuck")
    )
    incomplete_count = len([result for result in recovered if not result.get("ok")]) + len(unrecovered)
    response = {
        "ok": True,
        "already_completed": True,
        "duplicate_task_ids": task_ids,
        "helpers_initially_spawned": 0,
        "helpers_completed": len(recovered),
        "helpers_still_running": 0,
        "task_ok": bool(recovered) and success_count > 0 and incomplete_count == 0,
        "success_count": success_count,
        "incomplete_count": incomplete_count,
        "results": recovered,
        "note": note,
        "_duplicate_completed_fact": (
            "The requested task_id(s) already completed in this trace. No helper was spawned. "
            "Recovered results, if present, are the relevant producer-owned helper evidence. "
            "Continue from those facts unless outputs are missing, warnings/blockers remain, "
            "or the active task requires a semantically different helper with a new task_id.\n"
            "这些 task_id 已在本 trace 完成；未启动新 helper。若已恢复结果，请把它们作为生产者自有证据继续。"
        ),
    }
    if unrecovered:
        response["unrecovered_completed_task_ids"] = unrecovered
        response["_unrecovered_completed_fact"] = (
            "Some completed task_id(s) had no recoverable helper result. This is an evidence gap, "
            "not proof the work failed. Use existing workspace/output facts, collect/status facts, "
            "or dispatch a new semantically named helper only if the active acceptance contract still has a gap.\n"
            "部分已完成 task_id 无可恢复结果；这是证据缺口，不等于工作失败。"
        )
    if (
        recovered
        and incomplete_count == 0
        and all(
            result.get("_post_helper_action") == "output_json_directly"
            for result in recovered
            if result.get("ok")
        )
    ):
        response["_stage_status"] = "clean_helper_batch"
        response["_stage_evidence_facts"] = (
            "The duplicate dispatch resolved to clean producer-self-verified helper completion facts. "
            "Trust the helper-owned content judgment and avoid respawning or re-reading helper-owned artifacts "
            "merely to validate them again. Handle only separate boundaries such as project apply/diff, "
            "missing requested outputs, warnings/contradictions, or explicit display requests; send helper-owned "
            "verifier/build/acceptance gaps to the producer helper or a verify helper.\n"
            "重复派发已解析为干净 helper 完成事实；信任 helper 产物判断，仅处理独立边界。"
        )
        response["_completion_guidance"] = (
            response["_stage_evidence_facts"]
            + "\nIf no separate boundary remains in the active task, the next step is final synthesis from these compact facts, not another helper/content read.\n"
            "若当前任务没有独立边界，下一步是基于这些精简事实收尾，而不是再读内容或重派。"
        )
    return response


async def _handle_delegate_spawn_async(
    main_workspace: str, args: dict,
    *,
    archive_id: str, group_id: str, user_id: str,
    cleaned_tasks: list[dict],
    main_owner: str,
    user_lang_now: str,
    trace_id: str,
) -> str:
    """异步 spawn:启动 helper task 立即返回 proc_ids,不等结果。"""
    _sync_delegate_globals()

    if not cleaned_tasks:
        return json.dumps({
            "ok": False,
            "error": "no valid tasks",
            "error_kind": "empty_delegate_tasks",
            "hint": (
                "delegate spawn_async requires non-empty tasks. "
                "Use delegate wait/collect/status to wait for existing helpers instead of tasks=[]. "
                "Use delegate(action='collect'/'wait_any'/'status') with task_ids instead.\n\n"
                "spawn_async 需要非空任务；等待已有 helper 时改用 collect、wait_any 或 status。"
            ),
        }, ensure_ascii=False)
    _paired_task_map = args.get("_paired_task_map") or {}

    # ── 2026-05-11: spawn-time 任务粒度警告 ──
    # 实测教训(trace 12:12→13:06): 主线程派一个 kind='edit' helper 包揽 paper.docx +
    # pptx + xlsx + 6 charts,12 min 后 stuck。主线程没意识到任务粒度过大。
    # 这里扫每个 task 的 prompt,发现"巨型任务"特征就附 warning 到返回值,让 LLM 看到
    # 主动 kill 并重派拆分版。不硬拒(避免过度激进),只在 hint 里提示。
    granularity_warnings: list[dict] = []  # 2026-05-11 P1.2: 改 list[dict] 结构化
    # 2026-05-21: 确定性框架兜底警告(sanitize 阶段检测,存于 args)
    _fw_warn = args.get("_framework_warning")
    if isinstance(_fw_warn, dict) and _fw_warn.get("issue"):
        granularity_warnings.append(_fw_warn)
    # 2026-05-22: 图表任务重复派发警告(画全部 + 各画一张两套并存)
    _chart_dup_warn = args.get("_chart_dup_warning")
    if isinstance(_chart_dup_warn, dict) and _chart_dup_warn.get("issue"):
        granularity_warnings.append(_chart_dup_warn)
    try:
        from app.llm.tools.delegate_framework import broad_framework_guard_warnings
        granularity_warnings.extend(broad_framework_guard_warnings(cleaned_tasks))
    except Exception:
        pass

    # 2026-05-11 P14.B: spawn-time 依赖图检测
    # 病因(实测 trace 20:47:26): 主线程派 gen_paper kind=edit, prompt 引用
    # `results_skiplist.csv`, 但 bench_skiplist 还没 outputs_complete=true → gen_paper
    # 找不到文件, interrupted, 又被 resume → 浪费 22 min。
    # 修法: 扫 prompt 中提到的文件名, 看 ledger 里是否已有 helper 产出。没产出 → 警告。
    # 不硬拒 — 因为可能是 sibling task 同时产出 (e.g. paper 引用 charts, 两者并行)。
    try:
        _ledger = _get_completion_ledger(trace_id, last_n=100)
    except Exception:
        _ledger = []

    # 2026-05-12 P16.A: 工作流完整性检测
    # 病因(实测 23:46 trace): 用户说"重做论文+图片", 主线程 spawn 23 个 helper 全 kind=code
    # (bench 类), 0 个 draw/edit, 最后 plan.deliverables 空。**只跑了 bench 阶段没进入图表/文档阶段**。
    # 修法: 看用户消息含"论文/PPT/图/可视化"关键词时, 检查 ledger 里有没有 kind=edit/draw 完成,
    # 没有 + 本批 spawn 全 code → 警告"工作流不完整, 用户要的文档/图你没派"。
    try:
        _user_msg = _current_user_message.get() or ""
    except LookupError:
        _user_msg = ""
    _user_wants_doc = any(s in _user_msg for s in (
        "论文", "paper", "PPT", "ppt", "pptx", "幻灯", "报告.doc",
        ".docx", ".pptx", ".xlsx", "Excel", "excel", "文档"
    ))
    _user_wants_chart = any(s in _user_msg for s in (
        "图", "可视化", "chart", "图表", "画图", "图片"
    ))
    # 看 ledger 里是否已有 kind=edit/draw 完成
    _had_edit_done = any(
        e.get("kind") == "edit" and e.get("outputs_complete") is True
        for e in _ledger
    )
    _had_draw_done = any(
        e.get("kind") == "draw" and e.get("outputs_complete") is True
        for e in _ledger
    )
    # 看本批 spawn 是否有 edit/draw
    _batch_kinds = {(_t.get("kind") or "code") for _t in cleaned_tasks}
    _batch_has_edit = "edit" in _batch_kinds
    _batch_has_draw = "draw" in _batch_kinds

    # 已 outputs_complete=true 的 helper 产出的所有文件 basename
    _completed_files: set[str] = set()
    for _e in _ledger:
        if _e.get("outputs_complete") is True:
            for _f in (_e.get("delivered_files") or []):
                if isinstance(_f, str):
                    import os as _os_dep
                    _norm_f = _f.replace("\\", "/")
                    _bn = _os_dep.path.basename(_norm_f)
                    _completed_files.add(_bn.lower())
                    if _norm_f.startswith("_helpers_shared/"):
                        _completed_files.add(_norm_f.lower())
                        _completed_files.add(_norm_f[len("_helpers_shared/"):].lower())
                    if "_" in _bn:
                        _stripped = _bn.split("_", 1)[1] if _bn.count("_") <= 2 else _bn.split("_", _bn.count("_") - 1)[-1]
                        _completed_files.add(_stripped.lower())
            for _f in (_e.get("declared_files") or []):
                if isinstance(_f, str):
                    import os as _os_dep_decl
                    _norm_f = _f.replace("\\", "/")
                    _bn = _os_dep_decl.path.basename(_norm_f)
                    _completed_files.add(_bn.lower())
                    if _norm_f.startswith("_helpers_shared/"):
                        _completed_files.add(_norm_f.lower())
                        _completed_files.add(_norm_f[len("_helpers_shared/"):].lower())
                    if "_" in _bn:
                        _stripped = _bn.split("_", 1)[1] if _bn.count("_") <= 2 else _bn.split("_", _bn.count("_") - 1)[-1]
                        _completed_files.add(_stripped.lower())
    # 本批 spawn 中所有 task 的 expected_outputs 也算"会产出"
    _will_produce_files: set[str] = set()
    # 2026-05-15 P64: 同时建立 file_basename → producer_task_id 映射, 用于检测
    # 批内顺序违规 (consumer 和 producer 同批 spawn, 但 producer 还没跑完)。
    # 病因(实测 05-15 16:28): 主线程同批 spawn comp_infra (产 compress.h) 和
    # comp_huffman/lz77/.../custom (用 compress.h), 6 个算法 helper 在 framework 写完前
    # 就开始 gcc → 全部 fatal error: compress.h: No such file → 浪费 3 分钟重试。
    _file_to_producer: dict[str, str] = {}
    for _t_local in cleaned_tasks:
        _producer_tid = _t_local.get("task_id", "")
        for _exp in (_t_local.get("expected_outputs") or []):
            if isinstance(_exp, str):
                import os as _os_dep2
                _norm_exp = _exp.replace("\\", "/")
                _bn_exp = _os_dep2.path.basename(_norm_exp).lower()
                _will_produce_files.add(_bn_exp)
                if _producer_tid and _bn_exp not in _file_to_producer:
                    _file_to_producer[_bn_exp] = _producer_tid
                if _norm_exp.startswith("_helpers_shared/"):
                    _will_produce_files.add(_norm_exp.lower())
                    _will_produce_files.add(_norm_exp[len("_helpers_shared/"):].lower())
                if "_" in _bn_exp:
                    _stripped_exp = _bn_exp.split("_", 1)[1] if _bn_exp.count("_") <= 2 else _bn_exp.split("_", _bn_exp.count("_") - 1)[-1]
                    _will_produce_files.add(_stripped_exp.lower())
                    if _producer_tid and _stripped_exp not in _file_to_producer:
                        _file_to_producer[_stripped_exp] = _producer_tid





    for _t in cleaned_tasks:
        _prompt = _t.get("prompt", "") or ""
        _tid = _t.get("task_id", "?")
        _kind = _t.get("kind", "code")
        _framework = _t.get("framework", "")
        # 计数 deliverable 类型(基于 prompt 中出现的文件扩展名)
        # 2026-05-12 P18: 扩大检测范围 — 之前只看文档类,漏掉 .c/.h/Makefile 等源码类
        # 病因(实测 08:35 trace): bench_all_db prompt 含 6 算法 .c + .h + Makefile + .csv
        # 实际 8+ 种产物, 但旧逻辑只识别 1 种 (csv) → 阈值 ≥3 不触发 → 巨型任务通过。
        _ext_markers = {
            ".docx": "docx", ".pptx": "pptx", ".xlsx": "xlsx",
            ".pdf": "pdf", ".csv": "csv",
            # P18 新增: 源码/构建/库类
            ".c": "c_source", ".cpp": "cpp_source", ".rs": "rust_source",
            ".go": "go_source", ".java": "java_source", ".py": "py_source",
            ".h": "header", ".hpp": "header",
            "Makefile": "makefile", "CMakeLists.txt": "cmake",
        }
        _types_found = set()
        for _ext, _name in _ext_markers.items():
            if _ext in _prompt:
                _types_found.add(_name)
        # 图表/png 单独算一类(允许 helper 同时产 6 张 png — 这是单一类型多文件)
        _has_charts = any(s in _prompt for s in ("matplotlib", "图表", "chart", ".png"))
        if _has_charts:
            _types_found.add("chart")

        # 2026-05-12 P18.B: 多实体/算法/模块检测
        # 病因(实测 08:35 trace): bench_all_db prompt 列举 "AVL/SBT/RBTree/SkipList/BPTree/FractalSkip"
        # 6 个算法, 全塞给一个 helper → 该拆 6 个并行 helper, 每个一种算法。
        # 先优先识别 markdown 小节标题(### 图1 / ## 模块A), 避免把“关键约束”里的 bullet 误当实体。
        import re as _re_p18
        _heading_items = []
        for _heading in _re_p18.findall(r'^\s*#{2,6}\s+(.{1,80})$', _prompt, _re_p18.MULTILINE):
            _clean_heading = _heading.strip().strip('*').strip()
            if not _clean_heading:
                continue
            _heading_lower = _clean_heading.lower()
            if any(_skip in _heading_lower for _skip in (
                "关键约束", "约束", "要求", "注意", "说明", "交付", "输出", "需要画的图",
            )):
                continue
            _heading_items.append(_clean_heading)
        # 编号列表项: "1. XX", "1) XX", "- XX", "* XX"
        _bullet_items = _re_p18.findall(
            r"(?:^|\n)\s*(?:\d+\.|\d+\)|[-*•])\s+\*{0,2}(\w[\w +\-/]{1,40}?)\*{0,2}\s*(?:[—\-—]|$)",
            _prompt, _re_p18.MULTILINE
        )
        if len(_heading_items) >= 4:
            _enum_items = _heading_items
        else:
            _enum_items = _bullet_items
        _entity_count = len([x for x in _enum_items if len(x) >= 2])

        # 2026-05-12 P18.C: 巨型 prompt + 多 expected_outputs 检测
        _is_huge_prompt = len(_prompt) >= 2500
        _many_expected = len(_t.get("expected_outputs") or []) >= 4


        # 2026-05-11 P14.B: 依赖图检测 — prompt 引用的文件是否有 helper 产出
        # 病因(实测 trace 20:47:26): gen_paper prompt 引用 results_skiplist.csv,
        # 但 bench_skiplist 还没 outputs_complete=true → gen_paper interrupted 浪费时间。
        import re as _re_dep
        _referenced_files: set[str] = set()
        for _m in _re_dep.finditer(
            r'\b([a-zA-Z][\w\-]*\.(?:csv|tsv|json|xlsx|docx|pptx|png|jpg|svg|c|cpp|h|py|md|txt|html))\b',
            _prompt
        ):
            _referenced_files.add(_m.group(1).lower())
        # 过滤自己 expected_outputs + 通用占位
        _exp_basenames = {
            os.path.basename(x).lower()
            for x in (_t.get("expected_outputs") or [])
            if isinstance(x, str)
        }
        _template_names = {"xxx.csv", "data.csv", "output.csv", "result.csv",
                           "chart.png", "image.png", "file.txt", "input.txt",
                           "common.h", "main.c", "test.c", "example.py"}
        _truly_referenced = _referenced_files - _exp_basenames - _template_names

        _unsatisfied: list[str] = []
        for _ref in sorted(_truly_referenced):
            _ref_l = _ref.lower()
            _found = (
                _ref_l in _completed_files or
                _ref_l in _will_produce_files or
                any(f.endswith("_" + _ref_l) for f in _completed_files) or
                any(f.endswith("_" + _ref_l) for f in _will_produce_files)
            )
            if not _found:
                _unsatisfied.append(_ref)

        if _unsatisfied:
            granularity_warnings.append({
                "task_id": _tid,
                "issue": "unsatisfied_dependency",
                "severity": "medium",
                "details": (
                    f"task '{_tid}' references {len(_unsatisfied)} file(s) that are not present in completed helper outputs "
                    f"and are not declared in this spawn batch expected_outputs: {_unsatisfied[:5]}"
                    + (f" (plus {len(_unsatisfied)-5} more)" if len(_unsatisfied) > 5 else "")
                    + "\n\n引用文件缺少生产者；先补资源或调整依赖顺序。"
                ),
                "unsatisfied_files": _unsatisfied,
                "observed_dependency_gap_fact": (
                    "The referenced filenames are not present in completed helper outputs and are not declared as expected outputs "
                    "in this spawn batch. They may be real missing dependencies, non-material template text, or resources that "
                    "need an explicit producer/request path.\n\n"
                    "观察到引用文件暂无已完成产物或本批生产者；可能是真依赖、模板文本或需显式补资源。"
                ),
            })

        # 2026-05-15 P64: 批内顺序违规检测 — consumer 引用了 producer 的产物, 但两者同批
        # spawn (而非 producer 已 outputs_complete=true)。对头文件 / 接口文件 / Makefile 等
        # "编译期必需" 类依赖, 这是硬错误 — consumer 会立刻 gcc 失败而非"等到 producer 完成"。
        # 病因(实测 05-15 16:28): 主线程同批 spawn comp_infra (产 compress.h, benchmark.h,
        # Makefile) + comp_huffman/lz77/lz78/lzw/bwt/custom (用 compress.h)。algorithm helper
        # 立刻 gcc → compress.h: No such file → 6 个 helper 全部失败需要重试, 浪费 3 分钟 +
        # 一轮 LLM 推理。
        # 修法: 检测 consumer 引用文件 ∈ 同批 producer 的 expected_outputs, 且文件名是
        # "编译期必需"类型 (.h / Makefile / .json schema 等), 升级为 high severity 警告并
        # 显式说明应改成 串行 (先 producer, 等完成, 再 consumer)。
        _intra_batch_prereq: list[tuple[str, str]] = []  # (ref_file, producer_tid)
        _BUILD_TIME_EXT = {".h", ".hpp", ".hxx", ".pyi", ".d.ts", ".proto"}
        _BUILD_TIME_NAMES = {"makefile", "cmakelists.txt", "build.gradle", "cargo.toml",
                              "package.json", "go.mod", "compose.yaml", "compose.yml"}
        for _ref in sorted(_truly_referenced):
            _ref_l = _ref.lower()
            # 看是否在本批 producer 的 expected_outputs 里 (且不是已 outputs_complete)
            _producer = _file_to_producer.get(_ref_l)
            if not _producer:
                # 也试 stripped 形式
                for _f, _p in _file_to_producer.items():
                    if _f.endswith("_" + _ref_l) or _ref_l.endswith("_" + _f):
                        _producer = _p
                        break
            if not _producer or _producer == _tid:
                continue  # 自产或非批内 producer
            # 已完成 → 不算违规
            if _ref_l in _completed_files:
                continue
            # 判断是否"编译期必需"
            _ref_basename = _ref_l.split("/")[-1]
            _is_build_time = (
                _ref_basename in _BUILD_TIME_NAMES
                or any(_ref_basename.endswith(_ext) for _ext in _BUILD_TIME_EXT)
            )
            if _is_build_time:
                _intra_batch_prereq.append((_ref, _producer))

        if _intra_batch_prereq:
            _prereq_pairs = [f"`{f}` (producer={p})" for f, p in _intra_batch_prereq[:5]]
            _producer_tids = sorted({p for _, p in _intra_batch_prereq})
            granularity_warnings.append({
                "task_id": _tid,
                "issue": "intra_batch_ordering_violation",
                "severity": "high",
                "details": (
                    f"task '{_tid}' uses {len(_intra_batch_prereq)} build-time prerequisite file(s) "
                    f"(headers, Makefiles, schemas, or equivalent) whose producer is in the same spawn batch "
                    f"and has not completed yet: {', '.join(_prereq_pairs)}.\n\n"
                    "编译期依赖必须先存在；先完成框架/接口再派消费者。"
                ),
                "unsatisfied_files": [f for f, _ in _intra_batch_prereq],
                "producer_task_ids": _producer_tids,
                "observed_dependency_order_fact": (
                    f"Build-time prerequisite producer task(s) {_producer_tids} are in the same spawn batch as consumer task '{_tid}' "
                    "and have not completed. Headers, Makefiles, schemas, or equivalent files must exist before compile, test, or import consumers use them.\n\n"
                    "观察到编译期依赖与消费者同批且未完成；接口/框架文件需先存在。"
                ),
            })

        # Repeated same-task attempts are a strong planning signal, not a hard
        # dispatcher stop. The main model must still be able to choose whether
        # to continue the same task, upgrade mode, split work, or report the
        # verified gap. Returning an error here caused recoverable second/third
        # attempts to terminate before the model could adjust.
        _prev_attempts = _count_task_id_attempts(trace_id, _tid)
        if _prev_attempts >= 3:
            _historical_ok = [
                e for e in _ledger
                if e.get("task_id") == _tid and e.get("outputs_complete") is True
            ]
            if not _historical_ok:
                granularity_warnings.append({
                    "task_id": _tid,
                    "issue": "resume_attempt_cap",
                    "severity": "high",
                    "details": (
                        f"task '{_tid}' has been attempted {_prev_attempts} times without outputs_complete=true. "
                        "Treat this as a planning checkpoint: inspect the latest report, "
                        "then either resume with a sharper prompt, fix routing/resources/dependencies, split the task, "
                        "or use mode='hard' as a stricter same-kind retry after the root cause is addressed.\n\n"
                        "多次未完成时先读最新报告并修根因；hard 是同类严谨续作，不是跨类型修复。"
                    ),
                    "previous_attempts": _prev_attempts,
                    "observed_retry_history_fact": (
                        f"task_id='{_tid}' has repeated attempts without outputs_complete=true. The latest report and acceptance evidence "
                        "are needed to distinguish a useful same-task resume from a kind/resource/dependency/path/boundary problem. "
                        "Hard mode is same-kind stricter resource discipline, not broader tool access.\n\n"
                        "观察到同一任务多次未完成；需结合最新报告和验收证据判断续跑、修流程或拆分。"
                    ),
                })

        # 2026-05-11 P1.2: granularity_warnings 结构化(参照 Claude Code hookSpecificOutput)
        # 只向模型暴露结构化事实。避免 suggested_* 字段把程序启发误读成决策。
        # 多 deliverable 检测
        if len(_types_found) >= 3:
            granularity_warnings.append({
                "task_id": _tid,
                "issue": "too_many_deliverables",
                "severity": "high",
                "details": (
                    f"task '{_tid}' (kind={_kind}, prompt {len(_prompt)} chars) asks one helper for "
                    f"{len(_types_found)} deliverable types ({', '.join(sorted(_types_found))}). "
                    "Use separate focused helpers when outputs require different skills or validation paths.\n\n"
                    "一个 helper 不宜同时包揽多类产物；按产物/技能拆分。"
                ),
                "observed_parallel_boundary_fact": (
                    "The prompt includes several deliverable families that may require different skills or validation paths. "
                    "Independent producers can run in parallel when their inputs are already available; consumers need the "
                    "required files or resource requests before assembly.\n\n"
                    "观察到多类产物和多条验收路径；是否拆分由主进程结合依赖判断。"
                ),
                "deliverable_types": sorted(_types_found),
            })

        # 2026-05-15 P63: 多目标编译 + 集成运行检测(comp_bench 死循环根因)
        # 实测教训(05-15 16:57-18:35 comp_bench): prompt 让 1 个 helper 编译 6 种算法 + 链接 +
        # 跑统一 benchmark — 任何一个算法在某规模崩溃, 整个 bench.exe segfault → helper 卡进
        # "改算法→重编→重跑→还崩" 死循环, 中断 5 次共 1.5 小时, 最终被废弃。
        # 检测模式: prompt 含 (a) 编译命令一次性 ≥4 个不同 .c/.cpp 文件; 或 (b) OBJS = ...
        # 行包含 ≥4 个 .o 目标; 或 (c) Makefile 类多模块构建 + 运行可执行文件 + 含 "round_trip" /
        # "崩溃" / "失败跳过" 等容错关键词(暗示 LLM 已知道某些子模块可能崩, 但仍想一次跑通)。
        import re as _re_p63
        _gcc_multi = bool(_re_p63.search(
            r'\bg(?:cc|\+\+)\b[^\n]{1,200}?(?:[\w\-]+\.(?:c|cpp|cc)\s+){3,}[\w\-]+\.(?:c|cpp|cc)',
            _prompt,
        ))
        _objs_multi = False
        for _objs_m in _re_p63.finditer(r'OBJS\s*=\s*([^\n]+)', _prompt):
            _o_count = len(_re_p63.findall(r'[\w\-]+\.o\b', _objs_m.group(1)))
            if _o_count >= 4:
                _objs_multi = True
                break
        _has_integration_run = bool(_re_p63.search(
            r'(?:\./[\w_]+\.exe|\./bench|\./main|\./test)\b', _prompt
        ))
        _has_failure_keywords = any(_k in _prompt for _k in (
            "round_trip", "崩溃", "失败跳过", "跳过它", "segfault", "core dump",
            "如果某算法", "如果某个算法", "if any algorithm",
        ))
        # 触发条件: (多目标编译 或 多 OBJS) 且 (含集成运行 或 含失败容错关键词)
        _is_multi_target_integration = (
            (_gcc_multi or _objs_multi) and (_has_integration_run or _has_failure_keywords)
        )
        if _is_multi_target_integration:
            granularity_warnings.append({
                "task_id": _tid,
                "issue": "multi_target_build_should_split",
                "severity": "high",
                "details": (
                    f"task '{_tid}' appears to compile or integrate >=4 independent modules in one helper "
                    f"(gcc_multi={_gcc_multi}, OBJS_multi={_objs_multi}). "
                    "When independent modules can fail separately, isolate their build/test loops so one failure "
                    "does not consume the whole task.\n\n"
                    "多模块编译/测试应隔离；避免一个模块失败拖住整批。"
                ),
                "observed_parallel_boundary_fact": (
                    "The detected build or integration command mentions multiple modules that can fail independently. "
                    "Module-level build/test evidence can reduce cross-module recovery coupling before any later merge/report work.\n\n"
                    "观察到多个可能独立失败的模块；模块级验证可降低恢复耦合。"
                ),
                "detected_signals": {
                    "gcc_multi_source": _gcc_multi,
                    "objs_multi": _objs_multi,
                    "integration_binary": _has_integration_run,
                    "failure_keywords": _has_failure_keywords,
                },
            })

        # 2026-05-12 P18.B: 多实体列举检测(prompt 列举 ≥ 5 个算法/模块/算法名)
        # 实测教训(08:35 trace): bench_all_db 列举 6 种算法 (AVL/SBT/RBTree/SkipList/BPTree/FractalSkip)
        # → 主线程把"工作流分解"误解为"一个 helper 全做"。
        if _entity_count >= 5:
            granularity_warnings.append({
                "task_id": _tid,
                "issue": "overly_broad_entity_list",
                "severity": "high",
                "details": (
                    f"task '{_tid}' lists {_entity_count} independent entities "
                    "(algorithms, modules, or components) for one helper. "
                    "Use entity-level helpers when the entities can be implemented or verified independently.\n\n"
                    "多个独立实体应按实体拆分 helper。"
                ),
                "entity_items": [x[:30] for x in _enum_items[:6]],
                "observed_parallel_boundary_fact": (
                    "The prompt lists several entities that appear independently implementable or verifiable. "
                    "Concrete inputs, output filenames, and acceptance checks are needed for whichever boundary the main thread chooses.\n\n"
                    "观察到多个实体；所选边界需要明确输入、产物和验收事实。"
                ),
            })

        # 2026-05-12 P18.C: 巨型 prompt 警告(单 prompt ≥ 2500 字符 或 expected_outputs ≥ 4)
        if (_is_huge_prompt or _many_expected) and not _framework:
            granularity_warnings.append({
                "task_id": _tid,
                "issue": "overly_broad_task",
                "severity": "medium",
                "details": (
                    f"task '{_tid}' may be too broad: prompt={len(_prompt)} chars, "
                    f"expected_outputs={len(_t.get('expected_outputs') or [])}. "
                    "A helper should have one clear work boundary and a compact acceptance target.\n\n"
                    "helper 边界应清晰，验收目标应紧凑。"
                ),
                "observed_parallel_boundary_fact": (
                    "The prompt length or expected output count is high for one helper. This may indicate multiple "
                    "work boundaries, or it may be justified by a tightly coupled task; the main thread has the dependency context.\n\n"
                    "观察到单 helper 输入或产物较重；是否拆分取决于耦合关系。"
                ),
            })

        # 2026-05-12 P18: single_helper_too_wide 检测
        # 病因(实测 08:35 trace): 主线程派 1 个 helper 'bench_all_db' prompt 自报
        # "6 种算法 × 5 分布 × 5 规模 × 6 操作 × 3 重复 = 约 2700 组 benchmark"。
        # 一个 helper 串行做所有事, 违反铁律 2 (Parallelism). 应拆 6 个并行 helper.
        # too_many_deliverables 只看 5 种扩展名扩展, 漏检 6 个 .c 文件这种"多算法"枚举。
        # P18 直接扫 prompt 中的"枚举模式": "N 种 X / N×M = K 组" 等。
        import re as _re_p18
        _enum_signals_set: set[str] = set()  # 去重
        # 模式 1: "N 种算法/N 种 X" 列举(N >= 3)
        for _m in _re_p18.finditer(r'(\d+)\s*(?:种|个)\s*([\w\u4e00-\u9fa5]+)', _prompt):
            _n = int(_m.group(1))
            if 3 <= _n <= 50:  # 合理范围
                _enum_signals_set.add(f"{_n}种{_m.group(2)[:8]}")
        # 模式 2: "N×M×K = X 组/次" 乘积
        for _m in _re_p18.finditer(r'(\d+)\s*[×x*]\s*(\d+)(?:\s*[×x*]\s*(\d+))?', _prompt):
            try:
                _a = int(_m.group(1)); _b = int(_m.group(2))
                _c = int(_m.group(3)) if _m.group(3) else 1
                _product = _a * _b * _c
                if _product >= 50:  # 显然太多
                    _enum_signals_set.add(f"{_a}×{_b}" + (f"×{_c}" if _c > 1 else "") + f"={_product}")
            except (ValueError, TypeError):
                pass
        # 模式 3: 列表枚举 "1. X\n2. Y\n3. Z..." 主类目 ≥ 4 个
        _numbered_items = _re_p18.findall(r'^\s*\d+[.\)、]\s+\S{2,40}', _prompt, _re_p18.MULTILINE)
        if len(_numbered_items) >= 4:
            _enum_signals_set.add(f"列表{len(_numbered_items)}项")
        _enum_signals = sorted(_enum_signals_set)[:8]  # 最多 8 个不同信号
        # 模式 4: prompt 极长 + 单 task(>= 2000 字符强信号)
        _is_very_long = len(_prompt) >= 2000

        # 触发条件: 任一枚举信号 ≥ 2 个 OR prompt 极长且有 ≥ 1 枚举信号
        if (len(_enum_signals) >= 2 or
            (_is_very_long and len(_enum_signals) >= 1)):
            # 2026-05-21: single_helper_too_wide (high) 与 overly_broad_task (medium)
            # 对同一 task 会同时触发(都判"prompt 过长该拆"),产生两条语义重叠建议。
            # high 这条更具体(带并行拆分示例)且覆盖 medium 全部语义 → 去掉同 task 的 medium,
            # 避免主线程收到两条重复的"任务过宽"提示。
            granularity_warnings[:] = [
                _w for _w in granularity_warnings
                if not (_w.get("task_id") == _tid and _w.get("issue") == "overly_broad_task")
            ]
            granularity_warnings.append({
                "task_id": _tid,
                "issue": "single_helper_too_wide",
                "severity": "high",
                "details": (
                    f"task '{_tid}' (kind={_kind}, prompt {len(_prompt)} chars) has broad-work signals: "
                    f"{_enum_signals[:5]}. Assign independent units to separate helpers so each unit has clear ownership, "
                    "recovery, and verification evidence.\n\n"
                    "检测到多独立单元；应拆成可并行、可验收的小任务。"
                ),
                "enum_signals": _enum_signals,
                "observed_parallel_boundary_fact": (
                    "The detected units appear independently checkable from the prompt. If the active task evidence "
                    "supports that boundary, separate helper ownership can reduce recovery coupling; if not, keep the "
                    "current helper boundary and explain the coupling in dispatch_reason.\n\n"
                    "观察到多个可能独立验收的单元；是否拆分由主进程结合任务证据决定。"
                ),
            })

        # kind=edit + 含 matplotlib/算法实现 → kind 不对
        if _kind == "edit" and any(s in _prompt for s in (
            "matplotlib", "用 Python 实现", "算法实现", "benchmark", "编译", "数据处理"
        )):
            # 2026-05-11 P10: 区分两种"edit + 代码工作"场景
            # - 含 matplotlib/画图 → 推荐 draw helper(专用绘图)
            # - 含 benchmark/算法实现 → 推荐 code helper
            _is_draw_task = any(s in _prompt for s in (
                "matplotlib", "plt.", "画图", "绘图", "chart", "plot", "可视化"
            ))
            granularity_warnings.append({
                "task_id": _tid,
                "issue": "kind_mismatch",
                "severity": "high",
                "details": (
                    f"task '{_tid}' uses kind='edit' while the prompt asks for substantive code or data work "
                    f"(for example plotting code, benchmarks, compilation, or data processing). "
                    "Use a helper kind whose tool surface matches the work.\n\n"
                    "edit 负责文档组装；代码、计算、绘图应交给对应 helper。"
                ),
                "observed_helper_kind_name": "draw" if _is_draw_task else "code",
                "observed_tool_surface_fact": (
                    (
                        "The prompt names chart/image generation work that requires rendering and image-output checks.\n\n"
                        "提示词包含绘图/图片产物事实，需要能渲染并验收图像。"
                    ) if _is_draw_task
                    else "The prompt names implementation, computation, benchmark, compile, or test work that requires code-runner evidence.\n\n提示词包含代码/计算/测试事实，需要代码运行证据。"
                ),
            })
        # 2026-05-11 P10: kind=code + 任务核心是画图 → 推荐 draw
        # code helper 写画图也容易凭印象写错算法名(实测教训), draw 强制 3 步流程
        if _kind == "code" and any(s in _prompt for s in (
            "matplotlib", "plt.savefig", "生成 PNG", "生成图表", "绘制", "画 N 张图"
        )) and not any(s in _prompt for s in (
            "benchmark", "算法实现", "实现 .c", "编译", ".c)", "C 实现"
        )):
            # 只画图,不含其他代码工作 → draw 最合适
            granularity_warnings.append({
                "task_id": _tid,
                "issue": "consider_draw_kind",
                "severity": "medium",
                "details": (
                    f"task '{_tid}' kind='{_kind}' is primarily a chart-generation task. "
                    "The draw helper is a better fit because it validates source labels before rendering and produces image artifacts with focused checks.\n\n"
                    "核心是绘图时优先使用 draw，避免标签或字段错配。"
                ),
                "observed_helper_kind_name": "draw",
                "observed_tool_surface_fact": (
                    "The main deliverable appears to be an image or chart. The next delegation should preserve the "
                    "real source labels, plotted fields, and rendered output evidence in the helper envelope if the "
                    "main thread chooses this boundary.\n\n"
                    "观察到主要产物像图像/图表；若按此边界派发，应保留真实数据标签、字段和图像验收事实。"
                ),
            })
        _has_doc_output = _has_office_document_output(_prompt, _t.get("expected_outputs") or [])
        if _has_doc_output and _kind != "edit":
            granularity_warnings.append({
                "task_id": _tid,
                "issue": "kind_mismatch_doc",
                "severity": "high",
                "details": (
                    f"task '{_tid}' kind='{_kind}' declares or requests Office-style deliverables "
                    "(docx, pptx, or xlsx). Document assembly belongs to the edit helper so outputs can be produced and validated through the document tools.\n\n"
                    "Office/正式文档产物应由 edit 负责。"
                ),
                "observed_helper_kind_name": "edit",
                "observed_tool_surface_fact": (
                    "The requested deliverable is an Office-style document. The next delegation should preserve the "
                    "section outline, source evidence, expected output filenames, and validation checks if the main "
                    "thread chooses a document-assembly boundary.\n\n"
                    "观察到 Office/正式文档产物；若按文档组装边界派发，应保留大纲、证据、文件名和验收。"
                ),
            })

        # 2026-05-11 P11: edit 派遣 prompt 含图表但**未列出具体图文件名** → 警告
        # 病因(实测 18:46): paper helper kind=edit 收到 "嵌入对应图表" 这种模糊 prompt,
        # 没指定具体文件名, helper 不知道用哪些图, 又看到 CSV 在 → 自己重画了。
        # 修法: 主线程 prompt 必须**显式列文件名** (基于 ledger 里其他 helper 的产物),
        # 否则给 edit helper 警告 "你 prompt 不清楚, 我可能自己重画"。
        if _kind == "edit" and _has_doc_output:
            _mentions_chart = any(s in _prompt for s in (
                "图表", "chart", "图片", "插图", "插入图", "嵌入图", "PNG", "png", ".png"
            ))
            # 看 prompt 是否含具体 .png 文件名 (chart1_xxx.png / xxx_yyy.png)
            import re as _re_p
            _has_specific_pngs = bool(_re_p.search(
                r'\b[\w\-]+\.png\b', _prompt
            ))
            if _mentions_chart and not _has_specific_pngs:
                granularity_warnings.append({
                    "task_id": _tid,
                    "issue": "edit_chart_prompt_vague",
                    "severity": "medium",
                    "details": (
                        f"task '{_tid}' kind='edit' mentions charts or embedded images but does not list concrete .png filenames. "
                        "The edit helper should assemble verified image artifacts, not rediscover or recreate charts from ambiguous instructions.\n\n"
                        "嵌图任务要明确图片文件名；edit 只组装已验证图片。"
                    ),
                    "observed_missing_artifact_reference_fact": (
                        "The prompt mentions charts or embedded images but no concrete image filenames. If image "
                        "artifacts already exist, pass their exact paths and target sections; if they do not exist, "
                        "record that resource gap before document assembly.\n\n"
                        "观察到嵌图要求缺少具体图片文件名；已有图片应传精确路径，未有则先记录资源缺口。"
                    ),
                })

        # 2026-05-11 P1.5: "懒惰委派" anti-pattern 运行时检测
        # 参照 Claude Code coordinator system prompt §5 "Writing Worker Prompts":
        # > "Never write 'based on your findings'. These phrases delegate
        # >  understanding to the worker instead of doing it yourself."
        # ROUND2 §5.4 已写文字约束, 但运行时 prompt 含这类短语 LLM 不会发现.
        # 这里加结构化 warning, LLM 看到立刻知道要补具体 spec (文件路径/行号/根因).
        _LAZY_DELEGATION_PATTERNS = (
            "based on your findings",   # 英文
            "based on the findings",
            "based on the research",
            "based on the analysis",
            "根据上面的研究",          # 中文
            "根据前面的分析",
            "基于你的发现",
            "基于上面",
            "参考你的发现",
            "参考前面的分析",
            "你之前发现的问题",
            "把刚才发现的",
            "针对上述",
        )
        _prompt_lower = _prompt.lower()
        _matched_patterns = [
            p for p in _LAZY_DELEGATION_PATTERNS
            if p in _prompt_lower
        ]
        if _matched_patterns:
            granularity_warnings.append({
                "task_id": _tid,
                "issue": "lazy_delegation",
                "severity": "high",
                "details": (
                    f"task '{_tid}' prompt contains vague delegation phrase(s): {', '.join(_matched_patterns[:3])}. "
                    "Rewrite it with concrete inputs, evidence, target files, acceptance checks, and the intended dependency position.\n\n"
                    "委派提示要具体交付输入、证据、目标文件和验收条件。"
                ),
                "observed_prompt_specificity_fact": (
                    "The helper prompt relies on prior findings instead of naming concrete paths, facts, constraints, success criteria, "
                    "and required evidence. Helper context is isolated from the main thread except for the dispatched envelope and resources.\n\n"
                    "观察到委派提示依赖前文；helper 只可靠接收派发信封和资源。"
                ),
                "matched_patterns": _matched_patterns,
            })

    # 2026-05-15 P92: batch-level "兄弟 benchmark 任务的测试规格一致性"检测
    # 病因(实测 00:00 排序论文 trace): plan 派 6 个 sort_* helper, 各产 results_<algo>.csv
    # 做横向对比。但 3 个 helper prompt 用 "partial_50%/duplicates_100" 分布, 2 个用
    # "zipf/few_unique" 分布, 1 个未知。CSV 列不一致, 论文图只能用 random/sorted/reversed
    # 3 列, 其他列大半空缺。用户拿到的对比图缺一半数据。
    # 修法: 检测 3+ task 的 expected_outputs 命名同模式 (e.g. results_*.csv 是兄弟任务),
    # 扫描各自 prompt 中的"测试维度关键词", 不一致 → 高优警告。
    import re as _re_p92
    # 收集兄弟任务: 按 (expected_output 前缀, 扩展名) 分组
    _sibling_groups: dict[tuple[str, str], list[dict]] = {}
    for _t_p92 in cleaned_tasks:
        _outs = _t_p92.get("expected_outputs") or []
        for _o in _outs:
            _o_base = (_o or "").replace("\\", "/").split("/")[-1].lower()
            _o_name, _o_ext = (
                _o_base.rsplit(".", 1) + [""]
            )[:2] if "." in _o_base else (_o_base, "")
            _o_ext = ("." + _o_ext) if _o_ext else ""
            _parts_p92 = _o_name.split("_")
            if len(_parts_p92) >= 2 and _o_ext in (".csv", ".json", ".tsv", ".parquet"):
                _prefix_p92 = _parts_p92[0]
                # 长度过短的前缀("a"/"b") 信息不足, 至少 4 字母
                if len(_prefix_p92) >= 4:
                    _sibling_groups.setdefault((_prefix_p92, _o_ext), []).append(_t_p92)

    # 测试维度关键词分组 (互斥群)
    _BENCH_DIM_GROUPS = [
        # 数据分布
        {"random", "随机"},
        {"sorted", "已序", "有序"},
        {"reversed", "逆序"},
        {"partial", "partial_50", "partial_80", "部分有序", "部分排序"},
        {"duplicates", "duplicates_100", "高重复", "重复多"},
        {"zipf"},
        {"few_unique", "few unique", "少量唯一", "稀疏唯一"},
        {"sawtooth", "锯齿"},
        {"organpipe", "organ pipe"},
    ]

    def _extract_bench_dims(prompt_text: str) -> set[str]:
        """从 prompt 文本提取出现的测试维度组 label"""
        _txt = prompt_text.lower()
        _dims_present = set()
        for _grp in _BENCH_DIM_GROUPS:
            for _kw in _grp:
                if _kw in _txt:
                    # 用第一个 keyword 当组的代表 label
                    _dims_present.add(sorted(_grp)[0])
                    break
        return _dims_present

    for (_prefix_p92, _ext_p92), _sibling_tasks in _sibling_groups.items():
        if len(_sibling_tasks) < 3:
            # 2026-05-15 P94: 即使本批 < 3 兄弟, 也可能与 registry 中的历史兄弟跨批不一致
            # 例如批 1: quick/merge/heap/acms (4 个, P92 catch), 批 2: timsort/insertion (2 个,
            # P92 单看不报, 但 timsort 用 zipf/few_unique 跟批 1 的 partial/duplicates 不一致)。
            # 这里也跑 cross-batch 检测。
            pass
        # 收集本批各兄弟的测试维度
        _dim_by_task: dict[str, set[str]] = {}
        for _bt in _sibling_tasks:
            _bt_tid = _bt.get("task_id", "?")
            _bt_prompt = _bt.get("prompt", "") or ""
            _dim_by_task[_bt_tid] = _extract_bench_dims(_bt_prompt)

        # 2026-05-15 P94: 查 registry 看历史同模式的兄弟
        _historical_dims: dict[str, set[str]] = {}
        _trace_registry = _bench_dims_registry.get(trace_id, {})
        for _hist_tid, _hist_entry in _trace_registry.items():
            if _hist_entry.get("prefix") == _prefix_p92 and _hist_entry.get("ext") == _ext_p92:
                if _hist_tid not in _dim_by_task:  # 不重复本批的
                    _historical_dims[_hist_tid] = set(_hist_entry.get("dims") or [])

        # 合并本批 + 历史
        _all_sibling_dims = {**_dim_by_task, **_historical_dims}
        _total_siblings = len(_all_sibling_dims)
        if _total_siblings < 3:
            # 本批 + 历史一起也 < 3 兄弟, 不算
            continue

        # 计算所有维度的并集
        _all_dims = set()
        for _ds in _all_sibling_dims.values():
            _all_dims |= _ds
        # 检查每个任务覆盖度
        _missing_by_task: dict[str, set[str]] = {}
        for _tid_x, _ds in _all_sibling_dims.items():
            _missing = _all_dims - _ds
            if _missing:
                _missing_by_task[_tid_x] = _missing
        if _missing_by_task and len(_all_dims) >= 3:
            # 标记哪些来自历史
            _hist_task_set = set(_historical_dims.keys())
            _inconsistent_parts = []
            for _tid_x, _m in list(_missing_by_task.items())[:6]:
                _from = " (历史批)" if _tid_x in _hist_task_set else ""
                _inconsistent_parts.append(f"{_tid_x}{_from}缺[{','.join(sorted(_m))}]")
            _inconsistent_summary = ", ".join(_inconsistent_parts)
            _is_cross_batch = bool(_hist_task_set)
            granularity_warnings.append({
                "task_id": "<batch>",
                "issue": (
                    "sibling_bench_inconsistent_dimensions_cross_batch"
                    if _is_cross_batch else
                    "sibling_bench_inconsistent_dimensions"
                ),
                "severity": "high",
                "details": (
                    f"{'Cross-batch' if _is_cross_batch else 'In-batch'} sibling tasks produce "
                    f"{_prefix_p92}_*{_ext_p92} for comparison, but their benchmark dimensions differ. "
                    f"Expected union dimensions: {sorted(_all_dims)}; {len(_missing_by_task)} task(s) miss coverage: "
                    f"{_inconsistent_summary}. Align the matrix before drawing conclusions or generating charts.\n\n"
                    "横向对比必须统一测试维度，否则表格和图表不可比。"
                ),
                "sibling_task_ids_in_batch": [t.get("task_id", "?") for t in _sibling_tasks],
                "sibling_task_ids_in_history": sorted(_hist_task_set),
                "missing_dims_by_task": {k: sorted(v) for k, v in _missing_by_task.items()},
                "all_dims": sorted(_all_dims),
                "observed_benchmark_matrix_fact": {
                    "union_dimensions": sorted(_all_dims),
                    "cross_batch": _is_cross_batch,
                    "history_task_ids": sorted(_hist_task_set),
                    "schema_note": "Sibling comparison outputs need compatible dimensions, N values, repetition policy, and CSV schema before charting or conclusions.",
                },
            })

        # 2026-05-15 P94: 把本批的 dims 存进 registry, 供后续批 spawn 时跨批检测
        if trace_id not in _bench_dims_registry:
            _bench_dims_registry[trace_id] = {}
        for _tid_x, _ds in _dim_by_task.items():
            if _ds:  # 只存有 dims 的
                _bench_dims_registry[trace_id][_tid_x] = {
                    "dims": _ds,
                    "prefix": _prefix_p92,
                    "ext": _ext_p92,
                    "ts": time.monotonic(),
                }
        # GC: 限制 registry 总条目 (每 trace 最多 30)
        if len(_bench_dims_registry[trace_id]) > 30:
            # 删最早的 — 但没存 ts 排序键, 简单 pop 头
            _registry_list = list(_bench_dims_registry[trace_id].keys())
            for _old_tid in _registry_list[:-30]:
                _bench_dims_registry[trace_id].pop(_old_tid, None)


    # 2026-05-12 P16.A: 工作流完整性 batch-level 检测
    # 用户要"文档/图"但本批 + 历史 ledger 都只有 code → 工作流缺失
    if (_user_wants_doc or _user_wants_chart) and not _batch_has_edit and not _batch_has_draw:
        # 检查 ledger 历史: 之前是否已经派过 edit/draw 完成的?
        if not _had_edit_done and _user_wants_doc:
            granularity_warnings.append({
                "task_id": "<batch>",
                "issue": "workflow_incomplete_no_edit",
                "severity": "high",
                "details": (
                    f"The user requested document-style deliverables, but this batch has {len(cleaned_tasks)} task(s) "
                    f"with kinds {sorted(_batch_kinds)} and no edit helper; the ledger also has no completed edit output. "
                    "The user-visible document artifact currently has no completed assembly owner.\n\n"
                    "用户要文档/论文/PPT，但当前批次和历史记录未显示已完成的正式文档装配产物。"
                ),
                "observed_workflow_gap_fact": (
                    "The current batch and completed ledger do not show a document assembly owner for the requested user-facing artifact. "
                    "If this batch only gathers evidence, the plan still needs a later owner and acceptance evidence.\n\n"
                    "观察到文档交付物暂无完成的装配归属；若当前只取证，计划需保留后续归属。"
                ),
            })
        if not _had_draw_done and _user_wants_chart:
            granularity_warnings.append({
                "task_id": "<batch>",
                "issue": "workflow_incomplete_no_draw",
                "severity": "medium",
                "details": (
                    f"The user requested charts or visualizations, but this batch has {len(cleaned_tasks)} task(s) "
                    f"with kinds {sorted(_batch_kinds)} and no completed draw helper in the ledger. "
                    "The requested visual artifact currently has no completed producer.\n\n"
                    "用户要图表，但当前批次和历史记录未显示已完成的可验证图片产物。"
                ),
                "observed_workflow_gap_fact": (
                    "The current batch and completed ledger do not show a visual producer for the requested chart or image artifact. "
                    "A future visual boundary needs concrete data paths, image filenames, and validation evidence.\n\n"
                    "观察到图表/图片交付物暂无完成生产者；后续视觉边界需数据路径、文件名和验收事实。"
                ),
            })

    # 2026-05-12 P16.C: batch 全无 expected_outputs 警告
    # 病因(实测 23:46 trace): outputs_check / quality_warnings 0 次出现,
    # 因为主线程从不传 expected_outputs → Tier 1.C 系统验收完全失效。
    # 修法: batch 中 ≥ 3 task 但全部没传 expected_outputs → 强警告。
    if len(cleaned_tasks) >= 2:
        _no_exp_count = sum(
            1 for _t in cleaned_tasks
            if not _t.get("expected_outputs")
        )
        if _no_exp_count == len(cleaned_tasks):
            granularity_warnings.append({
                "task_id": "<batch>",
                "issue": "expected_outputs_missing",
                "severity": "high",
                "details": (
                    f"All {len(cleaned_tasks)} task(s) in this batch omit expected_outputs. "
                    "The system cannot determine outputs_complete or run output-oriented quality checks reliably.\n\n"
                    "缺少 expected_outputs 会削弱自动验收。"
                ),
                "observed_acceptance_gap_fact": (
                    "No task in this batch declares expected_outputs. Output-oriented completion checks, missing-output facts, "
                    "and fan-in decisions are less reliable without concrete filenames.\n\n"
                    "观察到整批缺少 expected_outputs；自动验收和汇总判断会变弱。"
                ),
            })

    # 2026-05-15 P77: chained dependency over-fanout 检测
    # 病因(实测 rc_filter trace 508s): 用户要"RC 滤波器实验: Word + Excel + Bode 图",
    #   主线程拆 3 helper (rc_excel_data + rc_bode_plot 并行, 然后 rc_report 串行)。
    #   但 plot 用 excel 数据、report 引用 xlsx 列名 + png — 强依赖链。
    #   3 次 spawn 冷启动 + 2 次批间决策 + 3 次交付/合并 = 浪费 ~45-60s,
    #   不如单 helper kind=edit 内部串行做完。
    # 修法: 检测同主题 (task_ids 共享名词前缀 / 总产物 ≤4 / 后任务 prompt 引用前任务产物)
    #   → 建议改单 helper。不硬拒(可能确实独立), 只作 medium severity warning。
    if 2 <= len(cleaned_tasks) <= 4:
        _all_tids = [_t.get("task_id", "") for _t in cleaned_tasks]
        # 检测共享前缀: 第一个下划线前的部分
        _prefixes = set()
        for _tid in _all_tids:
            if "_" in _tid:
                _prefixes.add(_tid.split("_", 1)[0])
            else:
                _prefixes.add(_tid)
        _shared_prefix = len(_prefixes) == 1 and len(_all_tids) >= 2

        # 检测 cross-task 引用 (后 task 的 prompt 提到前 task 的 expected_output basename)
        _has_cross_ref = False
        _ref_pairs: list[tuple[str, str, str]] = []
        for _i, _t_consumer in enumerate(cleaned_tasks):
            _consumer_prompt = (_t_consumer.get("prompt") or "").lower()
            for _j, _t_producer in enumerate(cleaned_tasks):
                if _i == _j:
                    continue
                for _eo in (_t_producer.get("expected_outputs") or []):
                    _bn = os.path.basename(str(_eo)).lower()
                    if _bn and len(_bn) > 4 and _bn in _consumer_prompt:
                        _has_cross_ref = True
                        _ref_pairs.append((
                            _t_consumer.get("task_id", "?"),
                            _t_producer.get("task_id", "?"),
                            _bn,
                        ))

        # 总产物数 (用于判断"小批"还是"大批")
        _total_outputs = sum(
            len(_t.get("expected_outputs") or []) for _t in cleaned_tasks
        )

        # 触发条件: 共享前缀 + 跨 task 引用 + 总产物 ≤ 4
        if _shared_prefix and _has_cross_ref and _total_outputs <= 4:
            granularity_warnings.append({
                "task_id": "<batch>",
                "issue": "over_fanout_chained_deps",
                "severity": "medium",
                "details": (
                    f"This batch has {len(cleaned_tasks)} task(s) sharing prefix '{list(_prefixes)[0]}_*' "
                    f"with cross-task output dependencies ({len(_ref_pairs)} reference(s)): "
                    f"{[(c, p, f) for c, p, f in _ref_pairs[:3]]}; total outputs={_total_outputs}. "
                    "When artifacts form a short dependency chain, a single helper may be more reliable than fan-out.\n\n"
                    "小型强依赖链可合并为单 helper，避免跨 helper 传递噪声。"
                ),
                "task_ids": _all_tids,
                "cross_ref_pairs": _ref_pairs[:5],
                "observed_dependency_coupling_fact": (
                    "The batch has a short chain of cross-task output dependencies and a small total output count. "
                    "A single helper can be more reliable when the steps are tightly coupled; fan-out remains useful for genuinely independent work.\n\n"
                    "观察到小型强依赖链；是否合并取决于步骤耦合和独立性。"
                ),
            })

    spawn_queue = asyncio.Queue()
    queue_token = set_current_spawn_queue(spawn_queue)
    owner_token = set_current_owner(main_owner)
    spawned: list[dict] = []

    try:
        helper_specs, register_done = await _spawn_helpers_only(
            cleaned_tasks=cleaned_tasks,
            main_workspace=main_workspace,
            archive_id=archive_id, group_id=group_id, user_id=user_id,
            main_owner=main_owner,
            user_lang_now=user_lang_now,
            spawn_queue=spawn_queue,
            helper_think=args.get("helper_think", False),
            guard_args=args,
        )
    except ValueError as exc:
        _msg = str(exc)
        if _msg.strip().startswith("{"):
            try:
                _payload = json.loads(_msg)
                if isinstance(_payload, dict) and _payload.get("preflight_guard"):
                    if granularity_warnings:
                        _payload.setdefault("granularity_warnings", _model_visible_warning_facts(granularity_warnings))
                    return json.dumps(_payload, ensure_ascii=False)
            except Exception:
                pass
        raise
    else:

        # Attach done_callbacks to store results in pending cache
        for spec in helper_specs:
            task = spec["task"]
            tid = spec["task_id"]
            proc_id = spec.get("proc_id", "")

            _ensure_completion_event(trace_id, tid)

            def _on_done(t: asyncio.Task, *, _tid=tid, _trace=trace_id):
                if t.cancelled():
                    return
                try:
                    res = t.result()
                except BaseException as e:
                    # 2026-05-11 P2.1: helper exception 时也带 terminal_reason
                    # 2026-05-11 P4.1: crashed 时给出明确 next_action, 主线程不必走一轮 LLM 决策
                    _crash_type = type(e).__name__
                    res = {
                        "task_id": _tid, "ok": False,
                        "report": f"helper crashed: {_crash_type}: {e}",
                        "terminal_reason": "crashed",
                        "crash_type": _crash_type,
                        # 明确告诉主线程 LLM 这是系统错误, 应该 resume 重试一次
                        "next_action": {
                            "type": "resume_after_crash",
                            "rationale": (
                                f"Helper crashed with {_crash_type}, likely a transient system error "
                                f"(not a model failure). Resume with same task_id preserves workspace; "
                                f"if it crashes again, retry with mode='hard' or report to user."
                            ),
                            "params": {
                                "action": "spawn",
                                "task_id": _tid,
                                "resume": True,
                                "prompt_hint": (
                                    "Reuse the original prompt as-is; the helper will continue from preserved workspace state.\n\n"
                                    "复用原提示；helper 从保留工作区继续。"
                                ),
                                "wait_window_sec": 300,
                            },
                            "max_retries": 1,  # 提示主线程 LLM 别无限 retry
                        },
                        "retry_instruction": (
                            f"Helper '{_tid}' crashed with {_crash_type}. Treat this as a transient system error, "
                            f"not as model completion or task failure. The helper workspace is preserved. Resume the "
                            f"same task_id once before accepting failure:\n"
                            f"  delegate(action='spawn', tasks=[{{\n"
                            f"    'task_id': '{_tid}', 'resume': true,\n"
                            f"    'prompt': '<copy the original prompt>'\n"
                            f"  }}])\n"
                            f"If the same task crashes again, resume with the same task_id and mode='hard', or report "
                            f"the verified system fault to the user.\n\n"
                            f"helper 崩溃优先同 task_id 续作一次；再次崩溃再升级或报告系统故障。"
                        ),
                    }
                paired_tid = _paired_task_map.get(_tid)
                if paired_tid and res.get("ok") and not t.cancelled():
                    async def _cancel_paired_winner_loser():
                        try:
                            h = await proc_registry().find_helper_by_task_id(
                                paired_tid, same_trace_as=ProcessRegistry.make_main_owner(_trace)
                            )
                            if h is not None and h.abort_event is not None:
                                h.abort_event.set()
                                debug.log(
                                    "delegate.code_hard_pair.cancelled_loser",
                                    f"helper '{_tid}' completed ok; requested paired helper '{paired_tid}' to stop",
                                )
                        except Exception as _e:
                            log.warning("paired helper cancel failed for %s -> %s: %r", _tid, paired_tid, _e)
                    from app.core.bg_tasks import schedule
                    schedule(
                        _cancel_paired_winner_loser(),
                        name=f"cancel_paired_{_tid}_{paired_tid}",
                    )
                from app.core.bg_tasks import schedule
                schedule(
                    _store_pending_result(_trace, _tid, res, t),
                    name=f"store_pending_{_tid}",
                )

            task.add_done_callback(_on_done)
            spawned.append({"task_id": tid, "proc_id": proc_id, "paired_with": spec.get("paired_with")})

        register_done.set()

        # 2026-05-11 P14.I: spawn 返回值附 ledger 摘要
        # 病因(实测 20:47-20:48): 主线程 1 分钟内 3 次 spawn (gen_charts/gen_paper x2 ...)
        # 不知道之前 helper 真实状态 → 派下游引用未产出文件。让 spawn 返回时直接给摘要。
        _ledger_summary_lines = []
        _completed_ok_tids: list[str] = []
        _outputs_missing_tids: list[str] = []
        try:
            _recent_ledger = _get_completion_ledger(trace_id, last_n=15)
            if _recent_ledger:
                _completed_ok_tids = sorted({
                    e['task_id'] for e in _recent_ledger
                    if e.get("outputs_complete") is True
                })
                _outputs_missing_tids = sorted({
                    e['task_id'] for e in _recent_ledger
                    if e.get("outputs_complete") is False
                })
                _ledger_summary_lines.append(
                    f"Trace ledger summary ({len(_recent_ledger)} completion event(s)):"
                )
                if _completed_ok_tids:
                    _ledger_summary_lines.append(
                        f"  ✅ outputs_complete: " + ", ".join(_completed_ok_tids[:10])
                    )
                if _outputs_missing_tids:
                    _ledger_summary_lines.append(
                        f"  ⚠️  outputs_missing: " + ", ".join(_outputs_missing_tids[:10])
                        + "  (resume the same task_id or reroute with a new task_id)"
                    )
                # 高 attempts task 提醒
                from collections import Counter as _Counter_atms
                _attempts = _Counter_atms(e['task_id'] for e in _recent_ledger)
                _high_attempt = [tid for tid, n in _attempts.items() if n >= 3]
                if _high_attempt:
                    _ledger_summary_lines.append(
                        f"  high_attempts>=3: {_high_attempt} "
                        f"(resume same task_id with mode='hard' or split the task)"
                    )
        except Exception:
            pass
        _ledger_summary_str = "\n".join(_ledger_summary_lines)
        if _ledger_summary_str:
            _ledger_summary_str = "\n\n" + _ledger_summary_str + "\n"

        return json.dumps({
            "ok": True,
            "action": "spawn_async",
            "spawned": spawned,
            "granularity_warnings": _model_visible_warning_facts(granularity_warnings),
            # P14.I: 结构化 ledger summary 供主线程消化
            "ledger_summary": {
                "completed_ok": _completed_ok_tids,
                "outputs_missing": _outputs_missing_tids,
            } if (_completed_ok_tids or _outputs_missing_tids) else None,
            "hint": (
                (
                    "Task granularity warning facts are present. Review the structured granularity_warnings field "
                    "before deciding whether to wait, collect, continue, revise, or dispatch more work:\n  "
                    + "\n  ".join(
                        f"[{w.get('issue', '?')}] task={w.get('task_id', '?')}: "
                        f"{w.get('details', '')}"
                        for w in _model_visible_warning_facts(granularity_warnings)
                    )
                    + "\n粒度警告只提供事实；后续等待、收集、重派或调整由主模型判断。\n\n"
                ) if granularity_warnings else ""
            ) + (
                f"Spawned {len(spawned)} helper(s) in background. "
                f"Use delegate(action='poll', task_ids=[...]) for heartbeat (<50ms), "
                f"delegate(action='wait_any', task_ids=[...]) to block until first done, "
                f"delegate(action='collect', task_ids=[...]) for final result. "
                f"Keep the main process productive while helpers run: write the document skeleton, prepare the next "
                f"helper request envelopes, inspect existing files, or collect ready helpers."
                f"\nhelper 后台运行时，主进程继续准备框架、检查文件或收集已完成结果。"
            ) + _ledger_summary_str,
        }, ensure_ascii=False)
    finally:
        reset_current_owner(owner_token)
        reset_current_spawn_queue(queue_token)


async def _handle_delegate_status(
    args: dict, *, main_owner: str, trace_id: str,
) -> str:
    """全局 dashboard — 列出本主线程所有活跃/已完成 helper 状态。

    2026-05-11 P3.x: 跟 poll 不同, 不需传 task_ids 参数, 自动收集本 trace 下所有 helper。
    用于主线程"看一眼全局状态"场景:
      - 我刚 spawn 了 5 个 helper, 现在大致怎么样?
      - 哪些还在跑, 哪些 stuck, 哪些已 done 可以 collect?
      - 全局摘要: N running / M done / K stuck
    """
    _sync_delegate_globals()
    from app.core import agent_state

    # 收集所有 running helper(通过 proc_registry).
    # 主线程 trace 透明: list_owned_by(main_owner) 返回同 trace 的所有 helper(含跨 owner)。
    helpers_running: list[dict] = []
    try:
        all_helpers = await proc_registry().list_owned_by(main_owner)
        for snap in all_helpers:
            # list_owned_by 已返回 dict (h.to_public_dict() 结果), 直接用
            if not isinstance(snap, dict):
                continue
            snap = dict(snap)  # 防止改到原引用
            snap["status"] = "running"
            helpers_running.append(snap)
    except Exception as e:
        log.warning("delegate status: failed to list helpers: %r", e)

    # 收集 pending_results (已完成等 collect) — 用模块级 _pending_results dict
    helpers_pending: list[dict] = []
    try:
        async with _pending_results_lock:
            for (tid_trace, tid_task), entry in _pending_results.items():
                if tid_trace != trace_id:
                    continue  # 跨 trace 隔离
                res = entry.get("result") or {}
                helpers_pending.append({
                    "task_id": tid_task,
                    "status": "done",
                    "ok": res.get("ok", False),
                    "terminal_reason": res.get("terminal_reason", "?"),
                    "collect_now": True,
                    "elapsed_sec": res.get("elapsed_sec"),
                })
    except Exception as e:
        log.warning("delegate status: failed to peek pending: %r", e)

    # 计算全局摘要
    n_running = len(helpers_running)
    n_stuck = sum(1 for h in helpers_running
                  if isinstance(h.get("stuck"), bool) and h["stuck"])
    n_done = len(helpers_pending)
    n_done_ok = sum(1 for h in helpers_pending if h.get("ok"))
    n_done_failed = n_done - n_done_ok

    # 2026-05-11 Tier 1.A: 加 recently_completed (从 completion_ledger)
    # 主线程长跑后会忘已 done 的 helper, ledger 永久记录可查询
    _recent = _get_completion_ledger(trace_id, last_n=10)
    # 摘要化(只取关键字段, 避免响应过大)
    recently_completed = [
        {
            "task_id": e["task_id"],
            "ok": e["ok"],
            "terminal_reason": e["terminal_reason"],
            "elapsed_sec": e["elapsed_sec"],
            "delivered_count": e["delivered_count"],
            "outputs_complete": e.get("outputs_complete"),
            "outputs_missing": e.get("outputs_missing"),
                "delivered_summary": e.get("delivered_summary"),
                "internal_evidence_files": (
                    e.get("internal_evidence_files", [])[:10]
                    if e.get("internal_evidence_files") else None
                ),
                "read_evidence_summary": e.get("read_evidence_summary"),
                # delivered_files 只在缺验收时展开, 减少响应大小
                "delivered_files": (
                e.get("delivered_files", [])[:10]
                if not e.get("outputs_complete") else None
            ),
        }
        for e in _recent
    ]
    structured_state = agent_state.structured_status(trace_id)
    blocked_work = structured_state.get("blocked_work") or []
    ready_to_resume_work = structured_state.get("ready_to_resume_work") or []
    artifacts_ready = structured_state.get("artifacts_ready") or []
    verified_evidence_recent = structured_state.get("verified_evidence_recent") or []
    return json.dumps({
        "ok": True, "action": "status",
        "summary": {
            "running": n_running,
            "stuck": n_stuck,
            "done_ok": n_done_ok,
            "done_failed": n_done_failed,
            "recently_completed_total": len(_recent),  # 1.A
            "total_active": n_running + n_done,
            "blocked_waiting_resource": len(blocked_work),
            "ready_to_resume": len(ready_to_resume_work),
            "ready_artifacts": len(artifacts_ready),
        },
        "running": helpers_running,
        "done_pending_collect": helpers_pending,
        "active_helpers": helpers_running,
        "completed_helpers": helpers_pending,
        "blocked_work": blocked_work,
        "ready_to_resume_work": ready_to_resume_work,
        "resource_requests": structured_state.get("resource_requests") or [],
        "artifacts_ready": artifacts_ready,
        "verified_evidence_recent": verified_evidence_recent,
        "contracts": structured_state.get("contracts") or [],
        "recently_completed": recently_completed,  # 1.A: ledger 历史
        "resource_hint": (
            "Ready-to-resume work items should be resumed with the same task_id, "
            "resume=true, and concrete satisfied_by resource paths. Blocked "
            "helpers are not factual task output; satisfy or refuse their "
            "resource requests before final delivery.\n\n"
            "可恢复 helper 应同 task_id 继续；阻塞请求需先满足或拒绝。"
        ),
        "hint": (
            f"{n_running} running ({n_stuck} stuck), {n_done} done "
            f"({n_done_ok} ok, {n_done_failed} failed). "
            + (f"另有 {len(_recent)} 个 helper 在本会话已完成 (recently_completed). "
               if _recent else "")
            + (f"Call collect(task_ids=[{', '.join(repr(h['task_id']) for h in helpers_pending)}]) "
               f"to fetch done results. " if helpers_pending else "")
            + ("⚠️ Stuck helpers detected — inspect the report first; repair scope/resources/routing/dependencies, then use mode='hard' only as a stricter same-kind retry when the kind is still correct. "
               if n_stuck > 0 else "")
            + ("Clean producer-verified helpers normally should not be resumed; use recently_completed facts to distinguish clean completion from warning/resource/partial states. "
               if any(r.get("outputs_complete") for r in recently_completed) else "")
        ),
    }, ensure_ascii=False)


async def _peek_all_pending_results(trace_id: str) -> dict[str, dict]:
    """[DEPRECATED 2026-05-11] 改用 _pending_results 直接遍历。"""
    _sync_delegate_globals()

    out: dict[str, dict] = {}
    async with _pending_results_lock:
        for (tr, tid), entry in _pending_results.items():
            if tr == trace_id:
                out[tid] = entry.get("result") or {}
    return out


async def _handle_delegate_poll(
    args: dict, *, main_owner: str, trace_id: str,
) -> str:
    """快速查询多个 task_id 的当前状态。无阻塞,< 50ms 返回。"""
    _sync_delegate_globals()

    task_ids = args.get("task_ids") or []
    if not isinstance(task_ids, list) or not task_ids:
        return json.dumps(
            {"ok": False, "error": "task_ids (list) required for poll"},
            ensure_ascii=False,
        )

    wait_window_supplied = "wait_window_sec" in args
    snapshots = []
    for tid in task_ids:
        # 1. 看 pending_results(已完成)
        pending_r = await _peek_pending_result(trace_id, tid)
        if pending_r is not None:
            snapshots.append({
                "task_id": tid,
                "status": "done",
                "ok": pending_r.get("ok", False),
                "collect_now": True,
                "hint": "use delegate(action='collect', task_ids=['" + tid + "']) to fetch result",
            })
            continue

        # 2. 看 ProcessRegistry 心跳
        try:
            h = await proc_registry().find_helper_by_task_id(
                tid, owner=main_owner,
            )
            if h is None:
                h = await proc_registry().find_helper_by_task_id(
                    tid, same_trace_as=main_owner,
                )
        except Exception:
            h = None

        if h is not None:
            snap = h.to_public_dict()
            snap["status"] = "running"
            snap["collect_now"] = False
            snap["hint"] = (
                "Task is still running. This poll result is an immediate heartbeat only; poll does not wait even if wait_window_sec was supplied. "
                "Use delegate(action='collect', task_ids=['" + tid + "'], wait_window_sec=N) to wait for the result, "
                "or delegate(action='kill', task_id='" + tid + "', reason='...') only if you deliberately want to abort it."
            )
            snapshots.append(snap)
            continue
        ledger_match = None
        for entry in reversed(_get_completion_ledger(trace_id, last_n=_LEDGER_PER_TRACE_LIMIT)):
            if entry.get("task_id") == tid:
                ledger_match = entry
                break
        if ledger_match is not None:
            snapshots.append({
                "task_id": tid,
                "status": "done_collected_or_historical",
                "ok": ledger_match.get("ok", False),
                "terminal_reason": ledger_match.get("terminal_reason", "?"),
                "elapsed_sec": ledger_match.get("elapsed_sec"),
                "delivered_count": ledger_match.get("delivered_count"),
                "outputs_complete": ledger_match.get("outputs_complete"),
                "outputs_missing": ledger_match.get("outputs_missing"),
                "collect_now": False,
                "hint": (
                    "Task already completed and is no longer pending for collect. "
                    "Use delegate(action='status') only if compact historical facts are needed; clean helper-owned completion "
                    "facts are producer evidence, not a reason to respawn or re-read content.\n\n"
                    "任务已完成且不可重复收集；干净 helper 完成事实就是生产者证据，不为复核内容重派或复读。"
                ),
            })
            continue

        else:
            snapshots.append({
                "task_id": tid,
                "status": "unknown",
                "hint": (
                    "Task is not currently running and has no pending collectable result in this trace. "
                    "Before spawning anything, call delegate(action='status') and inspect recently_completed plus task_id spelling. "
                    "Only spawn a new task_id if the original task truly has no usable result/output.\n\n"
                    "当前没有可收集结果；先查 status 和 task_id 拼写，确认无可用产物后再新派。"
                ),
            })

    response = {
        "ok": True, "action": "poll",
        "polled": snapshots,
    }
    if wait_window_supplied:
        response["wait_window_ignored"] = True
        response["wait_window_note"] = (
            "delegate(action='poll') is a non-blocking heartbeat/status query. "
            "The supplied wait_window_sec was ignored. To wait, call delegate(action='collect', task_ids=[...], wait_window_sec=N) "
            "or delegate(action='wait_any', task_ids=[...], wait_window_sec=N).\n\n"
            "poll 是即时状态查询，不等待；需要等待请用 collect 或 wait_any。"
        )
    return json.dumps(response, ensure_ascii=False)


async def _handle_delegate_collect(
    args: dict, *, main_owner: str, trace_id: str,
    main_workspace: str | None = None,
) -> str:
    """阻塞拿一个或多个 task 的最终 result。已 done 立即返回,默认 wait=30s。"""
    _sync_delegate_globals()

    task_ids = args.get("task_ids") or []
    if not isinstance(task_ids, list) or not task_ids:
        return json.dumps(
            {"ok": False, "error": "task_ids (list) required for collect"},
            ensure_ascii=False,
        )
    wait_window_sec = float(args.get("wait_window_sec", 30))
    if wait_window_sec < 1:
        wait_window_sec = 1.0
    elif wait_window_sec > 600:
        wait_window_sec = 600.0

    async def _collect_if_done(tid: str) -> dict | None:
        r = await _consume_pending_result(trace_id, tid)
        if r is not None:
            return r
        return await _recover_completed_result_for_collect(
            trace_id,
            tid,
            main_owner=main_owner,
            main_workspace=main_workspace,
        )

    import time as _t
    deadline = _t.monotonic() + wait_window_sec
    results: list[dict] = []
    pending_tids: set[str] = set(task_ids)
    unavailable: list[dict] = []

    # 第一轮:拿已完成的
    for tid in list(pending_tids):
        r = await _collect_if_done(tid)
        if r is not None:
            results.append(r)
            pending_tids.discard(tid)

    async def _has_active_helper(tid: str) -> bool:
        try:
            h = await proc_registry().find_helper_by_task_id(
                tid, owner=main_owner,
            )
            if h is None:
                h = await proc_registry().find_helper_by_task_id(
                    tid, same_trace_as=main_owner,
                )
            return h is not None
        except Exception:
            log.exception("delegate collect active helper lookup failed for %s", tid)
            return True

    async def _drop_unwaitable_tids() -> None:
        for tid in list(pending_tids):
            if await _has_active_helper(tid):
                continue
            r = await _collect_if_done(tid)
            if r is not None:
                results.append(r)
                pending_tids.discard(tid)
                continue
            pending_tids.discard(tid)
            unavailable.append({
                "task_id": tid,
                "status": "unknown_or_already_collected",
                "hint": (
                    "No active helper, pending result, ledger entry, or disk report "
                    "was found for this task_id. collect returned without waiting "
                    "because no completion event can arrive for an inactive task.\n\n"
                    "该 task_id 无活动 helper、待收集结果、账本或磁盘报告；请先查状态或重新派发正确任务。"
                ),
            })

    # 还有没拿到的 → 等
    while pending_tids and _t.monotonic() < deadline:
        await _drop_unwaitable_tids()
        if not pending_tids:
            break

        events = []
        for tid in pending_tids:
            ev = _ensure_completion_event(trace_id, tid)
            events.append((tid, ev))

        wait_tasks = [
            asyncio.create_task(ev.wait(), name=f"collect_wait_{tid}")
            for tid, ev in events
        ]
        try:
            done, _pending = await asyncio.wait(
                wait_tasks,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=max(0.1, deadline - _t.monotonic()),
            )
        finally:
            for t in wait_tasks:
                if not t.done():
                    t.cancel()

        for tid in list(pending_tids):
            r = await _collect_if_done(tid)
            if r is not None:
                results.append(r)
                pending_tids.discard(tid)

        if not done:
            break

    for tid in list(pending_tids):
        r = await _collect_if_done(tid)
        if r is not None:
            results.append(r)
            pending_tids.discard(tid)

    # 2026-05-11 P2.1: hint 里展示 terminal_reason 摘要,
    # 让主线程 LLM 一眼看到每个 helper 是 completed/stuck/interrupted/crashed
    _term_summary = " ".join(
        f"{r.get('task_id', '?')}={r.get('terminal_reason', '?')}"
        for r in results
    ) if results else ""
    # 2026-06-05: helper 同时带 ok=true + stuck/interrupted=true 时 (P130 read-helper
    # 之类的 hard-stop 可能仍走 forced_finalize 输出 ok=true), success_count 不能把
    # 它们算成成功 — 否则 timing_summary 会报 ok=1 stuck=1 自相矛盾。
    success_count = sum(
        1 for r in results
        if r.get("ok") and not r.get("interrupted") and not r.get("stuck")
    )
    partial_artifact_results = [
        r for r in results
        if (
            (r.get("interrupted") or r.get("stuck"))
            and (
                bool(r.get("outputs_check", {}).get("outputs_complete"))
                or bool(r.get("user_visible_files"))
                or bool(r.get("workspace_files"))
                or bool(r.get("files"))
            )
            and r.get("terminal_reason") != "resource_required"
            and not r.get("resource_required")
        )
    ]
    _partial_artifact_ids = {
        str(r.get("task_id") or "")
        for r in partial_artifact_results
        if str(r.get("task_id") or "")
    }
    resource_required_count = sum(
        1 for r in results
        if r.get("terminal_reason") == "resource_required" or r.get("resource_required")
    )
    interrupted_count = sum(
        1 for r in results
        if r.get("interrupted")
        and r.get("terminal_reason") != "resource_required"
        and not r.get("resource_required")
        and str(r.get("task_id") or "") not in _partial_artifact_ids
    )
    stuck_count = sum(
        1 for r in results
        if r.get("stuck")
        and r.get("terminal_reason") != "resource_required"
        and not r.get("resource_required")
        and str(r.get("task_id") or "") not in _partial_artifact_ids
    )
    quality_blocked_count = sum(
        1 for r in results
        if r.get("terminal_reason") == "quality_blocked" or r.get("quality_blocked")
    )
    failed_count = sum(
        1 for r in results
        if (
            not r.get("ok")
            and not r.get("interrupted")
            and not r.get("stuck")
            and r.get("terminal_reason") != "resource_required"
            and not r.get("resource_required")
            and not r.get("quality_blocked")
            and r.get("terminal_reason") != "quality_blocked"
        )
    )
    incomplete_count = (
        resource_required_count
        + interrupted_count
        + stuck_count
        + quality_blocked_count
        + failed_count
    )

    response = {
        "ok": True, "action": "collect",
        "helpers_requested": len(task_ids),
        "helpers_completed": len(results),
        "helpers_still_running": len(pending_tids),
        "helpers_unavailable": len(unavailable),
        "success_count": success_count,
        "incomplete_count": incomplete_count,
        "failed_count": failed_count,
        "interrupted_count": interrupted_count,
        "stuck_count": stuck_count,
        "partial_artifact_count": len(partial_artifact_results),
        "quality_blocked_count": quality_blocked_count,
        "resource_required_count": resource_required_count,
        "results": results,
        "still_running": sorted(pending_tids),
        "unavailable": unavailable,
        "wait_window_expired": bool(pending_tids),
        "hint": (
            (f"Terminal states: [{_term_summary}]. " if _term_summary else "")
            + (
                f"{len(results)} result(s) collected; "
                f"{len(pending_tids)} task(s) still running. "
                f"Call delegate(action='collect', task_ids={sorted(pending_tids)}) "
                f"again later, or do other work first."
            ) if pending_tids else
            f"Terminal states: [{_term_summary}]. All {len(results)} task(s) collected."
        ),
    }
    if partial_artifact_results:
        response["partial_artifacts"] = [
            {
                "task_id": r.get("task_id"),
                "terminal_reason": r.get("terminal_reason"),
                "user_visible_files": r.get("user_visible_files") or [],
                "workspace_files": r.get("workspace_files") or r.get("files") or [],
                "outputs_check": r.get("outputs_check") or {},
            }
            for r in partial_artifact_results[:8]
        ]
        response["_partial_artifact_policy"] = (
            "Some interrupted or stuck helpers already produced artifacts. This is not an automatic PASS. "
            "Treat the listed files and outputs_check as facts, then decide whether targeted inspection/verification is needed "
            "to accept, repair, or resume the same task_id.\n\n"
            "部分中断/卡住 helper 已有产物；这不是自动完成。请把文件存在与验收信息作为事实，再判断是否需要定向读取/验证。"
        )
    return json.dumps(response, ensure_ascii=False)


async def _handle_delegate_wait_any(
    args: dict, *, main_owner: str, trace_id: str,
) -> str:
    """阻塞等任一 task 完成,默认 wait_window=30s。"""
    _sync_delegate_globals()

    task_ids = args.get("task_ids") or []
    if not isinstance(task_ids, list) or not task_ids:
        return json.dumps(
            {"ok": False, "error": "task_ids (list) required for wait_any"},
            ensure_ascii=False,
        )
    wait_window_sec = float(args.get("wait_window_sec", 30))
    if wait_window_sec < 1:
        wait_window_sec = 1.0
    elif wait_window_sec > 300:
        wait_window_sec = 300.0

    # 先看是否已经有完成的
    for tid in task_ids:
        r = await _consume_pending_result(trace_id, tid)
        if r is not None:
            return json.dumps({
                "ok": True, "action": "wait_any",
                "winner_task_id": tid,
                "result": r,
            }, ensure_ascii=False)

    # 2026-06-10: interlock fix. _consume_pending_result clears the completion
    # event, so a second wait_any on an already-collected task used to block on
    # a cleared event for the full window before falling back to poll. Mirror
    # collect's unwaitable check: a task with no active helper can never set
    # its event again — recover from the ledger/disk instead of waiting.
    for tid in task_ids:
        try:
            h = await proc_registry().find_helper_by_task_id(tid, owner=main_owner)
            if h is None:
                h = await proc_registry().find_helper_by_task_id(tid, same_trace_as=main_owner)
        except Exception:
            h = object()  # lookup failure -> assume active, keep waiting
        if h is not None:
            continue
        recovered = await _recover_completed_result_for_collect(
            trace_id, tid, main_owner=main_owner,
        )
        if recovered is not None:
            return json.dumps({
                "ok": True, "action": "wait_any",
                "winner_task_id": tid,
                "result": recovered,
                "recovered_from_ledger": True,
            }, ensure_ascii=False)

    # 等任一 event
    events = [
        (tid, _ensure_completion_event(trace_id, tid))
        for tid in task_ids
    ]
    wait_tasks = {
        asyncio.create_task(ev.wait(), name=f"waitany_{tid}"): tid
        for tid, ev in events
    }

    try:
        done, _pending = await asyncio.wait(
            wait_tasks.keys(),
            return_when=asyncio.FIRST_COMPLETED,
            timeout=wait_window_sec,
        )
    finally:
        for t in wait_tasks:
            if not t.done():
                t.cancel()

    if not done:
        return await _handle_delegate_poll(
            {"task_ids": task_ids},
            main_owner=main_owner, trace_id=trace_id,
        )

    winner_task = next(iter(done))
    winner_tid = wait_tasks[winner_task]
    r = await _consume_pending_result(trace_id, winner_tid)
    if r is None:
        return await _handle_delegate_poll(
            {"task_ids": task_ids},
            main_owner=main_owner, trace_id=trace_id,
        )

    return json.dumps({
        "ok": True, "action": "wait_any",
        "winner_task_id": winner_tid,
        "result": r,
        "hint": (
            f"Winner: {winner_tid}. "
            f"{len([t for t in task_ids if t != winner_tid])} other task(s) still running. "
            f"Use delegate(action='collect') or delegate(action='wait_any') again to wait for more.\n\n"
            "已有一个 helper 完成；其它 helper 仍在运行，需要更多结果时继续等待或收集。"
        ),
    }, ensure_ascii=False)


async def handle_delegate(
    main_workspace: str,
    args: dict,
    *,
    archive_id: str,
    group_id: str,
    user_id: str,
) -> str:
    """Handle delegate tool call.

    Actions(向后兼容,缺省 'spawn'):
      - 'spawn' (默认): 并行运行多个 helper, 等所有完成
      - 'kill':         主线程 kill 指定 helper(by task_id)
      - 'fork_from':    针对单个已结束 helper, 复制其工作区作为新 helper 起点
                        (相当于"看完回复后让 helper 分裂并继续")
      - 'spawn_async':  立即返回 proc_ids,helper 后台跑(主线程不阻塞) — L1-1
      - 'poll':         <50ms 查心跳,task_ids 必传 — L1-1
      - 'collect':      阻塞拿最终结果(已 done 立即返回),task_ids 必传 — L1-1
      - 'wait_any':     等任一 task done(用于 fan-out 后想立即接住第一个结果) — L1-1
    """
    _sync_delegate_globals()

    action = str(args.get("action", "")).strip().lower()
    if not action:
        action = "spawn"  # backward compat
    _normalized_top_level_task = False
    if action in ("spawn", "spawn_async"):
        _normalized_top_level_task = _normalize_top_level_delegate_task_args(args)
        if _normalized_top_level_task:
            args["_schema_repair_fact"] = (
                "The delegate call supplied one helper task as top-level task fields. "
                "The runtime preserved those fields and wrapped them as tasks=[...] before execution."
            )
            try:
                debug.log(
                    "delegate.top_level_task_args_normalized",
                    "wrapped top-level helper task fields into tasks=[...]",
                    {
                        "task_id": args["tasks"][0].get("task_id"),
                        "kind": args["tasks"][0].get("kind"),
                        "action": action,
                    },
                )
            except Exception:
                pass

    # 2026-05-09 Patch 36: task_ids + spawn 误用兜底(适用于 spawn / spawn_async)
    # 病因(trace 779bbcf0 iter 11):模型调
    #   delegate(action='spawn', task_ids=['fix_woat'], wait_window_sec=300, resume=true)
    # 而非正确的:
    #   delegate(action='spawn', tasks=[{task_id:'fix_woat', resume:true, prompt:'...'}])
    # 旧行为:下游 tasks 空 → 报"tasks must be a non-empty array",模型下个 iter 重试,
    # 浪费 1 轮 LLM 调用 + 30s+ 时间。
    # 新行为:检测到 task_ids 非空 + 是 spawn 类 action → 把 task_ids 转译成 tasks,
    # prompt 用通用"继续上次未完成的工作"默认值,silent fix + log warning。
    # 此误用是可识别的 schema 误读,不该让模型空转重试。
    if (
        action in ("spawn", "spawn_async")
        and not args.get("tasks")
        and isinstance(args.get("task_ids"), list)
        and args["task_ids"]
    ):
        _raw_tids = args["task_ids"]
        _resume_flag = bool(args.get("resume", True))
        _generic_prompt = (
            args.get("prompt")
            or "继续未完成的工作。基于上次进度直接推进,不用从头开始。"
            "如果工作区状态显示已经完成,出最终报告。"
        )
        _new_tasks = []
        for _tid in _raw_tids:
            if not isinstance(_tid, str) or not _tid.strip():
                continue
            _new_tasks.append({
                "task_id": _tid.strip(),
                "resume": _resume_flag,
                "prompt": _generic_prompt,
                "kind": args.get("kind", "code"),
            })
        if _new_tasks:
            args = {**args, "tasks": _new_tasks}  # 不修改原 args
            log.warning(
                "delegate task_ids+%s 误用,自动转译为 tasks=[...]: %s",
                action, [t["task_id"] for t in _new_tasks],
            )
            debug.log(
                "delegate.task_ids_fallback",
                f"模型调 {action} 但用 task_ids=[...] 而非 tasks=[{{...}}],"
                f"已自动转译 {len(_new_tasks)} 个 task(用 generic resume prompt)",
                {"task_ids": [t["task_id"] for t in _new_tasks],
                 "resume": _resume_flag},
            )

    if action == "kill":
        return await _handle_main_kill_helper(args)

    # ── L1-1 (2026-05-09): 异步 action ──
    if action in ("spawn_async", "poll", "collect", "wait_any", "status"):
        trace_id = debug.current_trace_id() or "unknown"
        main_owner = ProcessRegistry.make_main_owner(trace_id)
        if action == "poll":
            return await _handle_delegate_poll(
                args, main_owner=main_owner, trace_id=trace_id,
            )
        elif action == "collect":
            return await _handle_delegate_collect(
                args, main_owner=main_owner, trace_id=trace_id,
                main_workspace=main_workspace,
            )
        elif action == "wait_any":
            return await _handle_delegate_wait_any(
                args, main_owner=main_owner, trace_id=trace_id,
            )
        elif action == "status":
            # 2026-05-11 P3.x: 全局 dashboard — 不需传 task_ids,自动列所有活跃 helper
            return await _handle_delegate_status(
                args, main_owner=main_owner, trace_id=trace_id,
            )
        elif action == "spawn_async":
            cleaned_tasks = await _sanitize_and_validate_tasks(
                args, main_workspace=main_workspace,
                archive_id=archive_id, group_id=group_id, user_id=user_id,
            )
            if isinstance(cleaned_tasks, str):
                return cleaned_tasks
            return await _handle_delegate_spawn_async(
                main_workspace, args,
                archive_id=archive_id, group_id=group_id, user_id=user_id,
                cleaned_tasks=cleaned_tasks,
                main_owner=main_owner,
                user_lang_now=current_user_lang() or "zh",
                trace_id=trace_id,
            )

    elif action not in ("spawn", "fork_from"):
        return json.dumps(
            {
                "ok": False,
                "error": (
                    f"Unknown delegate action {action!r}; valid actions are spawn, spawn_async, "
                    "poll, collect, wait_any, kill, fork_from, and status.\n"
                    "delegate action 无效，请改用支持的动作。"
                ),
            },
            ensure_ascii=False,
        )

    if action == "spawn" and not args.get("tasks") and not args.get("task_ids"):
        return await _sanitize_and_validate_tasks(
            args,
            main_workspace=main_workspace,
            archive_id=archive_id,
            group_id=group_id,
            user_id=user_id,
        )

    cleaned_tasks = await _sanitize_and_validate_tasks(
        args,
        main_workspace=main_workspace,
        archive_id=archive_id,
        group_id=group_id,
        user_id=user_id,
    )
    if isinstance(cleaned_tasks, str):
        return cleaned_tasks

    tasks = args.get("tasks") or []

    # 默认 90s(2026-05-07 Opt A): 主线程不再无限等待所有 helper。
    # 主线程可传 0 或负数恢复"等所有"旧行为; 或传更大值(≤1800)延长。
    # 2026-05-02: helper 不再有 safety timeout,wait_window_sec 上限纯粹是
    #   "主线程单次调用愿意阻塞多久"的产品决策,1800s 是为了避免一次 LLM tool call
    #   占用太久回不到 round2 主循环。helper 即使 wait_window 触发返回后仍继续跑。
    _DELEGATE_WAIT_WINDOW_MAX = 1800
    _DELEGATE_WAIT_WINDOW_DEFAULT = 90.0
    raw_wait = args.get("wait_window_sec")

    # 2026-05-10 Patch 69: task-level wait_window_sec 误放检测 + 自动 hoist
    # 病因(trace f973df3770544567):主线程派 task 时把 wait_window_sec 写进 task 字典里
    # `tasks=[{task_id, prompt, wait_window_sec: 300}]` — 但 schema 里 wait_window_sec
    # 是 top-level 字段。结果 args["wait_window_sec"] 是 None,实际等待窗口 = 90s 默认,
    # 触发频繁 wait_window_expired → 主线程多次 wake up,helper 还在跑。
    # LLM 误读 schema 是常见错;系统应纠错 + 教学,而不是任由错误生效。
    # 修法:如果 top-level 没 wait_window_sec 但 tasks 中有 task 传了,自动 hoist
    # 取最大值作为实际 wait_window_sec。同时 debug log 让能观察 LLM 学习。
    _hoisted_from_tasks = False
    if raw_wait is None:
        _task_level_waits = []
        for _t in tasks:
            if isinstance(_t, dict):
                _w = _t.get("wait_window_sec")
                if _w is not None:
                    try:
                        _task_level_waits.append(float(_w))
                    except (TypeError, ValueError):
                        pass
        if _task_level_waits:
            raw_wait = max(_task_level_waits)
            _hoisted_from_tasks = True
            debug.log(
                "delegate.wait_window.hoisted",
                f"P69: task-level wait_window_sec 自动 hoist 到 top-level "
                f"(取最大值 {raw_wait}s)。提示:wait_window_sec 是 top-level 字段,"
                f"放在每个 task 字典里 schema 不识别,默认 90s。下次请放在 delegate 调用顶层。",
            )

    wait_window_sec: float | None = _DELEGATE_WAIT_WINDOW_DEFAULT
    if raw_wait is not None:
        try:
            wait_window_sec = float(raw_wait)
            if wait_window_sec <= 0:
                wait_window_sec = None  # 显式传 0 或负数 = "等所有 helper"
            elif wait_window_sec < 30:
                # 太短没意义,helper 还没真开始干活就被 wait_window 切回
                wait_window_sec = 30.0
            elif wait_window_sec > _DELEGATE_WAIT_WINDOW_MAX:
                wait_window_sec = float(_DELEGATE_WAIT_WINDOW_MAX)
        except (TypeError, ValueError):
            wait_window_sec = _DELEGATE_WAIT_WINDOW_DEFAULT

    # ── min_results_to_return: 2026-05-03 优化 #1 ──
    # 模型可指定"够 N 个 helper 完成就回",剩下的留后台继续跑。
    # 用途:helper 完成时间分布失衡时,快的一回主线程立刻动手,不傻等慢的。
    # 实测 trace 09ba132f 主线程等所有 4 helper 跑完了 9:51,其中 arith 已经
    # 在 3:53 done,白等 6 分钟。
    #
    # 2026-05-05: 默认值改为 1 —— 只要任一 helper 完成就立即返回,保证主循环
    # 响应实时性。主线程拿到部分结果后可立即开始集成,不用傻等全部完成。
    # LLM 可显式传更大值(如 3=等最快 3 个),传 0 或 ≥task 数恢复"等全部"。
    raw_min = args.get("min_results_to_return")
    _cleaned_kinds = {
        str(t.get("kind") or "").strip().lower()
        for t in (cleaned_tasks if isinstance(cleaned_tasks, list) else [])
        if isinstance(t, dict)
    }
    _material_read_batch = bool(_cleaned_kinds) and _cleaned_kinds <= {"read", "ocr"}
    min_results_to_return: int | None = None if _material_read_batch else 1
    if raw_min is not None:
        try:
            min_results_to_return = int(raw_min)
        except (TypeError, ValueError):
            pass
    if min_results_to_return is not None:
        if min_results_to_return <= 0:
            min_results_to_return = None  # 0 = 等全部
        elif min_results_to_return >= len(tasks):
            min_results_to_return = None  # ≥task 数 = 等全部
    if _material_read_batch and raw_min is None:
        debug.log(
            "delegate.min_results.material_read_default_all",
            (
                "read-helper material batches wait for all helpers or wait_window instead of returning after "
                "the first result; partial early return often makes the main process synthesize from missing evidence."
            ),
        )

    # 2026-05-21: spawn 路径不再 mirror 一整套清洗/配对逻辑。
    # _sanitize_and_validate_tasks(上面已调用)是唯一清洗入口,直接复用其结果。
    # (历史上这里 mirror 了 sanitize 全部逻辑 → 双份维护 + 日志双打印,实测 trace
    #  c6e42ed6 17:58 hard-pair 日志打印两次;现统一为单一来源。)
    cleaned = cleaned_tasks
    _twin_map = args.get("_paired_task_map") or {}

    base = Path(main_workspace)
    user_tag = _user_workspace_tag(user_id)
    _env_fetch_stats = _auto_fetch_environment_workspace_refs(main_workspace, cleaned)
    if _env_fetch_stats.get("fetched"):
        debug.log(
            "delegate.environment.auto_fetch",
            f"fetched {len(_env_fetch_stats['fetched'])} _env refs before helper spawn",
            _env_fetch_stats,
        )
    _normalize_environment_output_paths_from_manifest(main_workspace, cleaned)
    _annotate_source_count_hints_from_manifest(main_workspace, cleaned)

    # ── 设置 ContextVar:owner=main, spawn_queue 让 helper 能动态加 task ──
    trace_id = debug.current_trace_id() or "unknown"
    main_owner = ProcessRegistry.make_main_owner(trace_id)
    owner_token = set_current_owner(main_owner)

    spawn_queue: asyncio.Queue = asyncio.Queue()
    queue_token = set_current_spawn_queue(spawn_queue)

    # 2026-05-15 (F841 lint 发现): 本地变量从来不被 read。但**调用本身有副作用**:
    # get_abort_channel 是 lazy creator,首次调用会在 GroupGuard 内部注册
    # _abort_channels[(archive,group,user)] 这个 entry。即使 handle_delegate 不
    # 调,后续的 _run_one_helper 也会独立 fetch 同一个 channel — 所以删掉这行
    # 行为不变(只是抽到第一个 helper spawn 时才注册而已)。保留这行 + 下划线前缀
    # 标记"仅为副作用,不读返回值",意图:在任何 helper spawn 之前就把 channel 注册
    # 好,这样从 spawn 开始之前到第一个 helper 拿 channel 之间这段窗口期, 如果用户
    # 按了 abort,signal_abort 也能找到 channel 并打信号(否则会 silent drop)。
    _abort_event = get_group_guard().get_abort_event(archive_id, group_id, user_id)
    del _abort_event  # 表明刻意丢弃

    initial_helper_specs: list[dict] = []
    helper_tasks: list[asyncio.Task] = []  # init early so except CancelledError can use
    # 2026-05-02 part7:同样 init early,except 路径可能在 helper 还没创建时抛
    helper_abort_pairs: list[tuple[asyncio.Task, asyncio.Event]] = []
    helper_task_ids: dict[asyncio.Task, str] = {}
    try:
        # ── resume race protection (2026-05-05 revised) ──
        # 旧逻辑:resume=true 时先查同 task_id 旧 helper;若仍活着,set abort_event
        # 让它 finalize,等 done,然后 spawn 新 stream。
        # 问题:主线程常误判健康 helper 为"需要替换",导致正在产出的 helper 被
        # 无谓 kill (实测: lsd_r8 已编译通过准备链接 bench.c,被 abort 杀死)。
        #
        # 2026-05-05 修正:旧 helper 存活时**不杀旧**——不 spawn 新 helper,
        # 但**接管旧 asyncio.Task**到本次 delegate 的 wait loop 里。
        # 之前(≤2026-05-07)只是 skip spawn,旧 task 变成孤儿:
        # 跑完后无人 await,结果丢失(实测 p8483_solve 974s 跑完但主线程从未看到)。
        # 现在:把旧 task 加入 helper_tasks,_dynamic_wait_loop 会正常等它完成并收集结果。
        #
        # 唯一例外:旧 helper 已 done (task.done()==True) → 正常续作。
        _skip_task_ids: set[str] = set()
        _adopted_old_tasks: list[asyncio.Task] = []  # 接管旧 task 引用
        _adopted_task_ids: list[str] = []  # 对应 task_id,供 still_running 反查
        # 2026-05-08 优化(silent prompt drop): 记录 LLM 这次 resume 想传的新 prompt 内容,
        # adopt 时旧 helper 看不到这个新 prompt(它在自己的 context 里继续跑)。
        # 之前主线程不知道自己的新指令被丢弃了——以为 helper 会按新方向走。
        # 现在把丢弃的 prompt 通过 adopted_with_dropped_prompts 上报,主线程能看到:
        # "你给 helper 的新指令未被采用,helper 还在按旧指令跑。要换方向先 kill 再重起。"
        _adopted_dropped_prompts: list[dict] = []
        for c in cleaned:
            tid = c["task_id"]
            try:
                existing_h = await proc_registry().find_helper_by_task_id(
                    tid, owner=main_owner,
                )
                if existing_h is None:
                    existing_h = await proc_registry().find_helper_by_task_id(
                        tid, same_trace_as=main_owner,
                    )
                if existing_h is None:
                    continue  # 没旧 helper,直接 fresh resume 路径
                old_task = existing_h.helper_task
                if old_task is None or old_task.done():
                    continue  # 旧 helper 已经 done,registry 还没清理,直接 spawn
                # 旧 helper 还活着 → 不杀旧,接管 task 到本次 wait loop
                _why = "resume" if c.get("resume") else "fresh spawn"
                debug.log(
                    f"delegate.{tid}.duplicate_protect",
                    f"live helper proc={existing_h.proc_id} for task_id={tid} still running "
                    f"({_why} attempt blocked); adopting existing task instead of spawning duplicate",
                )
                _skip_task_ids.add(tid)
                _adopted_old_tasks.append(old_task)
                _adopted_task_ids.append(tid)
                # 记录被丢弃的 prompt 摘要(>30 字时摘前 80 字给 LLM 看,够它认出是哪条)
                _new_prompt = (c.get("prompt") or "").strip()
                if _new_prompt:
                    _adopted_dropped_prompts.append({
                        "task_id": tid,
                        "dropped_prompt_excerpt": (_new_prompt[:80] + "…") if len(_new_prompt) > 80 else _new_prompt,
                    })
            except Exception:
                log.exception(
                    "resume_race_protect check failed for %s; proceeding with spawn", tid,
                )
        if _skip_task_ids:
            cleaned = [c for c in cleaned if c["task_id"] not in _skip_task_ids]
            debug.log(
                "delegate.resume_race_skipped",
                f"skipped {len(_skip_task_ids)} task(s) with live old helpers: "
                f"{sorted(_skip_task_ids)}",
            )
            # 2026-05-15 P66: 单独 log 被丢弃 prompt 的数量, 便于审计 LLM 重复犯错频率
            if _adopted_dropped_prompts:
                debug.log(
                    "delegate.prompt_silently_dropped",
                    f"{len(_adopted_dropped_prompts)} task(s) carried a NEW prompt but "
                    f"helper still running — prompt content discarded; "
                    f"task_ids={[d['task_id'] for d in _adopted_dropped_prompts]}. "
                    f"主线程下次操作必须先 kill 再传新 prompt, 或不传 prompt 只 poll。",
                )

        # ── 已完成去重:防止 LLM 在 repair_pairing 混淆后重复 spawn (2026-05-08) ──
        # repair_pairing 修复孤儿 tool_call 后可能让 LLM 以为"任务还没做",
        # 但 delegate 刚返回 ok=true。对刚成功完成(≤10min)的 task_id:
        # - fresh spawn → 拦截
        # - resume=true → 也拦截（2026-05-08 Fix 4: repair_pairing 后 LLM 常
        #   用 resume=true 重开已完成任务,不可 resume 一个已成功的 task）
        # - fork_from → 允许（fork 已完成任务做不同的活是合法的）
        _dup_completed_tids: set[str] = set()
        _dup_resume_completed_tids: set[str] = set()
        for c in cleaned:
            tid = c["task_id"]
            try:
                _was = await proc_registry().was_recently_completed(tid)
                if not _was:
                    continue
            except Exception:
                continue
            if c.get("resume"):
                _dup_resume_completed_tids.add(tid)
            elif not c.get("fork_from"):
                _dup_completed_tids.add(tid)

        # 合并所有重复 task_id
        _all_dup_tids = _dup_completed_tids | _dup_resume_completed_tids
        if _all_dup_tids:
            _dup_hint = sorted(_all_dup_tids)
            _why_parts: list[str] = []
            if _dup_completed_tids:
                _why_parts.append(
                    f"fresh spawn blocked: {sorted(_dup_completed_tids)}"
                )
            if _dup_resume_completed_tids:
                _why_parts.append(
                    f"resume blocked (already completed): {sorted(_dup_resume_completed_tids)}"
                )
            debug.log(
                "delegate.duplicate_completed_blocked",
                "; ".join(_why_parts),
            )
        # ── 2026-05-08 Fix 6: legacy paired hard tasks are auxiliary ──
        # If every primary task was already completed, do not let an old paired
        # hard sibling keep a duplicate delegation alive.
        _is_legacy_auxiliary_pair = _is_legacy_paired_hard_task
        _orig_cleaned = [c for c in cleaned if not _is_legacy_auxiliary_pair(c)]
        _all_orig_dup = _orig_cleaned and all(
            c["task_id"] in _all_dup_tids for c in _orig_cleaned
        )
        # 2026-05-08 Fix(Bug 1):加 cleaned 非空守卫。
        # 旧逻辑当所有原 task 都因"旧 helper 仍存活"被 _skip_task_ids 过滤掉后,
        # cleaned=[] 且 _all_dup_tids=set();set()==set()→True 误返回 already_completed,
        # 把正在被 adopt 的活 helper 当作已完成结果上报。LLM 看到 already_completed=true
        # + 旧 note "换一个不同的 task_id"→ 创建 _v2 helper, 真正在跑的旧 helper 被搁置。
        # 修复:cleaned 为空说明一切都走 adoption 路径,不应早返回,让 wait_loop 处理。
        if cleaned and (set(c["task_id"] for c in cleaned) == _all_dup_tids or _all_orig_dup):
            # 所有 primary task 都是已完成重复；legacy auxiliary pair 不应单独保持 spawn。
            _dup_hint = sorted(_all_dup_tids)
            return json.dumps(await _already_completed_delegate_response(
                trace_id=trace_id,
                main_owner=main_owner,
                main_workspace=main_workspace,
                task_ids=_dup_hint,
                note=(
                    f"task_id(s) {_dup_hint} already completed successfully in this conversation. "
                    "Continue from the recovered helper-owned output facts when present. Spawn a new helper only "
                    "for a semantically different task with a clearly different task_id.\n"
                    "这些 task_id 已完成；优先基于已恢复的 helper 产物事实继续，只有语义不同的新任务才新派 helper。"
                ),
            ), ensure_ascii=False)
        if _all_dup_tids:
            # 部分重复 → 只过滤重复的,其余正常 spawn
            cleaned = [c for c in cleaned if c["task_id"] not in _all_dup_tids]
            debug.log(
                "delegate.partial_duplicate_filtered",
                f"filtered {sorted(_all_dup_tids)}, proceeding with {len(cleaned)} remaining",
            )
            # 去重后若只剩历史 paired hard sibling，则一起拦截。
            if cleaned and all(_is_legacy_auxiliary_pair(c) for c in cleaned):
                debug.log(
                    "delegate.legacy_aux_pair_blocked_after_dedup",
                    "all primary tasks blocked by dedup; blocking legacy auxiliary pair too",
                )
                _dup_hint = sorted(_all_dup_tids)
                return json.dumps(await _already_completed_delegate_response(
                    trace_id=trace_id,
                    main_owner=main_owner,
                    main_workspace=main_workspace,
                    task_ids=_dup_hint,
                    note=(
                        f"task_id(s) {_dup_hint} already completed. "
                        "Continue from the recovered helper-owned output facts when present; no legacy paired helper was spawned.\n"
                        "这些任务已完成；若已恢复结果，直接基于 helper 产物事实继续，未启动旧配对 helper。"
                    ),
                ), ensure_ascii=False)

        # 处理 fork_from + resume 迁移:每个 task 的 workspace
        for c in cleaned:
            target_ws = str(base / f"_delegate_{user_tag}_{c['task_id']}")

            # ── v2 三层隔离:resume 时旧 workspace 可能在 .prev/ ──
            if c["resume"] and not os.path.isdir(target_ws):
                prev_base = os.path.join(os.path.dirname(str(base)), ".prev")
                prev_ws = os.path.join(prev_base, f"_delegate_{user_tag}_{c['task_id']}")
                if os.path.isdir(prev_ws):
                    _ignore_delegate = lambda d, contents: [
                        x for x in contents if x.startswith("_delegate_")
                    ]
                    os.makedirs(target_ws, exist_ok=True)
                    _copied_files = 0
                    for _entry in os.listdir(prev_ws):
                        _src = os.path.join(prev_ws, _entry)
                        _dst = os.path.join(target_ws, _entry)
                        try:
                            if os.path.isfile(_src) or os.path.islink(_src):
                                shutil.copy2(_src, _dst)
                                _copied_files += 1
                            elif os.path.isdir(_src) and not _entry.startswith("_delegate_"):
                                shutil.copytree(_src, _dst, dirs_exist_ok=True)
                                _copied_files += sum(1 for _ in Path(_dst).rglob("*") if _.is_file())
                        except OSError:
                            pass
                    debug.log(
                        f"delegate.{c['task_id']}.prev_migrate",
                        f"resume: copied ~{_copied_files} files from .prev/ to .temp/",
                    )

            if c["fork_from"]:
                src_tid = _sanitize_task_id(c["fork_from"], 0)
                src_ws = str(base / f"_delegate_{user_tag}_{src_tid}")
                if not os.path.isdir(src_ws):
                    return json.dumps(
                        {
                            "ok": False,
                            "error": (
                                f"fork_from source workspace does not exist: {src_tid}.\n"
                                "fork_from 源工作区不存在。"
                            ),
                        },
                        ensure_ascii=False,
                    )
                # 检查大小
                sz = _dir_size(src_ws)
                if sz > _FORK_WORKSPACE_REJECT_BYTES:
                    return json.dumps(
                        {"ok": False, "error":
                         (
                             f"fork_from source workspace is too large: {sz // 1024 // 1024}MB (>500MB).\n"
                             "fork_from 源工作区过大，已拒绝。"
                         )},
                        ensure_ascii=False,
                    )
                if sz > _FORK_WORKSPACE_WARN_BYTES:
                    debug.log("delegate.fork.large",
                              f"{c['task_id']} forking {sz // 1024 // 1024}MB from {src_tid}")
                # 清理目标后复制
                clean_workspace_dir(target_ws)
                n_copied = await _fast_copy_workspace(src_ws, target_ws)
                _cap = enforce_workspace_capacity(
                    target_ws,
                    label=f"helper_fork:{c['task_id']}",
                )
                if not _cap.get("ok", True):
                    return json.dumps(
                        {"ok": False, "error":
                         (
                             f"fork_from target workspace exceeds the capacity limit: "
                             f"{_cap.get('after_bytes', 0) // 1024 // 1024}MB.\n"
                             "fork_from 目标工作区超过容量上限。"
                         )},
                        ensure_ascii=False,
                    )
                debug.log("delegate.fork.copied",
                          f"{c['task_id']}: copied {n_copied} files from {src_tid}")
                # fork_from 之后 resume 强制 True(不要再覆盖工作区)
                c["resume"] = True
            initial_helper_specs.append({**c, "workspace": target_ws})

        # 2026-05-05: spawn 前清理主工作区旧会话残留(.o/.obj/.exe/historical artifacts)
        # 防止旧会话文件通过 fork 污染 helper 工作区
        _cleaned = _clean_main_workspace_before_spawn(main_workspace)

        _preflight_guard_payload = await _run_delegate_preflight_guard(
            args,
            initial_helper_specs,
            trace_id,
            main_workspace=main_workspace,
            archive_id=archive_id,
            group_id=group_id,
            user_id=user_id,
        )
        if _preflight_guard_payload is not None:
            return json.dumps(_preflight_guard_payload, ensure_ascii=False)

        _log_delegate_start_event(initial_helper_specs)

        try:
            await _record_task_contracts(trace_id, initial_helper_specs)
        except Exception as exc:
            debug.log("delegate.contract_record_failed", f"{type(exc).__name__}: {exc}")

        n_active_server = await proc_registry().count_active_helpers()
        if n_active_server + len(initial_helper_specs) > _MAX_HELPERS:
            return json.dumps({
                "ok": False,
                "error": (
                    f"Server active helper limit would be exceeded: "
                    f"{n_active_server}+{len(initial_helper_specs)}/{_MAX_HELPERS}.\n"
                    "服务端活跃 helper 数将超过上限。"
                ),
            }, ensure_ascii=False)
        n_active_agent = await proc_registry().count_active_helpers_for_trace(main_owner)
        if n_active_agent + len(initial_helper_specs) > _MAX_HELPERS_PER_AGENT:
            return json.dumps({
                "ok": False,
                "error": (
                    f"Per-agent active helper limit would be exceeded: "
                    f"{n_active_agent}+{len(initial_helper_specs)}/{_MAX_HELPERS_PER_AGENT}.\n"
                    "单个智能体活跃 helper 数将超过上限。"
                ),
            }, ensure_ascii=False)

        # The LLM delegation guard now runs before helpers start
        # (_run_delegate_preflight_guard above). Do not start a second blocking
        # guard after helper streams open: late guard_blocked feedback can leave
        # orphan work and confuse the main process. Keep the wait-loop guard
        # plumbing for older callers, but normal delegate spawn passes None.
        #
        # helper 启动前已经过统一守卫；启动后不再二次阻断，避免先拉起再返回拆分错误。
        _guard_task: asyncio.Task | None = None
        if initial_helper_specs:
            debug.log(
                "delegate.post_spawn_guard.skipped",
                "preflight guard already completed; no post-spawn blocking guard started",
            )

        # ── 创建 initial helper tasks + 注册到 ProcessRegistry(带 auto-cleanup)──
        # Bug #8 修 (v3 全面审计): 每个 helper 拿自己的 local_abort,
        # 注册到 Registry 的也是这同一个 → processes.kill(this_helper) 只杀本 helper
        # Bug B 修 (2026-05-02): 每个 helper 拿一个 wait_for_register Event,
        # helper 第一帧等这个 Event,handle_delegate 在 _register_helper_with_autoclean
        # 完成后 set,确保 helper 的心跳汇报能找到自己的 proc_id(无 race)。
        helper_tasks = []
        # helper_abort_pairs 已在 try 外预先初始化(except CancelledError 路径在 helper
        # 还没创建时也要安全访问)— 这里不重复定义,直接 append。
        _user_lang_now = current_user_lang()  # 2026-05-02 part12 Bug C
        # 2026-05-15 P98: 计算本批 expected_outputs 集合 (供 P35 排除 same-batch 兄弟)
        _batch_sibling_outputs2: set[str] = set()
        for _s in initial_helper_specs:
            for _o in (_s.get("expected_outputs") or []):
                if isinstance(_o, str) and _o.strip():
                    _batch_sibling_outputs2.add(_o)
        for spec in initial_helper_specs:
            per_helper_abort = asyncio.Event()  # 每 helper 独立 abort
            register_done = asyncio.Event()     # registry 写完前 helper 不开干
            _own_outputs2 = set(spec.get("expected_outputs") or [])
            _siblings_only2 = _batch_sibling_outputs2 - _own_outputs2
            task = asyncio.create_task(_run_one_helper(
                task_id=spec["task_id"],
                prompt=_task_prompt_for_helper(spec),
                main_workspace=main_workspace,
                helper_workspace=spec["workspace"],
                archive_id=archive_id,
                group_id=group_id,
                user_id=user_id,
                resume=spec["resume"],
                local_abort=per_helper_abort,
                wait_for_register=register_done,
                user_lang=_user_lang_now,
                kind=spec.get("kind", "code"),
                mode=spec.get("mode", "easy"),
                helper_think=args.get("helper_think", False),
                input_files=spec.get("input_files") or [],
                expected_outputs=spec.get("expected_outputs") or [],  # 1.C
                write_scopes=spec.get("write_scopes") or spec.get("expected_outputs") or [],
                acceptance_checks=spec.get("acceptance_checks") or [],
                batch_sibling_outputs=_siblings_only2,  # P98
            ))
            await _register_helper_with_autoclean(
                owner=main_owner,
                task=task,
                helper_task_id=spec["task_id"],
                helper_workspace=spec["workspace"],
                abort_event=per_helper_abort,  # ← 注册的是 per-helper,不是 shared
                description=f"main delegate: {spec['prompt'][:80]}",
                helper_kind=spec.get("kind", "code"),
                archive_id=archive_id,
                group_id=group_id,
                user_id=user_id,
            )
            register_done.set()  # registry 写完,helper 可以开干了
            helper_tasks.append(task)
            helper_task_ids[task] = spec["task_id"]
            helper_abort_pairs.append((task, per_helper_abort))

        # ── 接管旧 helper 的 asyncio.Task,由本次 wait loop 等结果 ──
        if _adopted_old_tasks:
            helper_tasks.extend(_adopted_old_tasks)
            for _task, _tid in zip(_adopted_old_tasks, _adopted_task_ids):
                helper_task_ids[_task] = _tid
            debug.log(
                "delegate.adopted_tasks",
                f"adopted {len(_adopted_old_tasks)} existing helper tasks "
                f"into current wait loop",
            )

        n_initial = len(helper_tasks)

        import time as _time
        _start = _time.monotonic()

        for _task, _tid in list(helper_task_ids.items()):
            _ensure_completion_event(trace_id, _tid)

            def _cache_late_result(t: asyncio.Task, *, _tid=_tid, _trace=trace_id):
                if t.cancelled():
                    return

                async def _store_if_not_collected():
                    await asyncio.sleep(0.05)
                    if getattr(t, "_delegate_result_collected", False):
                        return
                    try:
                        res = t.result()
                    except BaseException as e:
                        res = {
                            "task_id": _tid,
                            "ok": False,
                            "report": f"helper crashed: {type(e).__name__}: {e}",
                            "terminal_reason": "crashed",
                            "crash_type": type(e).__name__,
                        }
                    await _store_pending_result(_trace, _tid, res, t)

                from app.core.bg_tasks import schedule
                schedule(_store_if_not_collected(), name=f"store_late_pending_{_tid}")

            _task.add_done_callback(_cache_late_result)

        # ── 2026-05-02 part8 改:不再启动每 15s 的 summarizer 后台 loop ──
        # 改为主模型主动请求(processes(action="list", with_summary=true)按需触发一次)。
        # 旧 loop:7 helper 跑 5min = 20 次 lite 调用 ≈ 20s 算力,而主模型实测从未读
        # 这个字段(主模型决策只看 iter / recent_tools / last_thought 已够诊断)。
        # 新设计:模型自己决定何时要看摘要(怀疑某 helper 卡住时),触发一次 8s timeout
        # 同步 lite 调用,挂在 list 响应里。零浪费、阻塞短。

        # ── 动态 wait loop:支持 spawn_queue 中途加入新 helper ──
        # wait_window_sec 触发时会留下未完成 helper 的 task 引用,
        # response 构造时通过 ProcessRegistry 反查心跳汇报给主线程。
        results = await _dynamic_wait_loop(
            helper_tasks, spawn_queue,
            # 2026-06-05: ✗(failed)/⏸(stuck/interrupted) 是内部系统事件, 不应
            # 作为用户可见的进度里程碑。只对 ok=true 的 ✓ helper 输出 progress。
            on_done=lambda r, total: (
                debug.log(
                    "delegate.progress",
                    f"✓ {r.get('task_id', '?')} done in {_time.monotonic() - _start:.1f}s "
                    f"({total}/? helpers complete)",
                ) if r.get('ok') else None
            ),
            wait_window_sec=wait_window_sec,
            min_results_to_return=min_results_to_return,  # 2026-05-03 优化 #1
            twin_map=_twin_map,                            # hard-mode paired race
            main_owner=main_owner,
            guard_task=_guard_task,                        # 2026-05-10 P83 人设守卫
            helper_specs=initial_helper_specs,
        )

        # Guard/blocking paths return a dict, not list[dict]. Return it directly
        # so the main model sees the guard's free-form reason and can replan or
        # re-dispatch with dispatch_reason.
        if isinstance(results, dict):
            return json.dumps(results, ensure_ascii=False)

        # request_resource is a freeze/report path only. Round2/main decides
        # whether an existing sibling resource helper satisfies it, whether to
        # spawn a new resource helper, or whether to refuse/kill/resume.

    except asyncio.CancelledError:
        # 2026-05-02 part7:用户 abort = 暂停语义,不再硬 cancel helper task。
        # 先给所有未完成 helper set abort_event,让它们走 forced finalize:
        #   - chat_with_tools_loop racing 看到 abort → break → forced finalize 出报告
        #   - 报告写入 .helper_summary.txt → workspace 保留 → orchestrator 收尾
        #     扫描 _delegate_*/.helper_summary.txt 收集到 pause snapshot
        # 等最多 8 秒(实测 forced finalize ~1-2s 完成,8s 是宽松上限),
        # 之后还没退的 helper 才真的 cancel(防止极端情况资源泄漏)。
        debug.log(
            "delegate.abort.coop_first",
            f"signaling cooperative abort to {len(helper_abort_pairs)} helper(s) "
            f"before falling back to hard cancel",
        )
        for _t, _ev in helper_abort_pairs:
            if not _t.done():
                _ev.set()
        # 给 forced finalize 一段时间(任何一个都不会 raise — return_exceptions=True)
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *[_t for _t, _ in helper_abort_pairs if not _t.done()],
                    return_exceptions=True,
                ),
                timeout=8.0,
            )
        except asyncio.TimeoutError:
            debug.log(
                "delegate.abort.coop_timeout",
                "8s cooperative window expired; hard-cancelling stragglers",
            )
        except Exception:
            log.exception("error during cooperative abort wait (non-fatal)")
        # 还没 done 的少数 helper 真 cancel(资源安全兜底)
        for _t, _ in helper_abort_pairs:
            if not _t.done():
                _t.cancel()
        raise
    finally:
        reset_current_owner(owner_token)
        reset_current_spawn_queue(queue_token)
        # 2026-05-21: 守卫 task 若在 helper 全部先完成时仍 pending,需主动 cancel,
        # 否则测试/运行退出时报 "Task was destroyed but it is pending"(persona_guard_*)。
        try:
            if _guard_task is not None and not _guard_task.done():
                _guard_task.cancel()
        except Exception:
            pass

    # 2026-05-05: delegate 耗时总览,便于追踪瓶颈
    _elapsed = _time.monotonic() - _start
    # 2026-05-21: helper 的 elapsed_sec 从首次 spawn 起算(生命周期累计),而 batch total 从
    # 本次 handle_delegate 调用起算。当一个 helper 跨多次 wait_window 续等时,会出现
    # elapsed_sec > total 的"矛盾"(实测 btree=1000.8s vs total=409.1s)。这不是 bug,是两个
    # 不同语义的时间;给超过本批 total 的 helper 标注 (累计),避免运维误读为数据错误。
    def _fmt_helper(r):
        _es = r.get("elapsed_sec", "?")
        try:
            _tag = " 累计" if (isinstance(_es, (int, float)) and _es > _elapsed + 1.0) else ""
        except Exception:
            _tag = ""
        return f"{r.get('task_id','?')}={_es}s{_tag}"
    _per_helper = ", ".join(_fmt_helper(r) for r in results)
    # 2026-05-21: wait_window timeout 时 results 可能为空(helper 仍在后台跑),
    # 旧版 per_helper 为空白 → 运维分不清\"0 helper\"还是\"N 个还在跑\"。
    # 补一个仍在运行的计数,提升可观测性(纯日志,不改控制流)。
    _still_running = sum(1 for _t, _ in helper_abort_pairs if not _t.done())
    if not _per_helper:
        _per_helper = f"(无已完成结果; {_still_running} helper 仍在后台运行)"
    elif _still_running:
        _per_helper += f" | +{_still_running} 仍在运行"
    # ── 安全墙触发计数(stuck_reason 含 "safety timeout")──
    # 2026-05-02: 已撤掉 helper safety timeout,这个计数应该恒为 0,
    # 字段保留作历史 trace 调试用。
    safety_timeout_count = sum(
        1 for r in results
        if "safety timeout" in (r.get("stuck_reason", "") or "")
    )
    # 2026-05-02 Bug C 修:
    #   旧版 helpers_total = len(results) 把"已完成数"当"总数",
    #   wait_window 触发时还有 helper 在跑,主线程读到 "2/2 ok" 误以为全部完成。
    # 新版分三个独立字段,语义清晰:
    #   helpers_initially_spawned: 主进程一次性 spawn 的初始数(永远诚实)
    #   helpers_completed: 在 results 里有结果的(已结束 = ok/interrupted/stuck/error 任一)
    #   helpers_still_running: 仍在后台跑、未在本次 collect 里返回的(wait_window 触发才会非 0)
    n_results = len(results)                      # 已收的结果数
    n_spawned_via_fork = max(0, n_results - n_initial)  # spawn_helper fork 出来的
    helpers_completed = n_results
    helpers_still_running = max(
        0, n_initial + n_spawned_via_fork - helpers_completed
    )

    # 按提交顺序重排结果——as_completed 按完成时间排,会让模型困惑"我交的
    # task[0] 在哪"。重排后 LLM 看到的顺序与它的 tasks 数组顺序一致;
    # 后追加的 spawn 出来的 helper 排在末尾(模型可以从 task_id 看出 .fork 后缀)。
    # 2026-05-08 优化: 占位 result 精简——missing result 仅在 helper 真死/未注册时
    # 出现。当 task_id 同时在 still_running 时(wait_window 超时但 helper 健在),
    # 不再生成占位 placeholder——避免主线程同时看到"results 里 ok=false missing"
    # 和"still_running 里 iter=8 健康"的矛盾视角。下面 still_running 构造完后,
    # 在响应阶段把同名 placeholder 从 ordered 里筛掉。
    by_tid = {r.get("task_id"): r for r in results}
    initial_ordered = [
        by_tid.pop(c["task_id"], {
            "task_id": c["task_id"],
            "ok": False,
            "report": "(missing result — helper not yet finished)",
            "_still_running_placeholder": True,
        })
        for c in cleaned
    ]
    spawned_ordered = list(by_tid.values())
    ordered = initial_ordered + spawned_ordered

    # 2026-06-05: 同一 fix — ok=true 且 interrupted/stuck=true 不应算成功
    ok_count = sum(
        1 for r in ordered
        if r.get("ok") and not r.get("interrupted") and not r.get("stuck")
    )
    resource_required_count = sum(
        1 for r in ordered
        if r.get("terminal_reason") == "resource_required" or r.get("resource_required")
    )
    # A resource-frozen helper may also carry interrupted=true because the
    # local loop stops to preserve its workspace. Count it once as
    # resource_required so the main process sees one blocker, not two failures.
    partial_artifact_results = [
        r for r in ordered
        if (
            (r.get("interrupted") or r.get("stuck"))
            and (
                bool(r.get("outputs_check", {}).get("outputs_complete"))
                or bool(r.get("user_visible_files"))
                or bool(r.get("workspace_files"))
                or bool(r.get("files"))
            )
            and r.get("terminal_reason") != "resource_required"
            and not r.get("resource_required")
        )
    ]
    _partial_artifact_ids = {
        str(r.get("task_id") or "")
        for r in partial_artifact_results
        if str(r.get("task_id") or "")
    }
    interrupted_count = sum(
        1 for r in ordered
        if r.get("interrupted")
        and r.get("terminal_reason") != "resource_required"
        and not r.get("resource_required")
        and str(r.get("task_id") or "") not in _partial_artifact_ids
    )
    stuck_count = sum(
        1 for r in ordered
        if r.get("stuck")
        and r.get("terminal_reason") != "resource_required"
        and not r.get("resource_required")
        and str(r.get("task_id") or "") not in _partial_artifact_ids
    )
    # 2026-05-09 Patch 24: helper 异常路径(_copy_results_to_main 内部 crash 等)
    # 走 except 分支返回 {ok:False, report:"执行失败:..."},不会带 stuck/interrupted=True。
    # 旧版 aggregate 只看 ok/interrupted/stuck 三类,使得 timing_summary 报
    # `ok=0 interrupted=0 stuck=0 completed=2` — 主线程完全看不出哪些"完成但失败了"。
    # 2026-05-23: 基于最终 ordered 统计,避免自动资源恢复后同 task_id 的旧冻结结果
    # 仍被算作 failed。
    failed_count = sum(
        1 for r in ordered
        if (
            not r.get("ok")
            and not r.get("interrupted")
            and not r.get("stuck")
            and r.get("terminal_reason") != "resource_required"
            and not r.get("_still_running_placeholder")
        )
    )
    quality_blocked_count = sum(
        1 for r in ordered
        if r.get("terminal_reason") == "quality_blocked" or r.get("quality_blocked")
    )
    incomplete_count = (
        interrupted_count
        + stuck_count
        + failed_count
        + resource_required_count
        + quality_blocked_count
    )
    task_ok = (
        helpers_completed > 0
        and ok_count > 0
        and helpers_still_running == 0
        and incomplete_count == 0
    )
    debug.log(
        "delegate.timing_summary",
        f"total={_elapsed:.1f}s ok={ok_count} interrupted={interrupted_count} "
        f"stuck={stuck_count} failed={failed_count} quality_blocked={quality_blocked_count} | per_helper: {_per_helper}",
    )
    debug.log(
        "delegate.done",
        f"completed={helpers_completed} (initial={n_initial}, "
        f"forked_in_during_run={n_spawned_via_fork}), "
        f"ok={ok_count}, interrupted={interrupted_count}, stuck={stuck_count}, "
        f"failed={failed_count} "
        f"(total elapsed {_time.monotonic() - _start:.1f}s)",
    )

    # ── 多数撞墙判定(实测 trace 8b60c2: 6/7 helper 全 safety timeout)──
    # 2026-05-02: helper safety timeout 已撤,该字段恒为 false,保留作历史兼容。
    batch_timeout_majority = (
        safety_timeout_count >= 2 and safety_timeout_count * 2 >= helpers_completed
    )

    # ── still_running 计算(wait_window 触发时存在)──
    # 完成的 task_id 集合
    completed_tids = {r.get("task_id") for r in results if r.get("task_id")}
    # 注:n_spawned_via_fork 是 已在 results 里的 fork 数;wait_window 后还在跑的 fork
    # helper 不会出现在 results,所以 still_running 主要反映 initial helper 的 pending。
    # 严格的 "totally spawned" 包括 fork 中途加入还没结束的,这部分在下面 still_running 数组里。

    response = {
        "ok": True,
        # 2026-05-08 优化: 顶层字段精简——
        # 旧版有 helpers_forked_during_run / any_interrupted / any_stuck /
        # safety_timeout_count / batch_timeout_majority。
        # 前两个 0/derivable,后两个 since 2026-05-02 永远是 0/false (helper safety
        # timeout 已撤)。LLM 只需要 4 个核心计数 + 总时长 + results。
        # 2026-05-09 Patch 24: 加 failed_count(异常路径 helper),旧版只有
        # ok/interrupted/stuck 三类,"完成但出错"的 helper 在 aggregate 看不出 →
        # 主线程错过 escalation 信号。failed_count > 0 是重要决策依据。
        "helpers_initially_spawned": n_initial,
        "helpers_completed": helpers_completed,
        "helpers_still_running": helpers_still_running,
        "task_ok": task_ok,
        "success_count": ok_count,
        "incomplete_count": incomplete_count,
        "interrupted_count": interrupted_count,
        "stuck_count": stuck_count,
        "failed_count": failed_count,
        "partial_artifact_count": len(partial_artifact_results),
        "quality_blocked_count": quality_blocked_count,
        "resource_required_count": resource_required_count,
        "total_elapsed_seconds": round(_time.monotonic() - _start, 1),
        "results": ordered,
    }
    if partial_artifact_results:
        response["partial_artifacts"] = [
            {
                "task_id": r.get("task_id"),
                "terminal_reason": r.get("terminal_reason"),
                "user_visible_files": r.get("user_visible_files") or [],
                "workspace_files": r.get("workspace_files") or r.get("files") or [],
                "outputs_check": r.get("outputs_check") or {},
            }
            for r in partial_artifact_results[:8]
        ]
        response["_partial_artifact_policy"] = (
            "Some interrupted or stuck helpers already produced artifacts. This is not an automatic PASS. "
            "Treat the listed files and outputs_check as facts, then decide whether targeted inspection/verification is needed "
            "to accept, repair, or resume the same task_id.\n\n"
            "部分中断/卡住 helper 已有产物；这不是自动完成。请把文件存在与验收信息作为事实，再判断是否需要定向读取/验证。"
        )
    if not task_ok:
        response["_task_status"] = (
            "incomplete" if helpers_still_running or incomplete_count else "no_successful_helper"
        )
        response["_ok_field_meaning"] = (
            "ok=true means the delegate tool returned this runtime snapshot. "
            "task_ok=false means the delegated work is not yet a verified completed task.\n\n"
            "ok 表示工具返回了运行快照；task_ok 才表示委托任务是否完成。"
        )
        response["_evidence_policy"] = (
            "Helper reports with ok=false, interrupted=true, stuck=true, "
            "terminal_reason=resource_required, terminal_reason=quality_blocked, blocking quality warnings, "
            "or missing outputs describe failure or blocker state. Use successful helper outputs, verified artifacts, "
            "or main-thread tool results for exact task facts.\n\n"
            "失败/阻塞 helper 报告只说明状态；事实结论来自成功产物、验收证据或主线程工具结果。"
        )
    # Clean helper batches are trustworthy content evidence for their owned
    # outputs. The main thread still owns external acceptance boundaries such as
    # missing final artifacts, project apply/diff, and verifier commands.
    if (helpers_completed > 0
            and helpers_still_running == 0
            and interrupted_count == 0
            and stuck_count == 0
            and failed_count == 0
            and all(
                r.get("_post_helper_action") == "output_json_directly"
                for r in ordered if r.get("ok")
            )):
        response["_stage_status"] = "clean_helper_batch"
        response["_stage_evidence_facts"] = (
            "All helpers in this delegate batch completed cleanly and their declared outputs are available. "
            "Trust the successful helpers' content judgment for the files they owned. The main process should not "
            "re-read helper-produced text, Markdown, source, or project artifacts merely to re-verify content. Continue "
            "only for separate task boundaries: missing requested final artifacts, project apply/diff, explicit external "
            "verifier/check commands, helper warnings or contradictions, or a user request to quote/display file content.\n"
            "本批 helper 已干净完成且产物可用；信任 helper 对其产物内容的判断，主进程只处理缺失交付物、项目应用/差异、外部验收、警告矛盾或用户显式展示需求。"
        )
        response["_completion_guidance"] = (
            response["_stage_evidence_facts"]
            + "\nIf no separate boundary remains in the active task, synthesize/finalize from these compact helper facts. "
            "A clean helper batch by itself does not require another main-thread read, edit, or verification loop.\n"
            "若当前任务没有独立边界，直接用 helper 精简事实综合/收尾；干净 helper 批次本身不要求主进程再读、再改或再验。"
        )
    # 仅在 fork 数 > 0 时才暴露给 LLM(避免 0 噪音)
    if n_spawned_via_fork > 0:
        response["helpers_forked_during_run"] = n_spawned_via_fork
    # batch_timeout_majority 仅历史 trace 触发时才有意义
    if batch_timeout_majority:
        response["batch_timeout_majority"] = True

    resource_required = [
        r for r in ordered
        if r.get("terminal_reason") == "resource_required" or r.get("resource_required")
    ]
    if resource_required:
        response["resource_required"] = [
            {
                "task_id": r.get("task_id"),
                **(r.get("resource_required") or {}),
            }
            for r in resource_required
        ]
        response["error_kind"] = "helper_resource_required"
        response["error_summary"] = (
            f"{len(resource_required)} helper(s) are frozen and waiting for main-process resources. "
            "Treat their partial artifacts as recovery evidence, not as completed deliverables.\n\n"
            "helper 已冻结等待资源；部分产物不能当完整交付。"
        )
        response["_resource_recovery_facts"] = (
            "`resource_required[].needed_outputs`, existing helper outputs, same-batch resources, concrete resource paths, "
            "resource refusal, and the frozen helper state are the relevant recovery facts. The active task determines "
            "whether same-task resume, a resource helper, refusal/reporting, or cooperative interruption fits.\n\n"
            "资源需求、已有产物、同批资源、资源路径、拒绝事实和冻结状态是恢复事实；续作、派资源、拒绝或中断由当前任务决定。"
        )
        response["_action_required"] = response["_resource_recovery_facts"]

    # ── wait_window 触发: 把还在跑的 helper 心跳快照加进 response (2026-05-02 加) ──
    # 主线程拿到部分结果 + 未完成 helper 心跳后,可以决定:
    # (1) 继续等(再次调 delegate(action="spawn", resume_proc_ids=...) 或类似机制
    #     —— 当前没实现,让 helper 自然跑到 safety_timeout 30min 然后再 spawn 也行)
    # (2) kill 表现差的 helper(processes.kill(proc_id=...))
    # (3) 拿现有结果给用户回复(放弃没完成的)
    still_running: list[dict] = []
    if wait_window_sec is not None:
        # 通过 task_id 反查每个未完成 helper 的 ProcessHandle
        completed_tids = {r.get("task_id") for r in results if r.get("task_id")}
        for spec in cleaned:
            tid = spec["task_id"]
            if tid in completed_tids:
                continue
            # 找 helper 心跳
            try:
                h = await proc_registry().find_helper_by_task_id(
                    tid, owner=main_owner,
                )
                if h is None:
                    # owner 严格匹配找不到(可能 helper 被 unregister),用 trace 范围找
                    h = await proc_registry().find_helper_by_task_id(
                        tid, same_trace_as=main_owner,
                    )
                if h is not None:
                    # 2026-05-08 优化: 字段精简——
                    # 旧版同时返回 started_at_iso + running_for_sec + last_progress_iso
                    # + last_heartbeat_age_sec(4 个时间字段中两两冗余)。
                    # LLM 没有 wall-clock context, ISO 时间戳无意义,只用相对秒数。
                    # workspace_files 从 20 降到 8(主线程 kill 决策不需要完整清单)。
                    _now_t = time.time()
                    started_at = getattr(h, "registered_at", None) or getattr(h, "started_at", None)
                    entry = {
                        "task_id": tid,
                        "proc_id": h.proc_id,
                        "iter": h.last_iter,
                        "recent_tools": list(h.recent_tools[-5:]),
                        "last_thought": h.last_thought_preview,
                        "last_heartbeat_age_sec": (
                            round(_now_t - h.last_progress_at, 1)
                            if h.last_progress_at > 0 else None
                        ),
                    }
                    if isinstance(started_at, (int, float)) and started_at > 0:
                        entry["running_for_sec"] = round(_now_t - started_at, 1)
                    try:
                        public = h.to_public_dict()
                        wait_or_continue = str(public.get("wait_or_continue") or "").strip()
                        if wait_or_continue:
                            entry["wait_or_continue"] = wait_or_continue
                        if public.get("_runaway"):
                            entry["_runaway"] = True
                            entry["_runaway_reason"] = str(public.get("_runaway_reason") or "")
                            entry["severity"] = "high"
                            entry["attention_fact"] = "helper reports runaway risk; unchanged waiting may waste context and call budget"
                    except Exception:
                        pass
                    # ── workspace 文件清单(让主线程知道 helper 有什么 partial 产物)──
                    try:
                        ws_path = str(base / f"_delegate_{user_tag}_{tid}")
                        if os.path.isdir(ws_path):
                            _ws_all = sorted(os.listdir(ws_path))
                            entry["workspace_files"] = _ws_all[:8]
                            entry["workspace_file_count"] = len(_ws_all)
                            # 2026-05-05: early return 时合并 _helpers_shared/ 到主区,
                            # 让兄弟 helper 的 bug 修复/共享文件可以传播
                            _hs_src = os.path.join(ws_path, "_helpers_shared")
                            if os.path.isdir(_hs_src):
                                try:
                                    import shutil as _shutil
                                    _hs_dst = os.path.join(main_workspace, "_helpers_shared")
                                    os.makedirs(_hs_dst, exist_ok=True)
                                    for _sf in os.listdir(_hs_src):
                                        _sp = os.path.join(_hs_src, _sf)
                                        _dp = os.path.join(_hs_dst, _sf)
                                        if os.path.isfile(_sp):
                                            _shutil.copy2(_sp, _dp)
                                    debug.log(
                                        f"delegate.{tid}.helpers_shared",
                                        "merged _helpers_shared/ from still-running helper",
                                    )
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    still_running.append(entry)
                else:
                    still_running.append({
                        "task_id": tid,
                        "proc_id": None,
                        "note": "helper not in registry (may have just finished or never registered)",
                    })
            except Exception:
                log.exception(
                    "failed to snapshot still-running helper %s heartbeat", tid,
                )
        # 2026-05-08: 把被 duplicate_protect 接管的旧 task 也纳入 still_running
        # (adopted tasks 不在 cleaned 里,单独补查)
        for tid in _adopted_task_ids:
            if tid in completed_tids:
                continue
            try:
                h = await proc_registry().find_helper_by_task_id(
                    tid, owner=main_owner,
                )
                if h is None:
                    h = await proc_registry().find_helper_by_task_id(
                        tid, same_trace_as=main_owner,
                    )
                if h is not None:
                    # 2026-05-08 优化: 同上,精简时间字段
                    _now_t2 = time.time()
                    started_at = getattr(h, "registered_at", None) or getattr(h, "started_at", None)
                    entry = {
                        "task_id": tid,
                        "proc_id": h.proc_id,
                        "iter": h.last_iter,
                        "recent_tools": list(h.recent_tools[-5:]),
                        "last_thought": h.last_thought_preview,
                        "last_heartbeat_age_sec": (
                            round(_now_t2 - h.last_progress_at, 1)
                            if h.last_progress_at > 0 else None
                        ),
                        "adopted": True,  # 标记为接管的旧任务
                    }
                    if isinstance(started_at, (int, float)) and started_at > 0:
                        entry["running_for_sec"] = round(_now_t2 - started_at, 1)
                    try:
                        public = h.to_public_dict()
                        wait_or_continue = str(public.get("wait_or_continue") or "").strip()
                        if wait_or_continue:
                            entry["wait_or_continue"] = wait_or_continue
                        if public.get("_runaway"):
                            entry["_runaway"] = True
                            entry["_runaway_reason"] = str(public.get("_runaway_reason") or "")
                            entry["severity"] = "high"
                            entry["attention_fact"] = "helper reports runaway risk; unchanged waiting may waste context and call budget"
                    except Exception:
                        pass
                    still_running.append(entry)
                else:
                    still_running.append({
                        "task_id": tid,
                        "proc_id": None,
                        "note": "adopted helper not in registry (may have just finished)",
                        "adopted": True,
                    })
            except Exception:
                log.exception(
                    "failed to snapshot adopted helper %s heartbeat", tid,
                )
        if still_running:
            response["wait_window_expired"] = True
            response["wait_window_sec"] = wait_window_sec
            response["still_running"] = still_running
            runaway_helpers = [
                e for e in still_running
                if e.get("_runaway") or str(e.get("wait_or_continue") or "").lower() in {"intervene", "kill"}
            ]
            if runaway_helpers:
                runaway_tids = [e.get("task_id") for e in runaway_helpers if e.get("task_id")]
                response["error_kind"] = "helper_runaway_requires_intervention"
                response["error_summary"] = (
                    f"{len(runaway_helpers)} helper(s) need intervention: {runaway_tids}. "
                    "Waiting unchanged will keep spending context and call budget.\n\n"
                    "helper 需要介入；不要原样继续等待。"
                )
                response["runaway_helpers"] = runaway_helpers
                response["_runaway_recovery_facts"] = (
                    "runaway_helpers are a blocking workflow state. Partial files, last_thought, heartbeat freshness, "
                    "allowed kill reasons, usable partial evidence, and the current acceptance boundary are the relevant "
                    "facts for deciding whether waiting, collecting, cooperative interruption, a smaller helper, or a "
                    "clearly marked PARTIAL result fits.\n"
                    "跑飞 helper 是阻塞状态；部分产物、last_thought、心跳、kill 理由、可用证据和验收边界是恢复事实。"
                )
                response["_action_required"] = response["_runaway_recovery_facts"]
                response["escalation_advice"] = (
                    "⚠ helper_runaway_requires_intervention: "
                    f"task_ids={runaway_tids}. Stop waiting unchanged; resolve or split these helpers before fan-in.\n"
                    + response.get("escalation_advice", "")
                ).strip()
            # 2026-05-17 P154: 加强警告 — wait_window 触发且 0 个 helper 完成
            # (此前实测: 主线程把 ok=true + helpers_still_running>0 当成"已完成"
            #  继续 spawn 新任务, 触发 prompt_silently_dropped 螺旋。)
            # 在这个特殊场景下,把 response.ok 改为 false 风格的更强信号。
            if helpers_completed == 0:
                _all_running_secs = [
                    float(e.get("running_for_sec") or 0)
                    for e in still_running
                    if isinstance(e.get("running_for_sec"), (int, float))
                ]
                _max_running_sec = max(_all_running_secs) if _all_running_secs else 0.0
                _max_workspace_files = max(
                    [int(e.get("workspace_file_count") or 0) for e in still_running]
                    or [0]
                )
                _high_iter_tids = [
                    e.get("task_id") for e in still_running
                    if isinstance(e.get("iter"), int) and e.get("iter") >= 15
                ]
                _overbroad_active = (
                    len(still_running) <= 2
                    and _max_running_sec >= 240
                    and (_max_workspace_files >= 150 or _high_iter_tids)
                )
                response["error_kind"] = "wait_window_zero_completion"
                response["error_summary"] = (
                    f"wait_window={wait_window_sec}s expired with {len(still_running)} helper(s) still running and zero completions. "
                    "results=[] means no report has arrived yet; it does not mean the helpers failed.\n\n"
                    "等待窗口到期但 helper 仍在跑；先看心跳再决定。"
                )
                response["_wait_window_recovery_facts"] = (
                    "The same still_running task_id cannot accept a new prompt while its helper is live. Healthy heartbeat, "
                    "pathological heartbeat, helper reports after cooperative interruption, and clearly available partial outputs "
                    "are the relevant facts for deciding whether collect/wait_any, cooperative kill, or partial delivery fits.\n\n"
                    "运行中的同 task_id 不能接收新 prompt；心跳、协作中断报告和可用部分产物是恢复事实。"
                )
                response["_action_required"] = response["_wait_window_recovery_facts"]
                if _overbroad_active:
                    response["active_overbroad_warning"] = {
                        "issue": "active_but_no_results_after_long_wait",
                        "severity": "high",
                            "running_for_sec_max": round(_max_running_sec, 1),
                            "workspace_file_count_max": _max_workspace_files,
                            "high_iter_task_ids": [tid for tid in _high_iter_tids if tid],
                            "details": (
                                "A healthy heartbeat with long zero-result runtime can mean the task boundary is too broad, the workspace is noisy, "
                                "or the helper is still producing useful partial evidence. Current heartbeat/status, partial files, dependency facts, "
                                "and acceptance evidence should determine whether to wait, collect, interrupt, replan, split, or retry.\n\n"
                                "健康但久无结果是事实信号；等待、收集、中断、重派、拆分或重试由主模型结合证据判断。"
                            ),
                        }
                    debug.log(
                        "delegate.wait_window.active_overbroad_warning",
                        f"0 results after {wait_window_sec}s; max_running={_max_running_sec:.1f}s, "
                        f"max_workspace_files={_max_workspace_files}, high_iter={_high_iter_tids}",
                    )
            # 2026-05-08 优化(防混淆): 当 task_id 已经在 still_running(helper 健在)
            # 时, 把 results 里同 task_id 的 placeholder("missing result")筛掉。
            # 否则主线程同时看到"results.framework: ok=false missing"和
            # "still_running.framework: iter=8 健康"两个矛盾视图,实测会导致
            # 错误 kill 健康 helper(trace 952a8a 12:53:38)。
            _running_tids = {e.get("task_id") for e in still_running if e.get("task_id")}
            response["results"] = [
                r for r in response["results"]
                if not (r.get("task_id") in _running_tids and r.get("report", "").startswith("(missing result"))
            ]
            response["escalation_advice"] = (
                response.get("escalation_advice", "") +
                f" wait_window={wait_window_sec}s expired while {len(still_running)} helper(s) are still running. "
                "Use still_running heartbeat to choose: healthy progress -> collect or wait_any with a larger window; pathological repetition -> cooperative kill and inspect the report; "
                "self-reported completion -> wait for natural done; repeated failure -> fix kind, resources, paths, dependency order, task scope, or acceptance evidence. "
                "Use mode='hard' only as a stricter same-kind retry after root-cause review.\n\n"
                "根据心跳决定等待、协作中断、修根因或同类 hard 续作。"
            ).strip()
        # 2026-05-08 新增: silent prompt drop 通知。LLM resume=true 时给的新 prompt
        # 在 helper 还活着时被丢弃(helper 在自己旧 context 里继续跑)。把丢弃的
        # prompt 摘要回传, LLM 才知道自己的指令没生效——要换方向先 kill 再重起。
        # 不论 helper 是否在本次 wait_window 内完成都通知——helper 是基于旧 prompt
        # 跑的,result 反映的是旧方向不是 LLM 这次给的新方向。
        #
        # 2026-05-15 P66 升级: 提升为 top-level error_kind, 防 LLM 忽略 hint。
        # 病因(实测 05-15 16:31, 17:07 trace): 主线程 2 次给 running helper 传新 prompt,
        # 第 1 次系统给了 hint, 主线程 LLM 没改行为, 第 2 次又重复同样错误浪费一轮 iter。
        # 修法: 把警告提到 response 顶层, 加 error_kind 字段, hint 用 "🚨" 前缀,
        # 让 LLM 一眼看到不能错过。
        if _adopted_dropped_prompts:
            response["adopted_with_dropped_prompts"] = _adopted_dropped_prompts
            # P66: top-level error_kind 字段, 主线程 LLM 必须先看这个
            response["error_kind"] = "helper_still_running_prompt_dropped"
            response["error_summary"] = (
                f"This spawn/resume request included {len(_adopted_dropped_prompts)} task(s) whose helpers are still alive. "
                f"The new prompt was not adopted; those helpers continue with their existing prompt. "
                f"Task ids: {[d['task_id'] for d in _adopted_dropped_prompts]}.\n\n"
                "运行中的 helper 不会接收新 prompt；先等待或协作中断。"
            )
            _dropped_tids = {d["task_id"] for d in _adopted_dropped_prompts}
            # L8-1 (2026-05-09): 在对应的 result 条目上也标记 prompt_dropped
            for r in response.get("results", []):
                tid = r.get("task_id", "")
                if tid in _dropped_tids:
                    r["prompt_dropped"] = True
                    r["error_kind"] = "prompt_silently_dropped"
                    r["hint"] = (
                        "The new prompt for task_id={tid!r} was not adopted because the helper is still running. "
                        "To change direction, cooperatively kill it, wait for the report, then resume the same task_id with a revised prompt. "
                        "To wait, omit prompt and poll/wait_any the same task_id.\n\n"
                        "运行中的 helper 要么等待，要么先中断再同 task_id 续作。"
                    ).format(tid=tid)
            _drop_advice = (
                "\nadopted_with_dropped_prompts: "
                f"{len(_adopted_dropped_prompts)} task(s) did not adopt the new prompt because their helpers are still alive. "
                "Do not pass another prompt to the same task unless you cooperatively stop the old helper first. "
                "If it finishes naturally, interpret the result as produced from the previous prompt.\n\n"
                "新 prompt 未生效；等待旧 helper 或先中断再续作。"
            )
            # P66: 警告放到 escalation_advice 最前面 (LLM 通常从头读)
            response["escalation_advice"] = (
                _drop_advice + "\n" + response.get("escalation_advice", "")
            ).strip()

    # ── escalation_advice 分级(轻 → 重)──
    # 2026-05-08 清理: batch_timeout_majority 自 2026-05-02 helper safety timeout 撤掉后
    # 永远 False, 旧分支仅历史 trace fallback,移除以减少代码噪音。
    advice_parts = []
    if stuck_count > 0:
        stuck_tids = [r["task_id"] for r in results if r.get("stuck")]
        # 检查是否有 helper 报告了不可行 — 如果是，主线程不应重派
        _infeasible_reports = []
        for r in results:
            if r.get("stuck"):
                report = str(r.get("report", ""))
                if "不可行" in report or "infeasible" in report.lower() or "做不到" in report:
                    _infeasible_reports.append(r["task_id"])
        advice_parts.append(
            f"{stuck_count} helper(s) stopped after repeated failure (task_ids: {stuck_tids}). "
            "Read their reports and change the strategy before continuing.\n\n"
            "反复失败后先读报告并换策略。"
        )
        if _infeasible_reports:
            advice_parts.append(
                f"Helper(s) {_infeasible_reports} reported infeasibility. "
                "Either reduce scope/precision/features and resume, or deliver a clearly marked partial result if that is acceptable.\n\n"
                "不可行时缩小要求续作，或明确部分交付。"
            )
        advice_parts.append(
            f"\nRecovery order: inspect the helper report; preserve usable artifacts; resume the same task_id with a concrete revised direction when possible; "
            f"split the task if the boundary is too broad; start fresh only when existing artifacts are unusable. "
            f"The main process should manage and delegate implementation work rather than writing substantial code itself.\n\n"
            f"优先读报告、保留可用产物、同 task_id 续作或拆分。"
        )
    if advice_parts:
        response["escalation_advice"] = " ".join(advice_parts)

    # ── 2026-05-08: re-delegate 检测 ──
    # 同一 task_id 反复完成 ok=true → 系统强制接受，不再注入重派/修框架建议。
    # 防 _shared_bug_alerts 假阳性等原因导致的无意义重派循环。
    _completions_path = Path(main_workspace) / ".helper_completions.json"
    _completions: dict[str, int] = {}
    if _completions_path.exists():
        try:
            import json as _json2
            _completions = _json2.loads(_completions_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    _repeated_tids: set[str] = set()
    for r in results:
        _tid = str(r.get("task_id", ""))
        _ok = r.get("ok")
        if _tid and _ok is True:
            _count = _completions.get(_tid, 0) + 1
            _completions[_tid] = _count
            if _count >= 3:
                _repeated_tids.add(_tid)
    if _completions:
        try:
            import json as _json2
            _completions_path.parent.mkdir(parents=True, exist_ok=True)
            _completions_path.write_text(_json2.dumps(_completions, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    if _repeated_tids:
        _repeated_list = ", ".join(sorted(_repeated_tids))
        _repeated_advice = (
            f"\n{len(_repeated_tids)} helper task(s) have completed with ok=true at least three times: {_repeated_list}. "
            "Treat these tasks as complete, accept the existing outputs, and move to the next dependency or fan-in step.\n\n"
            "同一任务多次成功时应接受产物，不再重复派发。"
        )
        if response.get("escalation_advice"):
            response["escalation_advice"] = _repeated_advice + "\n" + response["escalation_advice"]
        else:
            response["escalation_advice"] = _repeated_advice.strip()

    # ── 2026-05-05: _shared_ 脚手架 bug 检测 ──
    # 扫描所有 helper 报告,如果多个 helper 提到 _shared_ 内容有 bug,
    # 建议主线程 spawn 一个 fix helper 先修脚手架再重派受影响任务。
    # 2026-05-08: re-delegate 检测会跳过 repeated_tids，防止假阳性导致循环重派。
    _shared_bug_reports: list[dict] = []
    for r in results:
        _tid = str(r.get("task_id", ""))
        if _tid in _repeated_tids:
            continue  # 已完成 ≥3 次，跳过 _shared_ bug 检测（大概率假阳性）
        report = str(r.get("report", ""))
        # 检测报告中对 _shared_ bug 的描述
        # 注意：不能匹配 "_shared_" — 每个 helper 报告都有 _helpers_shared/ 路径，会 100% 误报
        _shared_bug_keywords = [
            "scaffold bug", "runner bug", "bench_runner",
            "test runner", "验证逻辑有", "runner 有", "脚手架有",
            "_shared_ 有", "_shared_ bug", "_shared_ 错",
        ]
        if any(kw.lower() in report.lower() for kw in _shared_bug_keywords):
            _shared_bug_reports.append({
                "task_id": _tid,
                "ok": r.get("ok"),
                "snippet": report[:300],
            })
    if _shared_bug_reports:
        _bug_tids = [br["task_id"] for br in _shared_bug_reports]
        _shared_fix_advice = (
            f"\n{len(_shared_bug_reports)} helper report(s) indicate a possible shared scaffold issue "
            f"(tasks: {_bug_tids}). Spawn a focused code helper to repair the shared scaffold using the concrete report evidence. "
            "Keep helpers that already produced usable workarounds; restart only helpers that need the repaired scaffold.\n\n"
            "共享脚手架异常时派专门 code helper 修复，再按需重跑受影响任务。"
        )
        response["_shared_bug_alerts"] = _shared_bug_reports
        if response.get("escalation_advice"):
            response["escalation_advice"] += _shared_fix_advice
        else:
            response["escalation_advice"] = _shared_fix_advice.strip()

    # ── 2026-05-06: 自动验证建议(已撤,2026-05-07) ──
    # 原设计: 扫描所有 code/hard-mode helper 的 ok=True 结果,未经验证就
    #   标记 verification_needed=true + 注入 escalation_advice,要求主线程
    #   "必须 delegate verify helper,禁止主线程自己验证"。
    # 实测后果(2026-05-07 trace): 主线程收到 advice 后不是 spawn verify helper,
    #   而是 spawn avl_v3 + sbt_v3(code helpers,从头重做)。这些新 helper
    #   完成后又触发新的 verification_needed → 无限循环,永远无法 finalize。
    # 根因: 自动注入的 verification 要求与实际任务完成条件冲突 —
    #   code helper 永远无法"被验证"因为 verify 永远不会自动 spawn。
    #   结果是系统永远停在 round2,不断 spawn 新 _v3/_v4/_vN helper。
    # 正确做法: 模型自己决定何时验证。verify helper 的 spawn 由主线程
    #   基于实际观察决定(如编译 warning、benchmark 结果异常等),而非
    #   系统强制每个 code helper 都需 verify。

    # ── 立即清理已完成 helper 工作区(不等 TTL)──
    # 实测 trace 8b60c2 后续: 22 个 _delegate_* 累计 45GB,即使 TTL=30min 也太慢。
    # 任务成功完成 = 主线程已收到报告 = 工作区不再需要 → 立刻删。
    # 例外: stuck/interrupted helper 保留(用户可能想 resume);
    #      copy_results_to_main 已经把交付物搬到主区,helper 区可以丢。
    # ── helper 工作区立即清理(workspace 爆炸 bug 主修)──
    # 原则: 清理"无 resume 价值"的工作区,只保留可能续作的
    # 清理: 成功 + 普通异常失败(resume 也救不了 import error)
    # 保留: stuck / interrupted(主线程可能用 resume=true 续作)
    #
    # 2026-05-07 Bug 6 fix: 有兄弟 helper 还在跑时跳过清理。
    # 清理掉已完 helper 的 workspace 可能导致仍在跑的 helper 引用失效
    # (_helpers_shared 传播 / 交叉 import)。等所有 helper 都完再一起清。
    cleanup_count = 0
    cleanup_bytes = 0
    completed_task_ids: list[str] = []  # 2026-05-02 part7:这一批自然完成的 task,从 pause_state 清理

    _defer_cleanup = helpers_still_running > 0
    if _defer_cleanup:
        debug.log(
            "delegate.cleanup.deferred",
            f"{helpers_still_running} helper(s) still running; "
            f"deferring workspace cleanup for completed helpers "
            f"(avoid race with sibling references)",
        )

    # ── 2026-05-04 Bug #6 修复:stuck/interrupted helper 强制 abort LLM stream ──
    # 工作区保留以便 resume 是合理的,但 helper 的 asyncio task 此时往往还在 LLM
    # stream 里(thinking_high 30+ 秒一个 chunk)。主线程已经决定不再等它,**保留
    # workspace ≠ 保留 LLM stream**。trace f3a3aafb 实测:bwt_v2 在 12:48:11 标记
    # stuck → 主线程走人 → 但 LLM stream 又跑了 9 分多钟才结束,白烧 token。
    # 修法:即使保留 workspace 供 resume,也通过 ProcessRegistry.kill(force=True)
    # cancel 这个 helper 的 asyncio.Task。下次 resume 时 spawn 全新 helper,
    # 复用 workspace 状态(.helper_summary.txt + 中间文件)即可。
    for r in results:
        if not (r.get("stuck") or r.get("interrupted")):
            continue
        # 仅当 helper 仍在 ProcessRegistry 里(还没自然 finalize)才需要强 kill
        _tid = r.get("task_id") or ""
        if not _tid:
            continue
        try:
            _h = await proc_registry().find_helper_by_task_id(_tid)
            if _h is not None and _h.helper_task is not None and not _h.helper_task.done():
                _kill_res = await proc_registry().kill(
                    _h.proc_id,
                    requested_by=_h.owner,  # self-kill,ACL 必过
                    reason=KILL_REASON_CONTENT_USELESS,
                    force=True,
                )
                debug.log(
                    f"delegate.{_tid}.bg_force_kill",
                    f"stuck/interrupted helper still streaming → force-killed "
                    f"to stop ghost token burn (workspace 保留 for resume): {_kill_res}",
                )
        except Exception:
            log.exception(
                "delegate %s: bg force-kill of stuck helper failed (non-fatal)",
                _tid,
            )

    for r in results:
        if _defer_cleanup:
            break  # 2026-05-07 Bug 6: siblings still running, skip cleanup
        # 保留可 resume 的: stuck/interrupted(它们的报告里建议了 resume=true)
        if r.get("stuck") or r.get("interrupted"):
            continue
        # 其他都清(成功 / 普通异常)
        completed_tid = r.get("task_id")
        if completed_tid:
            completed_task_ids.append(completed_tid)
        helper_ws = r.get("workspace_dir") or r.get("helper_workspace")
        if not helper_ws:
            continue
        try:
            import os as _os
            import shutil as _shutil
            if _os.path.isdir(helper_ws):
                size = sum(
                    _os.path.getsize(_os.path.join(dp, f))
                    for dp, _, fs in _os.walk(helper_ws)
                    for f in fs
                    if _os.path.isfile(_os.path.join(dp, f))
                )
                _shutil.rmtree(helper_ws, ignore_errors=True)
                if not _os.path.isdir(helper_ws):
                    cleanup_count += 1
                    cleanup_bytes += size
        except (OSError, ImportError):
            pass  # 静默失败,稍后 TTL cleanup 会兜底
    if cleanup_count:
        debug.log(
            "delegate.cleanup_immediate",
            f"removed {cleanup_count} helper workspaces "
            f"({cleanup_bytes / 1024 / 1024:.1f} MB freed; "
            f"stuck/interrupted kept for possible resume)",
        )

    # 2026-05-02 part7:从 pause_state 移除自然完成的 task。
    # 上次 chat 暂停时这些 task 被写入 pause_state,这次 chat 模型决定 resume,
    # helper 自然跑完了 → pause_state 中对应条目应清掉,避免下次 chat 还显示
    # "上次 paused"诱导模型再次 resume 一个已经完成的任务。
    # 注:stuck/interrupted 不清(它们仍在 paused 池里,等下次 chat 决定怎么处理)。
    if completed_task_ids:
        try:
            from app.core import pause_state as _ps
            for tid in completed_task_ids:
                await _ps.remove_helper_from_pause(
                    archive_id=archive_id, group_id=group_id, user_id=user_id,
                    task_id=tid,
                )
            debug.log(
                "delegate.pause_state_pruned",
                f"removed {len(completed_task_ids)} completed task(s) from pause_state",
            )
        except Exception:
            log.exception("pause_state prune for completed helpers failed (non-fatal)")

    # ── 记录成功且产物齐全的 task_id，防止 LLM 在同一会话重复 spawn (2026-05-08) ──
    # repair_pairing 修复孤儿 tool_call 后可能让 LLM 产生 "任务还没做" 的幻觉，
    # 看到 delegate 刚返回 ok=true 也要重新 spawn。记录完成状态供下次 spawn 去重。
    # 2026-05-17: expected_outputs 缺失时 outputs_complete=false, 不能登记为已完成，
    # 否则主线程无法 resume 补齐文件。
    for r in results:
        outputs_check = r.get("outputs_check")
        if not isinstance(outputs_check, dict):
            outputs_check = {}
        if outputs_check.get("outputs_complete") is not True:
            continue
        if r.get("ok") and not r.get("interrupted") and not r.get("stuck"):
            tid = r.get("task_id")
            if tid and r.get("report") != "(missing result)":
                try:
                    await proc_registry().mark_recently_completed(tid)
                except Exception:
                    pass

    return json.dumps(response, ensure_ascii=False)


from app.llm.tools.delegate_wait import (  # noqa: E402,F401
    _dynamic_wait_loop,
    _handle_main_kill_helper,
    handle_spawn_helper,
    handle_wait_helper,
)








