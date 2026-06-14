"""Tool-calling LLM loop implementation."""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.llm.tools.runtime_hints import (
    LLM_REPEAT_TIMEOUT_RECOVERY_HINT,
    LLM_RETRY_FAILURE_RECOVERY_HINT,
    LLM_TIMEOUT_RECOVERY_HINT,
    SOURCE_WRITE_DELEGATION_HINT,
    artifact_acceptance_convergence_hint,
    auto_recall_checkpoint,
    helper_finalize_window,
    helper_completed_todos_handoff,
    helper_iter_checkpoint,
    helper_long_run,
    helper_office_read_convergence_checkpoint,
    helper_office_write_convergence_checkpoint,
    helper_pace_check,
    helper_read_to_write_checkpoint,
    helper_repeated_tool_call_bloat_checkpoint,
    helper_tool_call_bloat_checkpoint,
    main_env_run_convergence_hint,
    main_helper_completion_checkpoint,
    main_finalize_window,
    main_milestone_checkpoint,
    repeated_failure,
    retry_before_finalize,
    retry_required_before_final,
    retry_still_required_before_final,
    strategy_recovery,
)
from app.llm.json_utils import stable_prompt_json


def _sync_client_globals() -> None:
    from app.llm import client as _client
    globals().update({
        name: value
        for name, value in vars(_client).items()
        if not name.startswith("__") and name != "chat_with_tools_loop"
    })


_RETRYABLE_DELEGATE_NEXT_ACTIONS = {
    "resume_upgraded",
    "resume_after_crash",
    "resume_same_task_fix_output_format",
}
_RECOVERABLE_DELEGATE_REASONS = {
    "stuck", "interrupted", "timeout", "failed", "error",
    "resource_required", "outputs_missing", "quality_blocked", "crashed",
    "output_format_invalid",
}
_READONLY_HELPER_KINDS = {"read", "ocr", "project_map", "file_summary", "impact_review", "inventory", "summarize", "verify"}


_DELEGATE_MODEL_KEY_RENAMES = {
    "helpers_initially_spawned": "processing_records_started",
    "helpers_requested": "processing_records_requested",
    "background_work_started": "processing_records_started",
    "background_work_requested": "processing_records_requested",
    "helpers_completed": "results_returned",
    "helpers_returned": "results_returned",
    "helpers_still_running": "processing_records_running",
    "background_work_running": "processing_records_running",
    "helpers_unavailable": "processing_records_unavailable",
    "background_work_unavailable": "processing_records_unavailable",
    "helpers_forked_during_run": "processing_records_started_during_run",
    "background_work_started_during_run": "processing_records_started_during_run",
    "active_helpers": "active_processing_records",
    "active_background_work": "active_processing_records",
    "completed_helpers": "completed_processing_records",
    "completed_background_work": "completed_processing_records",
    "runaway_helpers": "runaway_processing_records",
    "runaway_background_work": "runaway_processing_records",
    "helper_name": "source_name",
    "helper_kind": "work_kind",
    "matching_helper_kind": "matching_work_kind",
    "suggested_helper_kind": "suggested_work_kind",
    "helper_prompt_fact": "work_prompt_fact",
    "helper_output_path": "work_output_path",
    "helper_output_fact": "work_output_fact",
    "helper_owned_artifact_fact": "generated_artifact_fact",
    "post_helper_usage_hint": "post_work_usage_hint",
    "helper_resource_required": "processing_record_resource_required",
    "background_work_resource_required": "processing_record_resource_required",
    "helper_route": "work_route",
    "helper_handoff_fact": "processing_handoff_fact",
    "producer_self_verified": "output_self_verified",
    "helper_producer_self_verified": "output_self_verified",
    "producer_boundary_fact": "output_boundary_fact",
    "_post_helper_action": "_post_work_action",
}
_DELEGATE_MODEL_DROP_KEYS = {
    "workspace_dir",
    "helper_workspace",
}
_DELEGATE_MODEL_TEXT_REPLACEMENTS = (
    ("helpers_initially_spawned", "processing_records_started"),
    ("helpers_requested", "processing_records_requested"),
    ("background_work_started", "processing_records_started"),
    ("background_work_requested", "processing_records_requested"),
    ("helpers_completed", "results_returned"),
    ("helpers_returned", "results_returned"),
    ("helpers_still_running", "processing_records_running"),
    ("background_work_running", "processing_records_running"),
    ("helpers_unavailable", "processing_records_unavailable"),
    ("background_work_unavailable", "processing_records_unavailable"),
    ("helpers_forked_during_run", "processing_records_started_during_run"),
    ("background_work_started_during_run", "processing_records_started_during_run"),
    ("active_helpers", "active_processing_records"),
    ("active_background_work", "active_processing_records"),
    ("completed_helpers", "completed_processing_records"),
    ("completed_background_work", "completed_processing_records"),
    ("runaway_helpers", "runaway_processing_records"),
    ("runaway_background_work", "runaway_processing_records"),
    ("helper_resource_required", "processing_record_resource_required"),
    ("background_work_resource_required", "processing_record_resource_required"),
    ("matching_helper_kind", "matching_work_kind"),
    ("suggested_helper_kind", "suggested_work_kind"),
    ("helper_prompt_fact", "work_prompt_fact"),
    ("helper_output_path", "work_output_path"),
    ("helper_output_fact", "work_output_fact"),
    ("helper_owned_artifact_fact", "generated_artifact_fact"),
    ("post_helper_usage_hint", "post_work_usage_hint"),
    ("helper_route", "work_route"),
    ("helper_runaway_requires_intervention", "processing_record_runaway_requires_intervention"),
    ("background_work_runaway_requires_intervention", "processing_record_runaway_requires_intervention"),
    ("helper_still_running_prompt_dropped", "processing_record_still_running_prompt_dropped"),
    ("background_work_still_running_prompt_dropped", "processing_record_still_running_prompt_dropped"),
    ("helper_producer_self_verified", "output_self_verified"),
    ("producer_self_verified", "output_self_verified"),
    ("producer_boundary_fact", "output_boundary_fact"),
    ("helper_result_summary", "processing_result_summary"),
    ("no_successful_helper", "no_successful_processing_record"),
    ("no_successful_background_work", "no_successful_processing_record"),
    ("clean_helper_batch", "clean_processing_record_batch"),
    ("clean_background_work_batch", "clean_processing_record_batch"),
)
_MODEL_CONTEXT_META_TEXT_KEYS = {
    "fact",
    "facts",
    "hint",
    "hints",
    "warning",
    "warnings",
    "details",
    "note",
    "notes",
    "instruction",
    "instructions",
    "reason",
    "summary_policy",
    "policy",
    "next_action_instruction",
    "error_summary",
    "escalation_advice",
    "attention_fact",
    "error",
    "blocked_reason",
    "options",
    "available_shapes",
    "available_recovery_shapes",
    "available_followups",
    "事实",
}


_MODEL_CONTEXT_LITERAL_VALUE_KEYS = {
    "tool_name",
}


def _first_present(mapping: dict, *keys: str, default=None):
    for key in keys:
        if key in mapping:
            value = mapping.get(key)
            if value is not None:
                return value
    return default


def _sanitize_delegate_model_text(value: str) -> str:
    text = str(value)
    for old, new in _DELEGATE_MODEL_TEXT_REPLACEMENTS:
        text = text.replace(old, new)
    text = re.sub(r"\bdelegation\b", "processing step", text, flags=re.IGNORECASE)
    text = re.sub(r"\bdelegated\b", "routed", text, flags=re.IGNORECASE)
    text = re.sub(r"\bdelegating\b", "routing", text, flags=re.IGNORECASE)
    text = re.sub(r"\bdelegate\b", "route", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:helper|producer)[-_ ]owned\b", "generated", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:helper|producer)\s+reports\b", "available evidence", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:helper|producer)\s+report\b", "available evidence", text, flags=re.IGNORECASE)
    text = re.sub(r"\bproducer\s+evidence\b", "evidence", text, flags=re.IGNORECASE)
    text = re.sub(r"\bbackground_work\b", "processing_records", text, flags=re.IGNORECASE)
    text = re.sub(r"\bbackground\s+(?:tasks?|work|producers?|branches?)\b", "processing records", text, flags=re.IGNORECASE)
    text = re.sub(r"\bproducer\s+helpers\b", "processing records", text, flags=re.IGNORECASE)
    text = re.sub(r"\bproducer\s+helper\b", "processing record", text, flags=re.IGNORECASE)
    text = re.sub(r"\bproducers\b", "processing records", text, flags=re.IGNORECASE)
    text = re.sub(r"\bproducer\b", "processing record", text, flags=re.IGNORECASE)
    text = re.sub(r"\bhelpers\b", "processing records", text, flags=re.IGNORECASE)
    text = re.sub(r"\bhelper\b", "processing record", text, flags=re.IGNORECASE)
    return text


def _sanitize_delegate_model_value(value, *, key: str = ""):
    if isinstance(value, dict):
        sanitized: dict = {}
        for key, item in value.items():
            if not isinstance(key, str):
                sanitized[key] = _sanitize_delegate_model_value(item, key="")
                continue
            if key in _DELEGATE_MODEL_DROP_KEYS:
                continue
            new_key = _DELEGATE_MODEL_KEY_RENAMES.get(key, key)
            sanitized[new_key] = _sanitize_delegate_model_value(item, key=new_key)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_delegate_model_value(item, key=key) for item in value]
    if isinstance(value, str):
        if key in _MODEL_CONTEXT_LITERAL_VALUE_KEYS:
            return value
        return _sanitize_delegate_model_text(value)
    return value


def _sanitize_delegate_result_for_model_context(result: str) -> str:
    """Remove internal helper vocabulary before delegate results enter LLM context."""
    if not isinstance(result, str) or not result:
        return result
    try:
        parsed = json.loads(result)
    except Exception:
        return _sanitize_delegate_model_text(result)
    if not isinstance(parsed, dict):
        return result
    return stable_prompt_json(_sanitize_delegate_model_value(parsed))


def _neutral_round3_gap_text(value: object) -> str:
    """Keep gap facts model-readable without seeding internal workflow terms."""
    text = str(value or "")
    text = re.sub(r"\bhelpers?\b", "processing step", text, flags=re.IGNORECASE)
    text = re.sub(r"\bhelper[_ -]gap\b", "processing_gap", text, flags=re.IGNORECASE)
    text = re.sub(r"\bbackground\s+(?:tasks?|work|producers?|branches?)\b", "processing step", text, flags=re.IGNORECASE)
    text = re.sub(r"\bproducer-owned\b", "generated", text, flags=re.IGNORECASE)
    text = re.sub(r"\bproducer\s+evidence\b", "evidence", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:_delegate_|_helpers_shared/|\.helper_[\w.-]+|\.helper_task_contract\.json)", "internal material", text)
    return text


def _round3_gap_missing_items(values: object) -> list[str]:
    """Return only user-relevant missing paths for Round3 gap wording."""
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for item in values:
        text = str(item or "").replace("\\", "/").strip()
        lowered = text.lower()
        if not text:
            continue
        if lowered.startswith(("_helpers_shared/", "_shared/", ".helper_")):
            continue
        if ".helper_" in lowered or "/.helper_" in lowered:
            continue
        out.append(text)
        if len(out) >= 3:
            break
    return out


def _sanitize_tool_result_internal_terms_for_model_context(result: str) -> str:
    """Sanitize internal routing metadata without changing user/file evidence fields."""
    if not isinstance(result, str) or not result:
        return result
    try:
        parsed = json.loads(result)
    except Exception:
        return result
    if not isinstance(parsed, dict):
        return result

    def visit(value, *, key: str = ""):
        if isinstance(value, dict):
            sanitized: dict = {}
            for raw_key, item in value.items():
                if not isinstance(raw_key, str):
                    sanitized[raw_key] = visit(item, key="")
                    continue
                if raw_key in _DELEGATE_MODEL_DROP_KEYS:
                    continue
                new_key = _DELEGATE_MODEL_KEY_RENAMES.get(raw_key, raw_key)
                sanitized[new_key] = visit(item, key=new_key)
            return sanitized
        if isinstance(value, list):
            return [visit(item, key=key) for item in value]
        if isinstance(value, str):
            key_l = key.lower()
            if (
                key_l in _MODEL_CONTEXT_META_TEXT_KEYS
                or key_l.endswith("_fact")
                or key_l.endswith("_facts")
                or key_l.endswith("_hint")
                or key_l.endswith("_hints")
                or key_l.endswith("_policy")
                or key_l.endswith("_route")
                or key_l.endswith("_option")
                or key_l.endswith("_options")
            ):
                return _sanitize_delegate_model_text(value)
        return value

    return stable_prompt_json(visit(parsed))


def _looks_like_final_plan_self_assessment(text: str) -> bool:
    """Detect a malformed final response that only evaluates a missing JSON plan."""
    low = (text or "").lower()
    if not low:
        return False
    parsed = _parse_response_plan_dict(text)
    if parsed is not None:
        keys = {str(key).lower() for key in parsed.keys()}
        has_plan_fields = "intent" in keys and "key_points" in keys
        # Content-by-reference: a structurally valid plan whose key_points only
        # POINT AT earlier tool-loop output ("already produced above") instead
        # of carrying it. Earlier assistant text never reaches the reply stage,
        # so such a plan loses the content entirely (arena judge trial: the
        # scoring JSON lived in a prior turn; the user got prose about it).
        if has_plan_fields:
            kp = parsed.get("key_points")
            if isinstance(kp, list) and kp:
                joined = " ".join(str(x) for x in kp).lower()
                refers_above = any(
                    marker in joined
                    for marker in (
                        "already produced above", "produced above", "provided above",
                        "output above", "shown above", "json above", "above with all",
                        "earlier in this conversation", "in the previous message",
                    )
                )
                has_substance = any(len(str(x)) >= 400 for x in kp)
                if refers_above and not has_substance:
                    return True
        self_assessment_keys = {
            "final_json_status",
            "contract_verification",
            "acceptance_points",
            "further_tools_needed",
            "contract_complete",
        }
        if not has_plan_fields and (keys & self_assessment_keys):
            return True
        status_text = json.dumps(parsed, ensure_ascii=False, sort_keys=True).lower()
        if not has_plan_fields and (
            "final json" in status_text
            or "contract" in status_text and "satisfied" in status_text
            or "acceptance" in status_text and "satisfied" in status_text
        ):
            return True
    self_assessment = (
        "final json" in low
        or "active task contract" in low
        or "contract-complete" in low
        or "contract complete" in low
        or "no adjustments needed" in low
        or "satisfies" in low and "contract" in low
    )
    checklist = any(marker in text for marker in ("M1-M4", "O1-O3", "R1-R3"))
    return self_assessment and checklist


def _response_plan_needs_audit_evidence_review(content: str) -> bool:
    """Return true when a final ResponsePlan should get one evidence-boundary review.

    This does not judge the findings. It only recognizes audit/review style
    final plans where the model is about to make multiple optimization/risk
    claims, so the same context can re-check evidence strength before Round3.
    """
    try:
        parsed = json.loads((content or "").strip())
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    key_points = parsed.get("key_points")
    if not isinstance(key_points, list) or not key_points:
        return False
    text = "\n".join([
        str(parsed.get("intent") or ""),
        "\n".join(str(x) for x in key_points[:24]),
        str(parsed.get("internal_note") or ""),
    ]).lower()
    task_markers = (
        "audit", "review", "optimization", "optimisation", "risk", "root-cause",
        "root cause", "correctness", "cache", "context",
        "审计", "评审", "复核", "优化", "风险", "根因", "正确性", "缓存", "上下文",
    )
    claim_markers = (
        "o1", "o2", "o3", "r1", "r2", "r3",
        "优化点", "风险", "warning", "警告", "finding", "发现",
    )
    return any(marker in text for marker in task_markers) and any(
        marker in text for marker in claim_markers
    )


def _parse_response_plan_dict(content: str) -> dict | None:
    try:
        parsed = json.loads((content or "").strip())
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _content_is_complete_response_plan(content: str) -> bool:
    """Return true when the final text already is a usable ResponsePlan JSON.

    Used to skip pre-final guidance interruptions: re-prompting a model that
    already emitted a complete plan reliably degrades the second answer into
    self-assessment prose (then a cleanup LLM call). Only intent + key_points
    presence is checked — schema details are validated downstream.
    """
    parsed = _parse_response_plan_dict(content)
    if parsed is None:
        return False
    keys = {str(k).lower() for k in parsed.keys()}
    return "intent" in keys and "key_points" in keys


_EXACT_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:"
    r"[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*"
    r"|[A-Za-z0-9_.-]+[\\/][A-Za-z0-9_.\\/-]+"
    r"|[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]*"
    r"|[A-Za-z0-9_.-]+\.(?:py|js|ts|tsx|jsx|csv|json|txt|md|docx|xlsx|pptx|pdf|html|css|sql|db|png|jpg|jpeg|webp|gif|mp3|wav|log|c|cpp|h|hpp|java|go|rs|toml|yaml|yml|xml)"
    r"|[A-Za-z][A-Za-z0-9_]*->[A-Za-z][A-Za-z0-9_]*"
    r")"
    r"(?![A-Za-z0-9_])"
)
_NUMBER_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?%?(?![A-Za-z0-9_])")


def _exact_tokens_for_cleanup_fidelity(text: str) -> set[str]:
    """Extract exact identifiers that a prose-to-JSON cleanup must preserve.

    This is deliberately factual and format-oriented: source fields, output
    aliases, paths, filenames, command-ish identifiers, and salient numbers.
    It does not decide which facts matter to the task.

    只抽取精确标识符用于格式转换保真校验，不替模型判断任务事实。
    """
    value = str(text or "")
    tokens: set[str] = set()
    for match in _EXACT_TOKEN_RE.finditer(value):
        token = match.group(0).strip("`'\".,;:()[]{}<>")
        if "." in token and "/" not in token and "\\" not in token:
            head = token.split(".", 1)[0]
            if len(head) <= 2:
                continue
        if len(token) >= 3:
            tokens.add(token)
    for match in _NUMBER_TOKEN_RE.finditer(value):
        token = match.group(0)
        if len(token) >= 2:
            tokens.add(token)
    return tokens


def _cleanup_preserves_exact_tokens(original: str, cleanup: str) -> tuple[bool, str]:
    """Validate that LLM JSON cleanup did not rename or drop exact facts.

    The cleanup pass is only a format conversion. If it changes many exact
    identifiers, keep the original text so downstream salvage can preserve
    evidence instead of accepting a plausible but wrong ResponsePlan.
    """
    original_tokens = _exact_tokens_for_cleanup_fidelity(original)
    if len(original_tokens) < 3:
        return True, "few_exact_tokens"
    cleanup_tokens = _exact_tokens_for_cleanup_fidelity(cleanup)
    missing = sorted(original_tokens - cleanup_tokens)
    preserved = len(original_tokens) - len(missing)
    ratio = preserved / max(len(original_tokens), 1)
    critical_missing = [
        token for token in missing
        if (
            "_" in token
            or "." in token
            or "/" in token
            or "\\" in token
            or "->" in token
        )
        and len(token) >= 5
    ]
    if critical_missing and ratio < 0.85:
        return False, (
            f"missing exact identifiers after cleanup: {critical_missing[:8]} "
            f"(preserved={preserved}/{len(original_tokens)}, ratio={ratio:.2f})"
        )
    if ratio < 0.70:
        return False, (
            f"cleanup dropped too many exact tokens: {missing[:10]} "
            f"(preserved={preserved}/{len(original_tokens)}, ratio={ratio:.2f})"
        )
    return True, f"preserved={preserved}/{len(original_tokens)} ratio={ratio:.2f}"


def _audit_review_content_regressed(previous: str, current: str) -> bool:
    """Detect an evidence-review output that deleted the actual audit answer.

    The review pass may downgrade or remove weak claims. It should not collapse
    a substantive audit plan into a tiny coverage checklist unless it states the
    evidence reason for that downgrade.
    """
    prev = _parse_response_plan_dict(previous)
    cur = _parse_response_plan_dict(current)
    if not prev or not cur:
        return False
    prev_points = [str(x).strip() for x in (prev.get("key_points") or []) if str(x).strip()]
    cur_points = [str(x).strip() for x in (cur.get("key_points") or []) if str(x).strip()]
    if len(prev_points) < 4:
        return False
    prev_text = "\n".join([
        str(prev.get("intent") or ""),
        "\n".join(prev_points),
        str(prev.get("internal_note") or ""),
    ]).lower()
    cur_text = "\n".join([
        str(cur.get("intent") or ""),
        "\n".join(cur_points),
        str(cur.get("internal_note") or ""),
    ]).lower()
    audit_markers = (
        "audit", "review", "optimization", "optimisation", "risk", "root-cause",
        "root cause", "correctness", "cache", "context",
        "审计", "评审", "复核", "优化", "风险", "根因", "正确性", "缓存", "上下文",
    )
    if not any(marker in prev_text for marker in audit_markers):
        return False
    downgrade_markers = (
        "unsupported", "insufficient evidence", "not enough evidence", "hypothesis",
        "low-confidence", "weak lead", "missing direct evidence",
        "证据不足", "缺少证据", "假设", "低置信", "弱线索", "未验证",
    )
    if any(marker in cur_text for marker in downgrade_markers):
        return False
    claim_markers = (
        "m1", "m2", "m3", "m4", "o1", "o2", "o3", "r1", "r2", "r3",
        "优化点", "风险", "机制",
    )
    prev_claim_markers = sum(1 for marker in claim_markers if marker in prev_text)
    cur_claim_markers = sum(1 for marker in claim_markers if marker in cur_text)
    prev_chars = sum(len(point) for point in prev_points)
    cur_chars = sum(len(point) for point in cur_points)
    checklist_markers = ("covered", "coverage", "files read", "存在于", "已读取", "覆盖", "检查清单")
    cur_looks_like_checklist = any(marker in cur_text for marker in checklist_markers)
    return (
        prev_claim_markers >= 4
        and cur_claim_markers <= 1
        and len(cur_points) <= max(2, len(prev_points) // 3)
        and cur_chars < max(240, int(prev_chars * 0.35))
        and cur_looks_like_checklist
    )


def _tool_result_explicit_success(result: str) -> bool:
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict):
        if data.get("ok") is True or data.get("success") is True:
            return True
        status = str(data.get("status") or "").strip().lower()
        if status in {"ok", "done", "success", "completed", "pass", "passed"}:
            return True
    try:
        ok, _ = _tool_result_signal(result)
        return bool(ok)
    except Exception:
        return False


def _tool_result_schema_retry_reason(tool_name: str, result: str) -> str | None:
    """Return a compact reason when a tool error likely needs its full schema.

    This only observes facts from the tool result. It does not decide the next
    action for the model; it only makes the full schema temporarily available
    on the next turn so the model can retry with better local facts.
    """
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("ok") is True or data.get("success") is True:
        return None
    error_code = str(data.get("error") or data.get("code") or "").strip().lower()
    if error_code in {
        "tool_not_available_in_this_context",
        "toolchain_already_continued_this_round",
    }:
        return None
    text_parts: list[str] = []
    for key in (
        "error", "code", "message", "hint", "fix_hint", "delegate_hint",
        "args_parse_error", "validation_error", "reason",
    ):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            text_parts.append(value.strip())
    haystack = " ".join(text_parts).lower()
    schema_terms = (
        "schema", "argument", "arguments", "parameter", "parameters",
        "required", "missing", "invalid action", "unknown action",
        "validation", "validate", "json", "enum", "unexpected field",
        "tool_call_args_json_broken", "invalid_action", "unknown_action",
    )
    if error_code == "tool_call_args_json_broken" or any(term in haystack for term in schema_terms):
        reason = error_code or haystack[:160] or f"{tool_name} argument error"
        return reason[:300]
    return None


def _artifact_type_from_path(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in {"py", "js", "ts", "tsx", "jsx", "java", "go", "rs", "c", "cpp", "h", "hpp", "cs", "rb", "php"}:
        return "code"
    if ext in {"json", "yaml", "yml", "toml", "ini", "cfg", "csv", "tsv", "xml"}:
        return "data"
    if ext in {"md", "txt", "docx", "pdf"}:
        return "report"
    if ext in {"png", "jpg", "jpeg", "webp", "svg"}:
        return "image"
    return "file"


def _main_env_run_convergence_family(tool_name: str, args: dict) -> str | None:
    name = (tool_name or "").strip()
    if name not in {"env_run", "workspace"} or not isinstance(args, dict):
        return None
    if name == "workspace" and str(args.get("action") or "").strip().lower() != "run":
        return None
    command = str(args.get("command") or args.get("cmd") or args.get("python_code") or "").strip()
    if not command:
        return None
    normalized = command.replace("\\", "/")
    db_match = re.search(r"(?i)(?:^|[\s\"'])([^\"'\s]+?\.(?:sqlite3?|db))(?:$|[\s\"'])", normalized)
    if db_match:
        return f"db:{db_match.group(1).lower()}"
    verifier_match = re.search(
        r"(?i)(?:^|[\/\s\"'])([^\"'\s/]*(?:verify|check|validate|grade|test|run[_-]?tests?)[\w.-]*\.(?:py|js|mjs|cjs|sh|ps1|bat|cmd))(?:$|[\s\"'])",
        normalized,
    )
    if verifier_match:
        return f"verifier:{verifier_match.group(1).lower()}"
    return None


def _main_verifier_command_text(tool_name: str, args: dict) -> str:
    """Return command-like text used to detect verifier/check execution."""
    name = (tool_name or "").strip()
    if not isinstance(args, dict):
        return ""
    if name in {"env_run", "bash", "bash_execute"}:
        return str(args.get("command") or args.get("cmd") or args.get("python_code") or "")
    if name == "workspace" and str(args.get("action") or "").strip().lower() == "run":
        return str(args.get("command") or args.get("cmd") or args.get("python_code") or "")
    return ""


def _verifier_visible_artifact_paths_from_listing(tool_name: str, parsed_result: dict) -> list[str]:
    if tool_name not in {"env_list_tree", "env_inventory"} or not isinstance(parsed_result, dict):
        return []
    candidates: list[str] = []
    if tool_name == "env_list_tree":
        for item in parsed_result.get("items") or []:
            if not isinstance(item, dict) or item.get("type") != "file":
                continue
            candidates.append(str(item.get("path") or "").replace("\\", "/").strip())
    else:
        for item in parsed_result.get("resources") or []:
            if not isinstance(item, dict):
                continue
            candidates.append(str(item.get("project_path") or "").replace("\\", "/").strip())
    artifact_exts = (".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".md", ".markdown", ".txt", ".html", ".htm")
    script_exts = (".py", ".js", ".mjs", ".cjs", ".sh", ".ps1", ".bat", ".cmd")
    paths: list[str] = []
    for path in candidates:
        lowered = path.lower()
        if not lowered or lowered.endswith(script_exts):
            continue
        if lowered.endswith(artifact_exts):
            paths.append(path)
    return paths[:20]


def _main_source_or_test_paths_from_project_discovery(tool_name: str, parsed_result: dict) -> list[str]:
    if tool_name not in {"env_list_tree", "env_inventory", "env_search"} or not isinstance(parsed_result, dict):
        return []
    if parsed_result.get("ok") is False:
        return []

    raw_paths: list[str] = []

    def add_path(value: object) -> None:
        path = str(value or "").replace("\\", "/").strip().strip("`'\"")
        if path:
            raw_paths.append(path)

    handoff = parsed_result.get("helper_handoff_fact") or parsed_result.get("background_handoff_fact")
    if isinstance(handoff, dict):
        for key in ("project_paths", "source_paths", "test_paths", "input_files", "acceptance_script_paths"):
            values = handoff.get(key)
            if isinstance(values, list):
                for value in values:
                    add_path(value)

    if tool_name == "env_list_tree":
        for item in parsed_result.get("items") or []:
            if isinstance(item, dict) and item.get("type") == "file":
                add_path(item.get("path"))
    elif tool_name == "env_inventory":
        for item in parsed_result.get("resources") or []:
            if isinstance(item, dict):
                add_path(item.get("project_path") or item.get("path"))
    elif tool_name == "env_search":
        for key in ("matches", "results", "items"):
            for item in parsed_result.get(key) or []:
                if isinstance(item, dict):
                    add_path(item.get("path") or item.get("project_path"))

    code_exts = (".py", ".js", ".ts", ".tsx", ".jsx", ".c", ".cc", ".cpp", ".h", ".hpp", ".rs", ".go", ".java")
    out: list[str] = []
    seen: set[str] = set()
    for path in raw_paths:
        lowered = path.lower()
        if lowered.startswith("."):
            continue
        if not lowered.endswith(code_exts):
            continue
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out[:16]


def _main_source_path_handoff_fact(paths: list[str]) -> str:
    shown = ", ".join(paths[:10])
    more = f" (+{len(paths) - 10} more)" if len(paths) > 10 else ""
    return (
        "[SYSTEM_HINT/main_source_path_handoff_fact]\n"
        f"Project discovery has already exposed likely source/test paths: {shown}{more}. "
        "For coding/debugging, these path/search facts are enough to give a focused code producer `input_files`, "
        "`expected_outputs`, and acceptance checks; that producer can read source bodies, diagnose, edit, and test from them. "
        "If you delegate from these facts, keep the producer prompt compact and do not paste complete source-code blocks "
        "that are already covered by `input_files`; include only specific observed facts that affect routing or acceptance. "
        "A batch of main-thread env_read calls over all known source/test paths before delegation repeats "
        "producer-owned reading and increases coordinator context. This is not a ban: one narrow env_read is useful when a "
        "specific missing routing or acceptance fact remains unresolved.\n\n"
        "事实：源码/测试路径和搜索事实已足够形成紧凑 code producer 请求；delegate prompt 不粘贴 input_files 已覆盖的完整源码；若只缺一个具体路由/验收事实，可窄读。"
    )


def _main_helper_handoff_paths_from_project_discovery(tool_name: str, parsed_result: dict) -> list[str]:
    """Extract factual helper input paths from project discovery results."""
    if tool_name not in {"env_list_tree", "env_inventory", "env_search"} or not isinstance(parsed_result, dict):
        return []
    if parsed_result.get("ok") is False:
        return []
    raw_paths: list[str] = []

    def add_path(value: object) -> None:
        path = str(value or "").replace("\\", "/").strip().strip("`'\"")
        if path:
            raw_paths.append(path)

    handoff = parsed_result.get("helper_handoff_fact") or parsed_result.get("background_handoff_fact")
    if isinstance(handoff, dict):
        for key in (
            "project_paths", "source_paths", "test_paths", "data_paths",
            "input_files", "acceptance_script_paths",
        ):
            values = handoff.get(key)
            if isinstance(values, list):
                for value in values:
                    add_path(value)
    if tool_name == "env_list_tree":
        for item in parsed_result.get("items") or []:
            if isinstance(item, dict) and item.get("type") == "file":
                add_path(item.get("path"))
    elif tool_name == "env_inventory":
        for item in parsed_result.get("resources") or []:
            if isinstance(item, dict):
                add_path(item.get("project_path") or item.get("path"))
    elif tool_name == "env_search":
        for key in ("matches", "results", "items"):
            for item in parsed_result.get(key) or []:
                if isinstance(item, dict):
                    add_path(item.get("path") or item.get("project_path"))

    useful_exts = (
        ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".c", ".cc", ".cpp", ".h", ".hpp",
        ".rs", ".go", ".java", ".sql", ".db", ".sqlite", ".sqlite3", ".csv", ".tsv", ".json",
        ".jsonl", ".yaml", ".yml", ".toml", ".md", ".txt",
    )
    command_shim_names = ("python", "python3", "node", "npm", "pnpm", "yarn", "pytest")
    out: list[str] = []
    seen: set[str] = set()
    for path in raw_paths:
        lowered = path.lower()
        if not lowered or lowered.startswith("."):
            continue
        name = lowered.rsplit("/", 1)[-1]
        stem = name.rsplit(".", 1)[0]
        if stem in command_shim_names:
            continue
        if not lowered.endswith(useful_exts):
            continue
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out[:20]


def _main_text_material_paths_from_project_discovery(tool_name: str, parsed_result: dict) -> list[str]:
    """Extract compact source-material paths suitable for one read/edit helper."""
    if tool_name not in {"env_list_tree", "env_inventory"} or not isinstance(parsed_result, dict):
        return []
    if parsed_result.get("ok") is False:
        return []
    fact = parsed_result.get("text_material_handoff_fact")
    if not isinstance(fact, dict) or fact.get("kind") != "compact_text_material_set":
        return []
    raw_paths = fact.get("material_paths")
    if not isinstance(raw_paths, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in raw_paths:
        path = str(value or "").replace("\\", "/").strip().strip("`'\"")
        if not path or path.startswith(".") or path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out[:20]


def _main_text_material_handoff_fact(paths: list[str]) -> str:
    shown = ", ".join(paths[:10])
    more = f" (+{len(paths) - 10} more)" if len(paths) > 10 else ""
    return (
        "[SYSTEM_HINT/main_text_material_handoff_fact]\n"
        f"Project discovery reports a compact text-material set: {shown}{more}. "
        "These are current project facts, not a forced decision. For triage, classification, summarization, "
        "drafting, report writing, or small text/data artifact production, one focused reading/implementation step can receive "
        "these paths as `input_files` plus the user request and acceptance checks. That step can read the material bodies "
        "and produce the requested artifact or concise evidence in its own context. Main-thread full-body reads still add "
        "value for one missing routing fact, a user-facing quote, or a main-owned evidence gap, but reading the whole set "
        "in the coordinator usually expands context that a focused step can own.\n\n"
        "事实：项目发现结果显示一组紧凑文本材料；可把路径和验收要求交给一个聚焦读取/实现步骤处理并产出。主线程全文读取整组材料通常增加协调上下文，除非缺少具体路由、引用或主进程自有证据缺口。"
    )


def _main_direct_project_work_label(tool_name: str, args: dict) -> str | None:
    """Label main-thread direct work that can duplicate helper-owned work after handoff facts exist."""
    name = (tool_name or "").strip()
    if name in {"env_read", "env_run", "env_fetch", "env_search"}:
        return name
    if name in {"inspect_file", "read_file"}:
        path = str((args or {}).get("path") or "").replace("\\", "/")
        if path.startswith("_env/") or path.endswith(".db") or path.endswith(".csv"):
            return name
    if name == "workspace":
        action = str((args or {}).get("action") or "").strip().lower()
        if action in {"locate", "run", "write"}:
            return f"workspace.{action}"
    if name == "bash":
        return "bash"
    if name == "python":
        return "python"
    return None


def _main_helper_handoff_overwork_checkpoint(
    *,
    paths: list[str],
    direct_count: int,
    recent_labels: list[str],
) -> str:
    shown = ", ".join(paths[:10])
    more = f" (+{len(paths) - 10} more)" if len(paths) > 10 else ""
    recent = ", ".join(recent_labels[-8:]) or "none"
    return (
        "[SYSTEM_HINT/main_processing_handoff_overwork_fact]\n"
        f"Project discovery already exposed focused-step input paths: {shown}{more}. "
        f"Since those handoff facts appeared, the main process has performed about {direct_count} direct "
        f"read/probe/script-style tool result(s) before useful processing results; recent direct work labels: {recent}. "
        "This is a factual coordinator-context signal, not a forced decision. A focused step can receive the known "
        "input_files plus acceptance checks and perform data probes, source reads, edits, and tests in its own context. "
        "Main-thread direct work still adds value for one missing routing fact, diff/apply preparation, or a narrow "
        "main-owned evidence gap. Compare the current task contract, observed facts, and missing evidence before deciding whether to "
        "delegate, continue one narrow main check, or finalize.\n\n"
        "事实：已有聚焦处理步骤可用路径后，主进程又进行了多次直接读取/探测/脚本式操作；这只是上下文负担信号，由模型结合当前契约决定是否路由后续处理或只做一个窄检查。"
    )


_MAIN_HELPER_HANDOFF_OVERWORK_DIRECT_COUNT = 5


def _record_main_tool_facts_in_agent_state(tool_name: str, args: dict, result: str, helper_kind: str | None) -> None:
    """Mirror factual main-thread tool outcomes into agent_state."""
    if helper_kind is not None:
        return
    try:
        from app.core import debug as _debug

        trace_id = _debug.current_trace_id()
    except Exception:
        trace_id = None
    if not trace_id or not isinstance(result, str):
        return
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(parsed, dict) or parsed.get("ok") is False:
        return

    try:
        from app.core import agent_state

        action = str(parsed.get("action") or "").strip()
        path = str(parsed.get("path") or "").strip()
        if tool_name == "env_read" and path:
            text_facts = parsed.get("content_facts") if isinstance(parsed.get("content_facts"), dict) else {}
            if not text_facts:
                text_facts = {
                    "total_lines": parsed.get("total_lines"),
                    "start_line": parsed.get("start_line"),
                    "end_line": parsed.get("end_line"),
                    "truncated": parsed.get("truncated"),
                }
            agent_state.add_evidence(
                trace_id=trace_id,
                source="env_read",
                status=agent_state.EVIDENCE_VERIFIED,
                summary=(
                    f"Project file read: {path}"
                    + (f" lines {parsed.get('start_line')}-{parsed.get('end_line')}" if parsed.get("start_line") else "")
                    + (" (truncated)" if parsed.get("truncated") else "")
                ),
                kind="project_file_read",
                data={
                    "path": path,
                    "source_zone": parsed.get("source_zone"),
                    "sha256": parsed.get("sha256"),
                    "text_facts": text_facts,
                    "content_full_saved_path": parsed.get("content_full_saved_path"),
                },
            )
        exact_reference = parsed.get("exact_text_reference")
        if tool_name == "env_read" and isinstance(exact_reference, dict):
            reference_path = str(exact_reference.get("path") or path or "").strip()
            if reference_path:
                text_facts = exact_reference.get("text_facts") if isinstance(exact_reference.get("text_facts"), dict) else {}
                line_count = text_facts.get("line_count")
                newline_counts = text_facts.get("newline_counts")
                fact = (
                    f"Exact reference file fact: {reference_path} was read as expected/golden/snapshot/reference text. "
                    "If an active verifier compares output to this file, preserve line order, delimiters, visible text, "
                    "and trailing blank lines according to the verifier's text-vs-byte comparison semantics."
                )
                evidence = agent_state.add_evidence(
                    trace_id=trace_id,
                    source="env_read",
                    status=agent_state.EVIDENCE_VERIFIED,
                    summary=f"Exact reference text read: {reference_path}",
                    kind="exact_text_reference",
                    data={
                        "path": reference_path,
                        "text_facts": text_facts,
                    },
                )
                try:
                    contracts = agent_state.structured_status(trace_id).get("contracts") or []
                    main_contract = next(
                        (
                            contract for contract in contracts
                            if isinstance(contract, dict) and str(contract.get("task_id") or "") == "main"
                        ),
                        {},
                    )
                    goal = str(main_contract.get("goal") or "").strip() or "current active task"
                    evidence_note = f"Exact reference evidence from {reference_path}"
                    if line_count is not None:
                        evidence_note += f"; line_count={line_count}"
                    if newline_counts:
                        evidence_note += f"; newline_counts={newline_counts}"
                    agent_state.upsert_task_contract(
                        trace_id=trace_id,
                        task_id="main",
                        goal=goal,
                        acceptance=[fact],
                        evidence_required=[evidence_note],
                        current_stage="reference_evidence_seen",
                    )
                except Exception:
                    log.exception(
                        "agent_state exact reference contract mirroring failed for %s (non-fatal)",
                        reference_path,
                    )
        if tool_name in {"env_apply_replace", "env_apply_create"} and path:
            evidence = agent_state.add_evidence(
                trace_id=trace_id,
                source=tool_name,
                status=agent_state.EVIDENCE_VERIFIED,
                summary=f"{tool_name} applied project file: {path}",
                kind="project_apply",
                data={
                    "action": action or tool_name,
                    "path": path,
                    "new_sha256": parsed.get("new_sha256"),
                    "backup_workspace_path": parsed.get("backup_workspace_path"),
                    "backup_project_path": parsed.get("backup_project_path"),
                },
            )
            agent_state.register_artifact(
                trace_id=trace_id,
                path=path,
                artifact_type=_artifact_type_from_path(path),
                created_by=tool_name,
                status=agent_state.ARTIFACT_READY,
                verified_by=tool_name,
                evidence_ids=[evidence["evidence_id"]],
                metadata={"action": action or tool_name},
            )

        text_parts = [
            str(parsed.get("test_summary") or ""),
            str(parsed.get("stdout") or ""),
            str(parsed.get("output") or ""),
        ]
        test_text = "\n".join(part for part in text_parts if part)
        if tool_name in {"env_run", "bash", "python", "workspace"} and test_text:
            passed_match = re.search(r"\b(\d+)\s+passed\b", test_text, re.IGNORECASE)
            failed_match = re.search(r"\b([1-9]\d*)\s+failed\b", test_text, re.IGNORECASE)
            error_match = re.search(r"\b([1-9]\d*)\s+errors?\b", test_text, re.IGNORECASE)
            if passed_match and not failed_match and not error_match:
                command = str(parsed.get("command") or (args or {}).get("command") or "").strip()
                agent_state.add_evidence(
                    trace_id=trace_id,
                    source=tool_name,
                    status=agent_state.EVIDENCE_VERIFIED,
                    summary=f"Test command passed: {passed_match.group(1)} passed"
                    + (f" ({command[:160]})" if command else ""),
                    kind="test",
                    data={
                        "passed": int(passed_match.group(1)),
                        "command": command,
                        "test_summary": parsed.get("test_summary"),
                    },
                )
    except Exception:
        try:
            log.exception("agent_state factual tool mirroring failed for %s (non-fatal)", tool_name)
        except Exception:
            pass


def _artifact_acceptance_key(tool_name: str, args: dict, result: str) -> str | None:
    """Return a stable artifact key for repeated artifact-acceptance checks.

    This is only used to inject model-visible convergence guidance after repeated
    checks. It does not decide success or change tool results.
    """
    name = (tool_name or "").strip()
    path = ""
    if isinstance(args, dict):
        for key in ("path", "file", "filename", "target", "source"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                path = value.strip().replace("\\", "/")
                break
    if not path:
        return None
    lowered = path.lower()
    artifact_exts = (".docx", ".pptx", ".xlsx", ".pdf", ".png", ".jpg", ".jpeg", ".csv", ".json", ".md", ".txt")
    if not lowered.endswith(artifact_exts):
        return None
    action = ""
    if isinstance(args, dict):
        action = str(args.get("action") or args.get("op") or "").strip().lower()
    checking_tools = {"office", "inspect_file", "read_file", "workspace", "python"}
    if name not in checking_tools:
        return None
    if name == "office" and action and action not in {
        "read", "inspect", "verify_numbers", "verify_rigor", "verify_integrity", "extract_images", "ocr_images",
    }:
        return None
    if name == "workspace":
        command = str(args.get("command") or args.get("cmd") or "").lower() if isinstance(args, dict) else ""
        if not any(token in command for token in ("read", "inspect", "verify", "python", "unzip", "zipfile", "document.xml")):
            return None
    if not _tool_result_explicit_success(result):
        return None
    return f"{name}:{action}:{lowered}" if action else f"{name}:{lowered}"


def _completed_todo_count_from_result(tool_name: str, result: str) -> int | None:
    """Return completed count when a todo_write result shows all items complete."""
    if tool_name != "todo_write":
        return None
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("ok") is not True:
        return None
    counts = data.get("counts")
    if not isinstance(counts, dict):
        return None
    try:
        total = int(counts.get("total") or 0)
        completed = int(counts.get("completed") or 0)
        pending = int(counts.get("pending") or 0)
        in_progress = int(counts.get("in_progress") or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0 or completed < total or pending or in_progress:
        return None
    return completed


def _office_write_artifact_key(tool_name: str, args: dict, result: str) -> str | None:
    """Return the Office artifact path after a successful write-like action.

    This is used only for model-visible convergence facts in helper loops. It
    does not classify the artifact as complete and does not alter tool results.
    """
    if (tool_name or "").strip() != "office" or not isinstance(args, dict):
        return None
    action = str(args.get("action") or "").strip().lower()
    if action not in {
        "write", "append", "replace_section", "replace_block", "insert_block",
        "delete_block", "insert_image", "edit", "save", "create",
    }:
        return None
    path = str(args.get("path") or args.get("file") or args.get("filename") or "").strip()
    if not path:
        return None
    lowered = path.replace("\\", "/").lower()
    if not lowered.endswith((".docx", ".pptx", ".xlsx")):
        return None
    if not _tool_result_explicit_success(result):
        return None
    return lowered


def _office_read_artifact_key(tool_name: str, args: dict, result: str) -> str | None:
    """Return Office read artifact key from call args plus explicit success.

    Large office reads may be structurally summarized before convergence
    checks, so the result action can become the wrapper tool name. The call
    args still contain the intended action and path.
    """
    if (tool_name or "").strip() != "office" or not isinstance(args, dict):
        return None
    action = str(args.get("action") or "").strip().lower()
    if action not in {"read", "inspect"}:
        return None
    path = str(args.get("path") or args.get("file") or args.get("filename") or "").strip()
    if not path:
        return None
    lowered = path.replace("\\", "/").lower()
    if not lowered.endswith((".docx", ".pptx", ".xlsx", ".xlsm")):
        return None
    if not _tool_result_explicit_success(result):
        return None
    return f"office:{action}:{lowered}"


def _compact_read_outline(text: str, *, max_items: int = 18) -> list[str]:
    """Extract stable, low-token anchors from line-numbered text output."""
    if not text:
        return []
    anchors: list[str] = []
    seen: set[str] = set()
    patterns = (
        r"^\s*(?P<line>\d{1,6})\s*:\s*(?P<body>(?:async\s+def|def|class)\s+[^:\n]{1,140})",
        r"^\s*(?P<line>\d{1,6})\s*:\s*(?P<body>#{1,6}\s+[^#\n]{1,140})",
        r"^\s*(?P<line>\d{1,6})\s*:\s*(?P<body>[A-Z][A-Za-z0-9 _/-]{2,80}:)\s*$",
    )
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        matched = None
        for pattern in patterns:
            m = re.match(pattern, raw_line)
            if m:
                matched = f"L{m.group('line')}: {m.group('body').strip()}"
                break
        if matched is None and len(anchors) < 4:
            m = re.match(r"^\s*(?P<line>\d{1,6})\s*:\s*(?P<body>[^{}\[\]<>]{8,120})", raw_line)
            if m:
                matched = f"L{m.group('line')}: {m.group('body').strip()}"
        if not matched or matched in seen:
            continue
        seen.add(matched)
        anchors.append(matched)
        if len(anchors) >= max_items:
            break
    return anchors


_DYNAMIC_GUIDANCE_MAX_PAYLOAD_CHARS = 12_000


def _dynamic_guidance_tag(payload: str) -> str:
    """Return a stable tag for de-duping one-shot runtime guidance."""
    text = str(payload or "")
    match = re.search(r"\[(SYSTEM_(?:HINT|HELPER_SUMMARY|RECALL)/[^\]\n]+)\]", text)
    if match:
        return match.group(1)
    if text.lstrip().startswith("## Tool Schema Retry Facts"):
        return "Tool Schema Retry Facts"
    return ""


def _dynamic_guidance_payload(payload: str) -> str:
    text = str(payload or "").strip()
    if len(text) <= _DYNAMIC_GUIDANCE_MAX_PAYLOAD_CHARS:
        return text
    keep = max(1000, (_DYNAMIC_GUIDANCE_MAX_PAYLOAD_CHARS - 260) // 2)
    omitted = len(text) - (keep * 2)
    return (
        text[:keep].rstrip()
        + "\n\n[dynamic guidance payload shortened: "
        + f"{omitted} chars omitted; durable facts should live in tool results, task_plan, agent_state, or artifacts]"
        + "\n动态提示过长已折叠；持久事实应保存在工具结果、task_plan、agent_state 或产物中。\n\n"
        + text[-keep:].lstrip()
    )


def _dynamic_guidance_tag_already_pending(msgs: list[dict], tag: str) -> bool:
    if not tag:
        return False
    marker = f"[{tag}]"
    for message in reversed(msgs):
        if not isinstance(message, dict):
            continue
        content = str(message.get("content") or "")
        if (
            "Tool Loop Dynamic Guidance" not in content
            and "Tool Schema Retry Facts" not in content
            and "[SYSTEM_HINT/" not in content
            and "[SYSTEM_HELPER_SUMMARY/" not in content
            and "[SYSTEM_RECALL/" not in content
        ):
            continue
        if marker in content or (tag == "Tool Schema Retry Facts" and "## Tool Schema Retry Facts" in content):
            return True
    return False


def _remove_pending_dynamic_guidance_tag(msgs: list[dict], tag: str) -> int:
    """Remove an older transient guidance block with the same tag before refresh."""
    if not tag:
        return 0
    marker = f"[{tag}]"
    removed = 0
    kept: list[dict] = []
    for message in msgs:
        if not isinstance(message, dict):
            kept.append(message)
            continue
        content = message.get("content")
        if not isinstance(content, str) or marker not in content:
            kept.append(message)
            continue
        cleaned, changed = _strip_transient_tool_loop_guidance_text(content)
        if changed:
            removed += 1
            if cleaned:
                kept.append({**message, "content": cleaned})
            elif message.get("role") == "tool":
                kept.append({**message, "content": "(transient tool-loop guidance refreshed)"})
        else:
            kept.append(message)
    if removed:
        msgs[:] = kept
        try:
            debug.log(
                "llm.tools.dynamic_guidance.replaced",
                f"replaced={removed} tag={tag}",
            )
        except Exception:
            pass
    return removed


def _replace_pending_dynamic_guidance_tag(msgs: list[dict], tag: str, payload: str) -> bool:
    """Replace a still-pending dynamic guidance block that carries the same tag."""
    if not tag:
        return False
    marker = f"[{tag}]"
    for idx in range(len(msgs) - 1, -1, -1):
        message = msgs[idx]
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, str) or marker not in content:
            continue
        if message.get("role") == "user" and content.startswith("## Tool Loop Dynamic Guidance"):
            parts = re.split(r"\n\n---\n\n", content)
            new_parts = []
            replaced = False
            for part in parts:
                if marker in part:
                    if not replaced:
                        if part.startswith("## Tool Loop Dynamic Guidance"):
                            head, sep, _ = part.partition("\n\n[SYSTEM_")
                            new_parts.append(head + ("\n\n" if sep else "\n\n") + payload)
                        else:
                            new_parts.append(payload)
                        replaced = True
                    continue
                new_parts.append(part)
            if replaced:
                message["content"] = "\n\n---\n\n".join(new_parts)
                try:
                    debug.log(
                        "llm.tools.dynamic_guidance.replaced",
                        f"tag={tag}",
                    )
                except Exception:
                    pass
                return True
        cleaned, changed = _strip_transient_tool_loop_guidance_text(content)
        if changed:
            if cleaned:
                message["content"] = cleaned.rstrip() + "\n\n" + payload
            else:
                message["content"] = payload
            try:
                debug.log(
                    "llm.tools.dynamic_guidance.replaced",
                    f"tag={tag}",
                )
            except Exception:
                pass
            return True
    return False


def _append_tool_loop_dynamic_guidance(
    msgs: list[dict],
    content: str,
    *,
    refresh_same_tag: bool = False,
) -> None:
    """Attach runtime guidance without creating avoidable message-boundary churn."""
    payload = _dynamic_guidance_payload(content or "")
    if not payload:
        return
    tag = _dynamic_guidance_tag(payload)
    if _dynamic_guidance_tag_already_pending(msgs, tag):
        if refresh_same_tag:
            if _replace_pending_dynamic_guidance_tag(msgs, tag, payload):
                return
            _remove_pending_dynamic_guidance_tag(msgs, tag)
        else:
            try:
                debug.log(
                    "llm.tools.dynamic_guidance.deduped",
                    f"pending tag={tag}",
                )
            except Exception:
                pass
            return
    if _dynamic_guidance_tag_already_pending(msgs, tag):
        try:
            debug.log(
                "llm.tools.dynamic_guidance.deduped",
                f"pending tag={tag}",
            )
        except Exception:
            pass
        return
    wrapped = (
        "## Tool Loop Dynamic Guidance\n"
        "Read-only runtime guidance for the next tool-planning step. It preserves the system/persona frame and records current progress, recovery hints, or convergence checkpoints.\n\n"
        "工具循环动态提示，只读；不改变系统与人设框架。\n\n"
        + payload
    )

    # Tool-loop hints are dynamic by nature. Prefer extending an already-dynamic
    # trailing tool result or guidance message instead of appending a fresh user
    # message, so stable system/tool prefixes remain cacheable across iterations.
    if msgs:
        last = msgs[-1]
        role = last.get("role")
        if role == "tool":
            old = last.get("content", "") or ""
            last["content"] = old + "\n\n" + wrapped
            return
        if role == "user" and str(last.get("content") or "").startswith("## Tool Loop Dynamic Guidance"):
            old = last.get("content", "") or ""
            last["content"] = old + "\n\n---\n\n" + payload
            return

    msgs.append({
        "role": "user",
        "content": wrapped,
    })


_TRANSIENT_TOOL_GUIDANCE_PREFIXES = (
    "## Tool Loop Dynamic Guidance",
    "## Tool Schema Retry Facts",
    "[SYSTEM_HINT/",
    "[SYSTEM_HELPER_SUMMARY/",
    "[SYSTEM_RECALL/",
)


def _strip_transient_tool_loop_guidance_text(text: str) -> tuple[str, bool]:
    """Remove one-shot tool-loop guidance after the model has seen it.

    Runtime hints are meant to affect the next model decision only. Durable
    facts must stay in normal tool results, agent_state, todo state, or helper
    reports; stale guidance should not keep polluting later prompt tails.
    """
    value = str(text or "")
    stripped = value.lstrip()
    if any(stripped.startswith(prefix) for prefix in _TRANSIENT_TOOL_GUIDANCE_PREFIXES):
        return "", True
    needles = (
        "\n\n## Tool Loop Dynamic Guidance\n",
        "\n\n## Tool Schema Retry Facts\n",
        "\n\n[SYSTEM_HINT/",
        "\n\n[SYSTEM_HELPER_SUMMARY/",
        "\n\n[SYSTEM_RECALL/",
    )
    cut_at: int | None = None
    for needle in needles:
        idx = value.find(needle)
        if idx >= 0 and (cut_at is None or idx < cut_at):
            cut_at = idx
    if cut_at is None:
        return value, False
    return value[:cut_at].rstrip(), True


def _clear_transient_tool_loop_guidance(
    msgs: list[dict],
    *,
    reason: str = "",
    action_taken: bool = True,
) -> int:
    """Clear one-shot dynamic guidance from prior messages.

    Runtime guidance should leave the prompt tail as soon as it has been
    converted into an action or replaced by durable facts. It should not be
    removed merely because the model emitted a non-tool response; that can be a
    failed/unfinished finalization attempt, and clearing there loses the active
    correction before the corresponding task has moved.
    """
    if not action_taken:
        try:
            debug.log(
                "llm.tools.dynamic_guidance.retained",
                f"reason={reason}",
            )
        except Exception:
            pass
        return 0
    removed = 0
    kept: list[dict] = []
    for message in msgs:
        if not isinstance(message, dict):
            kept.append(message)
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content:
            kept.append(message)
            continue
        cleaned, changed = _strip_transient_tool_loop_guidance_text(content)
        if not changed:
            kept.append(message)
            continue
        removed += 1
        if cleaned:
            kept.append({**message, "content": cleaned})
        elif message.get("role") == "tool":
            kept.append({**message, "content": "(transient tool-loop guidance cleared)"})
    if removed:
        msgs[:] = kept
        try:
            debug.log(
                "llm.tools.dynamic_guidance.cleared",
                f"cleared={removed} reason={reason}",
            )
        except Exception:
            pass
    return removed


def _clear_unused_schema_retry_guidance_after_response(
    tool_calls: object,
    *,
    trace_id: str | None = None,
    reason: str = "",
) -> int:
    """Clear transient schema expansion once the model stops using that tool.

    A schema retry hint is a one-call factual overlay. If the model responds by
    calling the repaired tool, result handling below keeps or clears state based
    on that tool result. If it calls a different tool or finalizes, the old full
    schema should not be injected again on the next iteration.
    """
    try:
        from app.core import toolchain_cache as _toolchain_cache

        expanded = _toolchain_cache.expanded_schema_tools(trace_id)
        if not expanded:
            return 0
        called: set[str] = set()
        for tc in tool_calls or []:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", None)
            if not name and isinstance(tc, dict):
                fn_dict = tc.get("function")
                if isinstance(fn_dict, dict):
                    name = fn_dict.get("name")
            if name:
                called.add(str(name))
        stale = expanded.difference(called)
        for name in sorted(stale):
            _toolchain_cache.clear_tool_schema_retry(
                name,
                trace_id,
                reason=reason or "model did not call expanded-schema tool",
            )
        if stale:
            debug.log(
                "llm.tools.schema_retry_guidance.cleared",
                f"cleared={len(stale)} reason={reason}",
                {"tools": sorted(stale), "called": sorted(called)},
            )
        return len(stale)
    except Exception:
        return 0


def _tools_loop_usage_tag(task_id: str | None) -> str:
    safe_task = str(task_id or "").strip()
    if not safe_task:
        return "main"
    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", safe_task)[:80] or "helper"
    return f"helper.{safe_task}"


def _tools_loop_shape_label(iter_no: int, task_id: str | None, suffix: str = "") -> str:
    safe_suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(suffix or "").strip(". "))[:50]
    safe_task = str(task_id or "").strip()
    if safe_task:
        safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", safe_task)[:80] or "helper"
        base = f"tools_loop.iter{iter_no}.helper.{safe_task}"
    else:
        base = f"tools_loop.iter{iter_no}.main"
    return f"{base}.call.{safe_suffix}" if safe_suffix else base


def _compact_tool_args(args, *, max_chars: int = 700) -> str:
    try:
        text = json.dumps(args if isinstance(args, (dict, list)) else {}, ensure_ascii=False)
    except Exception:
        text = "{}"
    if len(text) > max_chars:
        return text[: max_chars - 40] + "...[truncated]"
    return text


def _tool_result_error_kind(result: str | None) -> str:
    try:
        data = json.loads(result or "")
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("error_kind") or data.get("blocked_reason") or data.get("error") or "").strip()


def _compact_blocked_large_write_tool_args_in_last_assistant(
    msgs: list[dict],
    *,
    tool_name: str = "workspace",
    call_id: str,
    args: dict,
    result: str,
) -> bool:
    """Replace huge blocked write content in the just-appended assistant tool_call."""
    if not isinstance(args, dict):
        return False
    action = str(args.get("action") or "").strip().lower()
    original = str(args.get("content") or "")
    normalized_tool = str(tool_name or "").strip()
    is_workspace_write = normalized_tool == "workspace" and action in {"write", "append"}
    is_env_apply_create = normalized_tool == "env_apply_create"
    if not (is_workspace_write or is_env_apply_create) or len(original) <= 4000:
        return False
    try:
        parsed = json.loads(result or "{}")
    except Exception:
        parsed = {}
    if not isinstance(parsed, dict) or parsed.get("ok") is not False:
        return False
    compactable_error_kinds = {
        "main_thread_large_write_should_delegate_or_segment",
        "main_thread_source_create_should_delegate",
        "main_thread_project_artifact_create_should_delegate",
        "environment_workspace_write_not_project_file",
    }
    if not (_tool_result_error_kind(result) in compactable_error_kinds or str(parsed.get("warning") or "").strip()):
        return False
    if not call_id:
        return False
    try:
        from app.core import debug as _debug
        reason = _tool_result_error_kind(result) or str(parsed.get("warning") or "blocked_write")
        compact_args = dict(args)
        compact_args["content"] = (
            f"[content omitted after main-thread blocked write feedback; "
            f"original_chars={len(original)}, original_lines={original.count(chr(10)) + (1 if original else 0)}]"
        )
        compact_args["_omitted_content_reason"] = reason
        replacement = json.dumps(compact_args, ensure_ascii=False)
        for message in reversed(msgs):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            tool_calls = message.get("tool_calls") or []
            if not isinstance(tool_calls, list):
                return False
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict) or str(tool_call.get("id") or "") != call_id:
                    continue
                fn = tool_call.get("function")
                if not isinstance(fn, dict):
                    return False
                fn["arguments"] = replacement
                _debug.log(
                    "llm.tools.large_write_args_compacted",
                    f"compacted blocked write args for tool={normalized_tool} call_id={call_id} chars={len(original)}",
                )
                return True
            break
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).exception("failed to compact blocked large write tool args")
    return False


def _compact_delegate_input_file_source_blocks_in_last_assistant(
    msgs: list[dict],
    *,
    call_id: str,
    args: dict,
) -> bool:
    """Replace redundant source bodies in delegate tool-call args kept in history."""
    if not isinstance(args, dict) or not call_id:
        return False
    tasks = args.get("tasks")
    if not isinstance(tasks, list):
        return False
    try:
        from app.llm.tools.delegate import _strip_redundant_input_file_source_blocks
        compact_args = dict(args)
        compact_tasks = []
        omitted_total = 0
        for task in tasks:
            if not isinstance(task, dict):
                compact_tasks.append(task)
                continue
            input_files = task.get("input_files") or []
            prompt = str(task.get("prompt") or "")
            compact_prompt, omitted = _strip_redundant_input_file_source_blocks(prompt, input_files)
            if omitted:
                task_copy = dict(task)
                task_copy["prompt"] = compact_prompt
                task_copy["_omitted_source_blocks"] = omitted[:8]
                compact_tasks.append(task_copy)
                omitted_total += len(omitted)
            else:
                compact_tasks.append(task)
        if omitted_total <= 0:
            return False
        compact_args["tasks"] = compact_tasks
        compact_args["_omitted_source_blocks_reason"] = (
            "source bodies duplicated files named in input_files; producer can read current staged file bodies"
        )
        replacement = json.dumps(compact_args, ensure_ascii=False)
        for message in reversed(msgs):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            tool_calls = message.get("tool_calls") or []
            if not isinstance(tool_calls, list):
                return False
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict) or str(tool_call.get("id") or "") != call_id:
                    continue
                fn = tool_call.get("function")
                if not isinstance(fn, dict):
                    return False
                fn["arguments"] = replacement
                from app.core import debug as _debug
                _debug.log(
                    "llm.tools.delegate_source_args_compacted",
                    f"compacted delegate source blocks call_id={call_id} omitted={omitted_total}",
                )
                return True
            break
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).exception("failed to compact delegate source blocks")
    return False


def _delegate_workflow_result_summary(result) -> dict | None:
    """Return compact structured delegate facts for user-visible progress.

    The delegate field `helpers_completed` means results returned, not helpers
    that succeeded. User-visible progress must use success_count/task_ok and
    per-result terminal facts instead of treating returned helpers as complete.

    delegate 的 helpers_completed 表示返回结果数；进度展示以成功数和终态事实为准。
    """
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            return None
    if not isinstance(result, dict):
        return None
    items = result.get("results") or []
    completed_ids: list[str] = []
    failed_ids: list[str] = []
    missing_by_task: dict[str, list] = {}
    staged_project_files: list[str] = []
    main_available_files: list[str] = []
    copied_project_files: list[str] = []
    browser_evidence_facts: list[dict] = []
    browser_evidence_gap_facts: list[dict] = []
    result_items: list[dict] = []

    def _append_paths(target: list[str], values) -> None:
        if not isinstance(values, list):
            return
        for value in values:
            norm = str(value or "").replace("\\", "/").strip()
            if norm and norm not in target:
                target.append(norm)

    def _browser_evidence_from_helper_item(item: dict) -> dict | None:
        texts: list[str] = []
        for key in ("browser_evidence", "browser_evidence_fact", "summary", "report", "content"):
            value = item.get(key)
            if value in (None, "", [], {}):
                continue
            if isinstance(value, (dict, list)):
                texts.append(json.dumps(value, ensure_ascii=False))
            else:
                texts.append(str(value))
        outputs_check = item.get("outputs_check")
        if isinstance(outputs_check, dict):
            for key in ("browser_evidence", "browser_evidence_fact", "producer_boundary_fact"):
                value = outputs_check.get(key)
                if value in (None, "", [], {}):
                    continue
                texts.append(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))
        text = "\n".join(texts)
        if not text:
            return None
        # 2026-06-11 Round 17: signal definitions shared with the benchmark
        # trace exporter via browser_evidence_signals — one source of truth.
        from app.llm.tools.browser_evidence_signals import (
            has_browser_evidence_signal,
            has_negative_browser_boundary,
        )
        if has_negative_browser_boundary(text):
            return None
        if not has_browser_evidence_signal(text):
            return None
        urls = []
        for match in _URL_RE.findall(text):
            url = str(match).rstrip(".,;)")
            if url and url not in urls:
                urls.append(url)
            if len(urls) >= 6:
                break
        tid = str(item.get("task_id") or "").strip()
        return {
            "task_id": tid,
            "source": "result_summary",
            "urls": urls,
            "fact": (
                "Available result evidence shows browser-family evidence using an actual browser automation or host-browser route. "
                "Use that fact rather than re-running the browser solely to prove it existed."
            ),
        }

    def _browser_evidence_gap_from_helper_item(item: dict) -> dict | None:
        texts: list[str] = []
        for key in ("summary", "report", "content", "missing", "warnings"):
            value = item.get(key)
            if value in (None, "", [], {}):
                continue
            texts.append(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))
        outputs_check = item.get("outputs_check")
        if isinstance(outputs_check, dict):
            for key in ("quality_warnings", "blocking_quality_warnings", "missing_or_warnings"):
                value = outputs_check.get(key)
                if value in (None, "", [], {}):
                    continue
                texts.append(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))
        text = "\n".join(texts)
        if not text:
            return None
        from app.llm.tools.browser_evidence_signals import has_browser_gap_signal
        if not has_browser_gap_signal(text):
            return None
        urls = []
        for match in _URL_RE.findall(text):
            url = str(match).rstrip(".,;)")
            if url and url not in urls:
                urls.append(url)
            if len(urls) >= 6:
                break
        return {
            "task_id": str(item.get("task_id") or "").strip(),
            "source": "result_summary",
            "urls": urls,
            "fact": (
                "Available result evidence shows the requested browser/host-browser evidence boundary was not satisfied "
                "or was blocked in that tool environment. This is a separate active-task evidence fact; it does "
                "not invalidate artifact self-checks, and it should not be treated as "
                "browser-family evidence."
            ),
        }

    if isinstance(items, list):
        for item in items[:20]:
            if not isinstance(item, dict):
                continue
            tid = str(item.get("task_id") or "").strip()
            if not tid:
                continue
            outputs = item.get("outputs_check") if isinstance(item.get("outputs_check"), dict) else {}
            missing = outputs.get("outputs_missing") or item.get("outputs_missing") or []
            terminal = str(item.get("terminal_reason") or "").strip().lower()
            ok = item.get("ok")
            outputs_complete = outputs.get("outputs_complete")
            if missing:
                missing_by_task[tid] = list(missing)[:8]
            producer_verified = outputs.get("producer_self_verified") is True
            quality_blocked = bool(outputs.get("quality_blocked") or item.get("quality_blocked"))
            if (
                producer_verified
                or (ok is True and outputs_complete is not False and not missing and not quality_blocked)
            ):
                completed_ids.append(tid)
            elif ok is False or missing or terminal in {
                "failed",
                "interrupted",
                "stuck",
                "timeout",
                "crashed",
                "resource_required",
                "quality_blocked",
                "outputs_missing",
            }:
                failed_ids.append(tid)
            _append_paths(staged_project_files, item.get("staged_project_files"))
            _append_paths(main_available_files, item.get("main_available_files") or item.get("workspace_files") or item.get("files"))
            copy_stats = item.get("copy_stats") if isinstance(item.get("copy_stats"), dict) else {}
            _append_paths(copied_project_files, copy_stats.get("env_copied_files"))
            browser_evidence = _browser_evidence_from_helper_item(item)
            if browser_evidence is not None:
                browser_evidence_facts.append(browser_evidence)
            browser_gap = _browser_evidence_gap_from_helper_item(item)
            if browser_gap is not None:
                browser_evidence_gap_facts.append(browser_gap)
            result_item = {
                "task_id": tid,
                "kind": item.get("kind"),
                "ok": ok,
                "terminal_reason": item.get("terminal_reason"),
                "outputs_complete": outputs_complete,
                "producer_self_verified": producer_verified,
                "outputs_missing": list(missing)[:8] if isinstance(missing, list) else missing,
                "main_available_files": list(main_available_files[-8:]),
                "staged_project_files": list(staged_project_files[-8:]),
                "copy_stats": {
                    key: copy_stats.get(key)
                    for key in (
                        "env_copied_count",
                        "env_copied_files",
                        "copied_source_count",
                        "skipped_source",
                        "capped",
                    )
                    if key in copy_stats
                },
            }
            for _candidate_key in ("voice_reply_file_candidate", "deliverable_candidate", "delivery_guidance"):
                if item.get(_candidate_key):
                    result_item[_candidate_key] = item.get(_candidate_key)
            if browser_evidence is not None:
                result_item["browser_evidence"] = browser_evidence
            if browser_gap is not None:
                result_item["browser_evidence_gap"] = browser_gap
            result_items.append(result_item)
    still_items = result.get("still_running") or []
    running_ids: list[str] = []
    if isinstance(still_items, list):
        for item in still_items[:20]:
            tid = str(item.get("task_id") if isinstance(item, dict) else item or "").strip()
            if tid:
                running_ids.append(tid)
    return {
        "action": result.get("action"),
        "task_ok": result.get("task_ok"),
        "helpers_requested": _first_present(
            result,
            "helpers_requested",
            "background_work_requested",
            "helpers_initially_spawned",
            "background_work_started",
            default=0,
        ),
        "helpers_returned": _first_present(result, "helpers_completed", "results_returned", default=0),
        "helpers_still_running": _first_present(
            result,
            "helpers_still_running",
            "background_work_running",
            default=0,
        ),
        "success_count": result.get("success_count", len(completed_ids)),
        "incomplete_count": result.get("incomplete_count", 0),
        "failed_count": result.get("failed_count", 0),
        "interrupted_count": result.get("interrupted_count", 0),
        "resource_required_count": result.get("resource_required_count", 0),
        "quality_blocked_count": result.get("quality_blocked_count", 0),
        "completed_task_ids": completed_ids,
        "failed_task_ids": failed_ids,
        "running_task_ids": running_ids,
        "missing_outputs_by_task": missing_by_task,
        "main_available_files": main_available_files[:12],
        "staged_project_files": staged_project_files[:12],
        "copied_project_files": copied_project_files[:12],
        "browser_evidence_facts": browser_evidence_facts[:8],
        "browser_evidence_gap_facts": browser_evidence_gap_facts[:8],
        "result_items": result_items[:12],
        "task_status": result.get("_task_status"),
    }


def _workflow_event_status_for_tool_result(tool: str, result, fallback_status: str) -> tuple[str, bool | None]:
    """Return event-layer status without changing tool-loop control semantics.

    `_tool_result_signal` deliberately treats delegate task_ok=false as a
    failure signal so the model cannot finalize incomplete work. Workflow events
    need a different distinction: a delegate wait-window snapshot can be a
    successful tool return while the helper is still running.
    """
    parsed_result = None
    try:
        parsed_result = json.loads(result) if isinstance(result, str) else result
    except Exception:
        parsed_result = None
    if isinstance(parsed_result, dict) and isinstance(parsed_result.get("ok"), bool):
        ok = bool(parsed_result["ok"])
    else:
        ok = None
    try:
        signal_ok, _ = _tool_result_signal(result)
        if isinstance(signal_ok, bool):
            ok = signal_ok
    except Exception:
        pass
    if (tool or "").strip() != "delegate":
        return ("done" if ok else "error") if ok is not None else fallback_status, ok
    try:
        parsed = parsed_result if isinstance(parsed_result, dict) else (json.loads(result) if isinstance(result, str) else result)
    except Exception:
        parsed = None
    if not isinstance(parsed, dict):
        return ("done" if ok else "error") if ok is not None else fallback_status, ok
    try:
        ok, _ = _tool_result_signal(result)
    except Exception:
        pass
    tool_return_ok = parsed.get("ok")
    running = int(_first_present(parsed, "helpers_still_running", "background_work_running", default=0) or 0)
    incomplete = int(parsed.get("incomplete_count") or 0)
    failed = int(parsed.get("failed_count") or 0)
    interrupted = int(parsed.get("interrupted_count") or 0)
    resource_required = int(parsed.get("resource_required_count") or 0)
    quality_blocked = int(parsed.get("quality_blocked_count") or 0)
    if tool_return_ok is True and running > 0:
        return "running", True
    if tool_return_ok is True and (
        parsed.get("task_ok") is False
        or incomplete
        or failed
        or interrupted
        or resource_required
        or quality_blocked
    ):
        return "incomplete", True
    if tool_return_ok is False:
        return "error", False
    return ("done" if ok else "error") if ok is not None else fallback_status, ok


def _publish_main_tool_event(kind: str, *, tool: str, iteration: int, status: str = "running", args=None, result=None, elapsed_sec: float | None = None, call_id: str = "") -> None:
    """Publish main-thread tool activity to the environment monitor."""
    try:
        from app.core import debug as _debug
        from app.core.core_processes import ProcessRegistry, current_helper_proc_id, current_owner
        from app.core.environment_events import publish_workflow_event
        from app.core.runtime_mode import current_environment

        helper_proc_id = current_helper_proc_id()
        env = current_environment()
        trace_id = _debug.current_trace_id() or ""
        payload = {
            "kind": kind,
            "proc_type": "helper" if helper_proc_id is not None else "main",
            "tool": tool,
            "iteration": iteration,
            "status": status,
            "trace_id": trace_id,
            "call_id": call_id,
        }
        if helper_proc_id is not None:
            payload["proc_id"] = helper_proc_id
            parent_trace_id = ProcessRegistry.trace_id_of(current_owner() or "") or ""
            if parent_trace_id and parent_trace_id != trace_id:
                payload["parent_trace_id"] = parent_trace_id
        if env is not None:
            payload.update({
                "archive_id": env.archive_id,
                "group_id": env.group_id,
                "user_id": env.user_id,
            })
        if args is not None:
            payload["args_preview"] = _compact_tool_args(args)
        if elapsed_sec is not None:
            payload["elapsed_seconds"] = round(float(elapsed_sec), 3)
        if result is not None:
            event_status, ok = _workflow_event_status_for_tool_result(tool, result, status)
            payload["status"] = event_status
            payload["ok"] = ok
            payload["result_preview"] = str(result or "")[:700]
            try:
                parsed_result = json.loads(result) if isinstance(result, str) else result
            except Exception:
                parsed_result = None
            if isinstance(parsed_result, dict):
                mutation_summary = {
                    key: parsed_result.get(key)
                    for key in (
                        "project_mutation_fact",
                        "project_mutations",
                        "created_project_files",
                        "modified_project_files",
                        "deleted_project_files",
                    )
                    if parsed_result.get(key)
                }
                if mutation_summary:
                    existing_summary = payload.get("result_summary")
                    if not isinstance(existing_summary, dict):
                        existing_summary = {}
                    existing_summary.update(mutation_summary)
                    payload["result_summary"] = existing_summary
            if tool == "delegate":
                summary = _delegate_workflow_result_summary(result)
                if summary is not None:
                    existing_summary = payload.get("result_summary")
                    if isinstance(existing_summary, dict):
                        payload["result_summary"] = {**existing_summary, **summary}
                    else:
                        payload["result_summary"] = summary
        publish_workflow_event(payload)
    except Exception:
        pass


def _current_task_contract_snapshot(max_chars: int = 3200) -> str:
    """Return a compact current-task contract for long toolchain reminders."""
    text = _current_task_plan_focus_snapshot(max_chars=max_chars).strip()
    return text or "(current task context unavailable)"


def _task_focus_refresh_hint(
    *,
    iteration: int,
    task_id: str | None,
    helper_kind: str | None,
    chars_total: int,
) -> str:
    """Build a low-frequency task-focus refresh for long tool loops.

    This is intentionally factual rather than prescriptive: it re-exposes the
    current request, plan, and contract near the prompt tail without deciding
    whether the task is complete.
    """
    role = "main" if task_id is None else f"helper:{task_id}"
    snapshot = _current_task_contract_snapshot(max_chars=4200 if task_id is None else 3000)
    lines = [
        "[SYSTEM_HINT/current_task_focus_refresh]",
        f"iteration={iteration}; loop_role={role}; helper_kind={helper_kind or 'none'}; context_chars={chars_total}",
        "Current-task facts for attention refresh. These facts do not force completion, continuation, or a specific tool call.",
        "Use them to compare the next action or final JSON against the active request, current plan snapshot, declared deliverables, acceptance points, and verified evidence.",
        "For audit/review/optimization/root-cause outputs, requested counts are ceilings, not quotas: include fewer strong findings when evidence supports fewer, and label weak leads with the missing direct evidence instead of converting them into definite conclusions.",
    ]
    if task_id is None:
        lines.append(
            "If new memory/tool evidence changes the active task, plan, deliverables, or acceptance points, update the active plan/contract with the available planning tool before relying on the old snapshot."
        )
    else:
        lines.append(
            "For helper work, stay within the assigned boundary. If evidence shows the boundary is incomplete or inconsistent, report the concrete missing inputs/outputs or resume facts instead of broadening the task silently."
        )
    lines.extend([
        "",
        snapshot,
        "",
        "当前任务焦点刷新：这是事实快照，不替模型做决定；用它对照下一步、最终 JSON、交付物和验收证据。若证据显示任务边界变化，应先维护计划/契约再继续。",
    ])
    return "\n".join(lines)


def _summarize_large_tool_result(
    tool_name: str,
    result: str,
    budget: int,
    *,
    force: bool = False,
    compact: bool = False,
) -> str | None:
    """Summarize oversized tool results before falling back to head/tail truncation."""
    if not isinstance(result, str) or (len(result) <= budget and not force):
        return None
    try:
        parsed = json.loads(result)
    except Exception:
        parsed = None
    if not isinstance(parsed, dict):
        return None

    if compact:
        compact_text_limit = max(96, min(280, budget // 24))
        excerpt_text_limit = max(140, min(360, budget // 18))
        result_item_report_limit = max(120, min(320, budget // 16))
        list_item_limit = 6
    else:
        compact_text_limit = max(220, min(700, budget // 10))
        excerpt_text_limit = max(360, min(1200, budget // 4))
        result_item_report_limit = max(180, min(700, budget // 8))
        list_item_limit = 12

    def _compact_summary_value(value, *, max_items: int | None = None, max_text: int | None = None):
        if max_items is None:
            max_items = list_item_limit
        if max_text is None:
            max_text = compact_text_limit
        if isinstance(value, str):
            if len(value) <= max_text:
                return value
            return value[:max_text] + f"\n[summary excerpt: original_chars={len(value)}]"
        if isinstance(value, list):
            items = [_compact_summary_value(v, max_items=max_items, max_text=max_text) for v in value[:max_items]]
            if len(value) > max_items:
                items.append({"_remaining_items": len(value) - max_items})
            return items
        if isinstance(value, dict):
            compact: dict = {}
            for idx, key in enumerate(sorted(value.keys(), key=str)):
                if idx >= max_items:
                    compact["_remaining_keys"] = len(value) - max_items
                    break
                compact[key] = _compact_summary_value(value.get(key), max_items=max_items, max_text=max_text)
            return compact
        return value

    def _short_preview(value, *, max_text: int) -> str:
        text = "" if value is None else str(value)
        if len(text) <= max_text:
            return text
        return text[:max_text] + f" [chars={len(text)}]"

    summary: dict = {
        "ok": parsed.get("ok"),
        "action": parsed.get("action") or parsed.get("tool") or tool_name,
        "summarized": True,
        "original_chars": len(result),
    }
    if compact:
        summary["policy"] = (
            "compact structured summary; reuse these facts when enough. "
            "Only request a targeted follow-up read for a named missing detail."
        )
    else:
        summary["summary_policy"] = (
            "Large tool result was structurally summarized for the next LLM context. "
            "Use returned paths, line ranges, status fields, or rerun/read targeted segments if full evidence is needed."
        )
    for key in (
        "task_ok", "helpers_completed", "helpers_still_running",
        "results_returned", "background_work_running", "processing_records_running",
        "background_work_requested", "background_work_started",
        "processing_records_requested", "processing_records_started",
        "incomplete_count",
        "resource_required_count", "failed_count", "outputs_complete", "outputs_missing",
        "quality_blocked", "blocking_quality_warnings", "path", "read_text_path",
        "files", "deliverables", "promoted", "skipped", "next_start_block",
        "format", "paragraph_count", "block_count", "returned_block_start",
        "returned_block_end", "returned_block_count", "has_more_blocks",
        "table_count", "image_count", "slide_count", "sheet_count",
        "has_more", "total_blocks", "line_ranges", "coverage_summary", "item_counts",
        "total_lines", "shown_range", "truncated", "encoding", "size", "bytes",
        "workspace_path", "main_workspace_path", "source_path", "line_count",
        "matched_count", "count", "pattern", "is_regex", "limit", "max_results",
        "voice_reply_file_candidate", "deliverable_candidate", "delivery_guidance",
    ):
        if key in parsed:
            summary[key] = _compact_summary_value(parsed.get(key))
    for key, value in parsed.items():
        if not isinstance(key, str):
            continue
        if (
            key == "tool_result_truncated"
            or key == "output_truncated"
            or key == "visible_excerpt_policy"
            or key.endswith("_truncated")
            or key.endswith("_original_chars")
            or key.endswith("_full_saved_path")
            or key in {"full_result_saved_path", "full_result_original_chars"}
        ):
            summary[key] = _compact_summary_value(value, max_items=6, max_text=260)
    if tool_name == "office" and str(parsed.get("action") or "").lower() == "read":
        headings = parsed.get("headings")
        if isinstance(headings, list):
            summary["headings"] = [
                {
                    "block_index": item.get("block_index"),
                    "level": item.get("level"),
                    "text": _short_preview(item.get("text"), max_text=120),
                }
                for item in headings[:40]
                if isinstance(item, dict)
            ]
            if len(headings) > 40:
                summary["headings_more"] = len(headings) - 40
        tables = parsed.get("tables")
        if isinstance(tables, list):
            summary["table_summaries"] = [
                {
                    "index": item.get("index", idx),
                    "rows": len(item.get("rows") or []) if isinstance(item, dict) else None,
                    "first_row": _compact_summary_value(
                        (item.get("rows") or [None])[0] if isinstance(item, dict) and item.get("rows") else None,
                        max_items=8,
                        max_text=80,
                    ),
                }
                for idx, item in enumerate(tables[:12])
                if isinstance(item, dict)
            ]
            if len(tables) > 12:
                summary["tables_more"] = len(tables) - 12
        for key in ("figure_consistency", "orphan_image_rels", "orphan_image_hint"):
            if key in parsed:
                summary[key] = _compact_summary_value(parsed.get(key))
    if isinstance(parsed.get("matches"), list):
        match_items = []
        for item in parsed["matches"][:list_item_limit]:
            if not isinstance(item, dict):
                continue
            match_items.append({
                "path": item.get("path"),
                "line": item.get("line") or item.get("line_no"),
                "preview": _short_preview(
                    item.get("preview") or item.get("text") or item.get("line_text"),
                    max_text=min(compact_text_limit, 160 if compact else compact_text_limit),
                ),
            })
        summary["matches"] = match_items
        if len(parsed["matches"]) > len(match_items):
            summary["matches_more"] = len(parsed["matches"]) - len(match_items)
    if isinstance(parsed.get("outputs_check"), dict):
        oc = parsed["outputs_check"]
        summary["outputs_check"] = {
            k: oc.get(k)
            for k in (
                "outputs_complete", "outputs_missing", "delivered_count",
                "quality_blocked", "blocking_quality_warnings", "quality_warnings",
            )
            if k in oc
        }
    if tool_name == "env_inventory" and isinstance(parsed.get("resources"), list):
        _row_limit = 40 if compact else 60
        _rows = parsed["resources"][:_row_limit]
        summary["resources"] = [
            {k: r.get(k) for k in ("project_path", "category", "suffix", "staged", "key_candidate") if k in r}
            for r in _rows if isinstance(r, dict)
        ]
        if len(parsed["resources"]) > _row_limit:
            summary["resources_truncated"] = len(parsed["resources"]) - _row_limit
        for k in ("summary", "filters", "truncated", "manifest_paths", "staged_now"):
            if k in parsed:
                summary[k] = _compact_summary_value(parsed[k])
        summary["next_action_instruction"] = (
            "Full inventory is at _env/project_inventory.md and _env/.resource_manifest.json. "
            "Use project_path or staged_path values as helper input_files; for coding/debugging, the path list is enough "
            "for a compact code-helper request without expanding source bodies in the main thread.\n\n"
            "完整清单在 _env/project_inventory.md 和 _env/.resource_manifest.json；代码调试优先把路径作为 helper input_files。"
        )
    if isinstance(parsed.get("results"), list):
        items = []
        for item in parsed["results"][:12]:
            if not isinstance(item, dict):
                continue
            oc = item.get("outputs_check") if isinstance(item.get("outputs_check"), dict) else {}
            _raw_files = item.get("files") or item.get("main_available_files") or item.get("committed_files")
            _display_files = _raw_files
            if isinstance(_raw_files, list):
                _display_files = [
                    value for value in _raw_files
                    if not str(value or "").replace("\\", "/").startswith("_env/")
                ]
            items.append({
                "task_id": item.get("task_id"),
                "kind": item.get("kind"),
                "ok": item.get("ok"),
                "terminal_reason": item.get("terminal_reason"),
                "outputs_complete": oc.get("outputs_complete"),
                "outputs_missing": oc.get("outputs_missing"),
                "quality_blocked": oc.get("quality_blocked") or item.get("quality_blocked"),
                "files": _display_files,
                "voice_reply_file_candidate": item.get("voice_reply_file_candidate"),
                "deliverable_candidate": item.get("deliverable_candidate"),
                "delivery_guidance": _compact_summary_value(item.get("delivery_guidance")),
                "staged_project_files": _compact_summary_value(item.get("staged_project_files")),
                "pending_project_apply_fact": item.get("pending_project_apply_fact"),
                "internal_evidence_files": _compact_summary_value(item.get("internal_evidence_files")),
                "read_evidence_summary": _compact_summary_value(item.get("read_evidence_summary")),
                "report_excerpt": str(item.get("report") or "")[:result_item_report_limit],
            })
        summary["result_items"] = items
        if len(parsed["results"]) > len(items):
            summary["result_items_more"] = len(parsed["results"]) - len(items)
    for text_key in ("summary", "report", "stdout", "stderr", "error", "details", "content"):
        if text_key in parsed and parsed.get(text_key) is not None:
            text = str(parsed.get(text_key) or "")
            if text_key == "content" and tool_name in {"read_file", "read_function"}:
                outline = _compact_read_outline(text, max_items=18 if compact else 24)
                if outline:
                    summary["content_outline"] = outline
                if compact and len(outline) >= 8:
                    excerpt = text[: min(excerpt_text_limit, 140)]
                    summary[text_key + "_excerpt"] = excerpt
                    if len(text) > len(excerpt):
                        summary[text_key + "_original_chars"] = len(text)
                    continue
            summary[text_key + "_excerpt"] = text[:excerpt_text_limit]
            if len(text) > excerpt_text_limit:
                summary[text_key + "_original_chars"] = len(text)
    serialized = stable_prompt_json(summary)
    if len(serialized) <= max(1200, budget):
        return serialized
    for key in ("stdout_excerpt", "stderr_excerpt", "content_excerpt", "report_excerpt", "details_excerpt"):
        if isinstance(summary.get(key), str) and len(summary[key]) > 300:
            summary[key] = summary[key][:300]
    for item in summary.get("result_items") or []:
        if isinstance(item, dict) and isinstance(item.get("report_excerpt"), str):
            item["report_excerpt"] = item["report_excerpt"][:300]
    summary["summary_too_large"] = True
    serialized = stable_prompt_json(summary)
    if len(serialized) <= max(1200, budget):
        return serialized
    compact = {
        "ok": summary.get("ok"),
        "action": summary.get("action"),
        "summarized": True,
        "summary_too_large": True,
        "original_chars": summary.get("original_chars"),
        "task_ok": summary.get("task_ok"),
        "results_returned": _first_present(summary, "results_returned", "helpers_completed"),
        "processing_records_running": _first_present(
            summary,
            "processing_records_running",
            "background_work_running",
            "helpers_still_running",
        ),
        "incomplete_count": summary.get("incomplete_count"),
        "resource_required_count": summary.get("resource_required_count"),
        "result_items": [
            {
                "task_id": item.get("task_id"),
                "kind": item.get("kind"),
                "ok": item.get("ok"),
                "terminal_reason": item.get("terminal_reason"),
                "outputs_complete": item.get("outputs_complete"),
                "files": item.get("files"),
                "report_excerpt": str(item.get("report_excerpt") or "")[:160],
            }
            for item in (summary.get("result_items") or [])[:6]
            if isinstance(item, dict)
        ],
    }
    for key, value in summary.items():
        if (
            isinstance(key, str)
            and (
                key == "tool_result_truncated"
                or key == "output_truncated"
                or key.endswith("_full_saved_path")
                or key in {"full_result_saved_path", "full_result_original_chars"}
            )
        ):
            compact[key] = value
    return stable_prompt_json(compact)


def _infer_tool_result_spill_root(dispatcher) -> str:
    """Best-effort workspace path for saving oversized tool results."""
    try:
        import functools
        if isinstance(dispatcher, functools.partial):
            value = (dispatcher.keywords or {}).get("workspace_dir")
            if value:
                return str(value)
            dispatcher = dispatcher.func
    except Exception:
        pass
    try:
        freevars = list(getattr(getattr(dispatcher, "__code__", None), "co_freevars", ()) or ())
        cells = list(getattr(dispatcher, "__closure__", None) or [])
        values = {name: cell.cell_contents for name, cell in zip(freevars, cells)}
        for key in ("workspace_dir", "helper_workspace", "main_workspace"):
            value = values.get(key)
            if isinstance(value, str) and value.strip():
                return value
    except Exception:
        pass
    try:
        import tempfile
        return tempfile.gettempdir()
    except Exception:
        return ""


def _write_spilled_tool_result(
    *,
    root_dir: str,
    tool_name: str,
    iteration: int,
    call_id: str,
    text: str,
    extension: str = ".json",
) -> str:
    import hashlib
    import re as _re
    import tempfile
    import time
    from pathlib import Path

    root = Path(root_dir or tempfile.gettempdir()).resolve()
    safe_tool = _re.sub(r"[^A-Za-z0-9_.-]+", "_", str(tool_name or "tool"))[:60] or "tool"
    safe_call = _re.sub(r"[^A-Za-z0-9_.-]+", "_", str(call_id or ""))[:40] or "call"
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    folder = root / "_tool_results"
    folder.mkdir(parents=True, exist_ok=True)
    suffix = extension if str(extension or "").startswith(".") else f".{extension}"
    filename = f"iter{int(iteration):03d}_{safe_tool}_{safe_call}_{int(time.time())}_{digest}{suffix}"
    path = folder / filename
    path.write_text(text, encoding="utf-8")
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _spill_large_tool_result_for_context(
    tool_name: str,
    result: str,
    *,
    spill_root: str,
    iteration: int,
    call_id: str,
    total_threshold: int = 24_000,
    field_threshold: int = 8_000,
    field_head_chars: int = 3_000,
) -> str:
    """Save long real tool output and return a bounded model-visible result.

    This is a context hygiene layer only. It does not alter the actual tool
    execution, callbacks, debug logs before this point, or agent-state evidence.
    """
    if not isinstance(result, str) or not result:
        return result
    should_spill_total = len(result) > total_threshold
    try:
        parsed = json.loads(result)
    except Exception:
        parsed = None
    long_text_keys = {
        "stdout", "stderr", "content", "contents", "file_content",
        "file_contents", "raw_content", "raw_text", "error", "errors",
        "error_result", "error_output", "details", "detail", "report",
        "summary", "output", "message", "exception", "exceptions",
        "diagnostics", "diagnostic", "warning", "warnings", "traceback",
        "tracebacks", "logs", "log", "text", "preview", "partial_stdout",
        "partial_stderr", "body", "diff",
    }

    def _nested_long_text_paths(value, path=()):
        paths: list[tuple[tuple[str, ...], str, str]] = []
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key)
                if (
                    isinstance(key, str)
                    and key in long_text_keys
                    and isinstance(item, str)
                    and len(item) > field_threshold
                ):
                    paths.append((path, key, item))
                elif isinstance(item, (dict, list)):
                    paths.extend(_nested_long_text_paths(item, (*path, key_text)))
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                if isinstance(item, (dict, list)):
                    paths.extend(_nested_long_text_paths(item, (*path, str(idx))))
        return paths

    def _apply_nested_text_spill(value, *, path=(), saved_paths: dict[str, str]) -> object:
        if isinstance(value, dict):
            nested = dict(value)
            for key, item in list(value.items()):
                key_text = str(key)
                if (
                    isinstance(key, str)
                    and key in long_text_keys
                    and isinstance(item, str)
                    and len(item) > field_threshold
                ):
                    label = "_".join((*path, key_text)) or key_text
                    if label not in saved_paths:
                        saved_paths[label] = _write_spilled_tool_result(
                            root_dir=spill_root,
                            tool_name=tool_name,
                            iteration=iteration,
                            call_id=f"{call_id}_{label}",
                            text=item,
                            extension=".txt",
                        )
                    nested[key] = item[:field_head_chars]
                    nested[f"{key}_truncated"] = True
                    nested[f"{key}_original_chars"] = len(item)
                    nested[f"{key}_full_saved_path"] = saved_paths[label]
                    nested[f"{key}_excerpt_policy"] = (
                        f"Only the head excerpt is shown; full text was saved at `{saved_paths[label]}` "
                        f"(`{key}_full_saved_path`)."
                    )
                    nested["output_truncated"] = True
                elif isinstance(item, (dict, list)):
                    nested[key] = _apply_nested_text_spill(item, path=(*path, key_text), saved_paths=saved_paths)
            return nested
        if isinstance(value, list):
            return [
                _apply_nested_text_spill(item, path=(*path, str(idx)), saved_paths=saved_paths)
                if isinstance(item, (dict, list)) else item
                for idx, item in enumerate(value)
            ]
        return value

    if isinstance(parsed, dict):
        long_keys = [
            key for key in long_text_keys
            if isinstance(parsed.get(key), str) and len(str(parsed.get(key))) > field_threshold
        ]
        nested_long_paths = _nested_long_text_paths(parsed)
        if not should_spill_total and not long_keys and not nested_long_paths:
            return result
        saved_path = _write_spilled_tool_result(
            root_dir=spill_root,
            tool_name=tool_name,
            iteration=iteration,
            call_id=call_id,
            text=result,
        )
        field_saved_paths: dict[str, str] = {}
        compact = dict(parsed)
        compact["tool_result_truncated"] = True
        compact["output_truncated"] = True
        compact["full_result_saved_path"] = saved_path
        compact["full_result_original_chars"] = len(result)
        compact["visible_excerpt_policy"] = (
            f"Long real tool output was truncated before entering model context. The full result was saved at "
            f"`{saved_path}` (`full_result_saved_path`). The model-visible result keeps only head excerpts for oversized "
            "text fields; read the saved file or a targeted segment only when the active task needs missing details.\n"
            "长工具输出已保存；上下文只保留头部摘录，必要时读取保存文件或定向片段。"
        )
        for key in long_keys:
            text = str(parsed.get(key) or "")
            if key not in field_saved_paths:
                field_saved_paths[key] = _write_spilled_tool_result(
                    root_dir=spill_root,
                    tool_name=tool_name,
                    iteration=iteration,
                    call_id=f"{call_id}_{key}",
                    text=text,
                    extension=".txt",
                )
            compact[key] = text[:field_head_chars]
            compact[f"{key}_truncated"] = True
            compact[f"{key}_original_chars"] = len(text)
            compact[f"{key}_full_saved_path"] = field_saved_paths[key]
            compact[f"{key}_excerpt_policy"] = (
                f"Only the head excerpt is shown; full text was saved at `{field_saved_paths[key]}` "
                f"(`{key}_full_saved_path`)."
            )
        if nested_long_paths:
            compact = _apply_nested_text_spill(compact, saved_paths=field_saved_paths)
        serialized = stable_prompt_json(compact)
        if len(serialized) <= total_threshold:
            return serialized
        head = serialized[: max(1200, min(total_threshold - 500, 4000))]
        fallback_summary = {
            "ok": parsed.get("ok"),
            "action": parsed.get("action") or parsed.get("tool") or tool_name,
            "tool_result_truncated": True,
            "output_truncated": True,
            "full_result_saved_path": saved_path,
            "full_result_original_chars": len(result),
            "head_excerpt": head,
            "visible_excerpt_policy": (
                f"Full tool result was saved at `{saved_path}` (`full_result_saved_path`); "
                "only a head excerpt is visible in context."
            ),
            "summary_zh": "完整工具结果已保存；上下文只显示头部摘录。",
        }
        if field_saved_paths:
            fallback_summary["field_full_saved_paths"] = dict(sorted(field_saved_paths.items()))
        return stable_prompt_json(fallback_summary)
    if len(result) <= total_threshold:
        return result
    saved_path = _write_spilled_tool_result(
        root_dir=spill_root,
        tool_name=tool_name,
        iteration=iteration,
        call_id=call_id,
        text=result,
    )
    return stable_prompt_json({
        "ok": None,
        "action": tool_name,
        "tool_result_truncated": True,
        "output_truncated": True,
        "full_result_saved_path": saved_path,
        "full_result_original_chars": len(result),
        "head_excerpt": result[:field_head_chars],
        "visible_excerpt_policy": (
            f"Full non-JSON tool result was saved at `{saved_path}` (`full_result_saved_path`); "
            "only a head excerpt is visible in context."
        ),
        "summary_zh": "完整非 JSON 工具结果已保存；上下文只显示头部摘录。",
    })


def _head_only_tool_result_fallback(result: str, *, original_chars: int, budget_chars: int) -> str:
    """Return a bounded fallback excerpt without preserving tail content."""
    head_size = max(0, int(budget_chars) - 260)
    head = str(result or "")[:head_size]
    return (
        head
        + f"\n\n[...P44 truncated tool result: original_chars={int(original_chars)}, "
        f"budget_chars={int(budget_chars)}; only the head excerpt is visible. "
        "If the result JSON includes `full_result_saved_path` or `*_full_saved_path`, "
        "read that file or a targeted segment only when needed...]"
    )


def _tool_names_from_schemas(tools: list[dict]) -> set[str]:
    """Return callable tool names advertised to the model for this loop."""
    names: set[str] = set()
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = str((tool.get("function", {}) or {}).get("name", "")).strip()
        if name:
            names.add(name)
    return names


def _looks_like_unparsed_tool_markup(content: str) -> bool:
    """Detect tool-call markup that was emitted as plain text."""
    if not content:
        return False
    markers = (
        "<｜｜DSML｜｜tool_calls>",
        "<｜｜DSML｜｜invoke",
        "<｜tool_calls｜>",
        "<tool_call>",
        "<Read file=",
        "<Write file=",
        "<Edit file=",
        "<Glob pattern=",
        "<Search pattern=",
        "<Run command=",
        "<Tool ",
        "<env_",
    )
    return any(marker in content for marker in markers)


def _delegate_items_from_result(result) -> list[dict]:
    """Return normalized per-helper delegate result items from a tool result."""
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except Exception:
        return []
    if not isinstance(parsed, dict):
        return []
    results_field = parsed.get("results")
    if isinstance(results_field, list):
        return [item for item in results_field if isinstance(item, dict)]
    if isinstance(results_field, dict):
        return [item for item in results_field.values() if isinstance(item, dict)]
    return []


def _delegate_item_outputs_complete(item: dict) -> bool:
    outputs_check = item.get("outputs_check")
    if isinstance(outputs_check, dict):
        return bool(outputs_check.get("outputs_complete", False))
    if isinstance(outputs_check, bool):
        return outputs_check
    if item.get("outputs_complete") is False:
        return False
    if item.get("outputs_missing") or item.get("declared_missing"):
        return False
    return False


def _delegate_item_is_race_lost(item: dict) -> bool:
    return bool(item.get("race_lost_to"))


def _delegate_item_is_terminal_tts_generation_fact(item: dict) -> bool:
    """TTS authorization/resource failures are final facts, not retry fuel."""
    kind = str(item.get("kind") or item.get("helper_kind") or item.get("suggested_retry_kind") or "").lower()
    resource = item.get("resource_required") if isinstance(item.get("resource_required"), dict) else {}
    resource_kind = str(
        resource.get("matching_helper_kind")
        or resource.get("suggested_helper_kind")
        or resource.get("helper_kind")
        or ""
    ).lower()
    if kind != "tts" and resource_kind != "tts":
        return False
    text = " ".join(
        str(value or "")
        for value in (
            item.get("terminal_reason"),
            item.get("error"),
            item.get("error_kind"),
            item.get("stuck_reason"),
            item.get("summary"),
            item.get("report"),
            item.get("report_excerpt"),
            resource.get("blocked_reason") if resource else "",
            resource.get("resource_resolution_facts") if resource else "",
        )
    ).lower()
    return any(
        marker in text
        for marker in (
            "tts voice profile is not configured",
            "built-in tts",
            "authorization",
            "voice authorization",
            "voice reply",
            "语音",
            "tts",
        )
    ) and (
        str(item.get("terminal_reason") or "").lower() == "resource_required"
        or bool(resource)
    )


def _delegate_item_is_incomplete(item: dict) -> bool:
    if _delegate_item_is_race_lost(item):
        return False
    terminal_reason = str(item.get("terminal_reason") or "").lower()
    outputs_check = item.get("outputs_check")
    quality_blocked = False
    if isinstance(outputs_check, dict):
        quality_blocked = bool(
            outputs_check.get("quality_blocked")
            or outputs_check.get("blocking_quality_warnings")
        )
    return bool(
        item.get("ok") is False
        or item.get("interrupted")
        or item.get("stuck")
        or item.get("error")
        or item.get("error_kind")
        or item.get("resource_required")
        or item.get("needs_resource")
        or item.get("quality_blocked")
        or quality_blocked
        or not _delegate_item_outputs_complete(item)
        or terminal_reason in {
            "stuck", "interrupted", "timeout", "failed", "error",
            "resource_required", "outputs_missing", "quality_blocked", "crashed",
        }
    )


def _delegate_item_is_nonblocking_readonly_evidence(item: dict) -> bool:
    """Read-only helper evidence can be useful even without file outputs.

    A project_map/file_summary/impact_review/inventory helper should normally return text
    evidence, not files. If such a helper asks for an edit resource only to write
    an internal report, the main workflow may synthesize from existing evidence
    instead of being forced into a resume loop.

    只读 helper 的文本结论是证据；缺内部报告文件不强制阻塞主流程。
    """
    kind = str(item.get("kind") or item.get("helper_kind") or "").lower()
    if kind not in _READONLY_HELPER_KINDS:
        return False
    outputs_check = item.get("outputs_check")
    missing: list = []
    if isinstance(outputs_check, dict):
        missing = outputs_check.get("outputs_missing") or []
    missing += item.get("outputs_missing") or []
    text = " ".join(str(x) for x in missing).lower()
    resource = item.get("resource_required") if isinstance(item.get("resource_required"), dict) else {}
    if resource:
        text += " " + str(resource.get("blocked_reason") or "").lower()
        text += " " + " ".join(str(x) for x in (resource.get("needed_outputs") or [])).lower()
    summary = str(
        item.get("summary")
        or item.get("report")
        or item.get("content")
        or item.get("report_excerpt")
        or item.get("progress_summary")
        or item.get("last_note")
        or ""
    )
    low_summary = summary.lower()
    incomplete_extract = any(
        marker in low_summary
        for marker in (
            "abort_extract",
            "did not complete an llm final summary",
            "recent reasoning excerpt",
            "interruption state",
            "let me continue",
            "let me now",
            "need to continue",
            "continue reading",
        )
    )
    has_evidence = len(summary.strip()) >= 200
    wants_internal_report = any(
        marker in text
        for marker in (
            "_helpers_shared",
            "evidence",
            "evidence.txt",
            "_evidence.txt",
            "_helper_report",
            "helper_report",
            "write the report",
            "write a report",
            "report file",
            "internal report",
            "edit helper to write",
        )
    )
    return has_evidence and wants_internal_report and not incomplete_extract


def _delegate_item_is_read_no_evidence_loop(item: dict) -> bool:
    """Return true for P130-style read helpers that should not be auto-resumed.

    The helper/stuck detector already reports a specific workflow fact: this
    read helper looped on reads without producing evidence, and the main process
    should preserve the report, use existing evidence, change consumer kind, or
    summarize the gap. Turning that state into an automatic same-task read
    resume contradicts the observed failure mode.

    P130 读循环不自动同 task_id 续作 read，避免机制推动错误恢复路径。
    """
    kind = str(item.get("kind") or item.get("helper_kind") or "").lower()
    if kind not in {"read", "ocr"}:
        return False
    text = " ".join(
        str(item.get(key) or "")
        for key in (
            "terminal_reason",
            "stuck_reason",
            "report",
            "summary",
            "content",
            "report_excerpt",
            "retry_instruction",
            "post_helper_usage_hint",
        )
    ).lower()
    return (
        "p130" in text
        and "read-helper no-evidence loop" in text
        and "without writing" in text
    )


_CURRENT_MESSAGE_MARKERS = (
    "## Current Message To Answer",
    "## Current Message To Route",
    "## Current Message To Reply To",
    "## Current User Request",
)
_INTERNAL_DYNAMIC_USER_PREFIXES = (
    "## Tool Loop Dynamic Guidance",
    "## Tool Schema Retry Facts",
    "## Round 2 Dynamic Task Guidance",
    "[SYSTEM_HINT/",
    "[SYSTEM_HELPER_SUMMARY/",
    "[SYSTEM_RECALL/",
)
_CURRENT_SEGMENT_INTERNAL_TAIL_MARKERS = (
    "\n\n---\n\n## Round 2 Dynamic Task Guidance",
    "\n\n---\n\n## Tool Loop Dynamic Guidance",
    "\n\n---\n\n## Tool Schema Retry Facts",
)
_READ_ONLY_WRITE_FORBID_RE = re.compile(
    r"(do\s+not\s+modify\s+files|don't\s+modify\s+files|no\s+file\s+modifications|"
    r"no\s+code\s+changes|analysis\s+only|read[-\s]?only|"
    r"不要(?:自行)?改|不要修改|不要写(?:入)?文件|不要动文件|只读|仅分析|不要自行改)",
    re.IGNORECASE,
)
_BROWSER_REPRO_REQUEST_RE = re.compile(
    r"(?is)"
    r"("
    r"(?:use|open|run|with|via|in)\s+(?:the\s+)?(?:host[-\s]?)?browser(?:\s+tool)?"
    r"|browser\s+tool"
    r"|host[-\s]?browser"
    r"|playwright|puppeteer|selenium|chromium"
    r"|浏览器|宿主浏览器"
    r")"
    r".{0,240}"
    r"("
    r"reproduce|replicate|observe|confirm|verify|validate|debug|fix|test"
    r"|复现|重现|观察|确认|验证|调试|修复|测试"
    r")"
    r"|("
    r"reproduce|replicate|observe|confirm|verify|validate|debug|fix|test"
    r"|复现|重现|观察|确认|验证|调试|修复|测试"
    r")"
    r".{0,240}"
    r"("
    r"(?:host[-\s]?)?browser(?:\s+tool)?|playwright|puppeteer|selenium|chromium"
    r"|浏览器|宿主浏览器"
    r")",
)
_BROWSER_PRE_EDIT_REQUEST_RE = re.compile(
    r"(?is)"
    r"("
    r"(?:browse|use|open|run|with|via|in|reproduce|replicate|observe|confirm|validate|debug|test)"
    r".{0,120}(?:host[-\s]?)?browser(?:\s+tool)?"
    r".{0,240}(?:then|before|prior\s+to|pre[-\s]?change|pre[-\s]?fix|,)"
    r".{0,160}(?:patch|fix|repair|edit|change|update|modify)"
    r"|(?:host[-\s]?)?browser(?:\s+tool)?"
    r".{0,160}(?:reproduce|replicate|observe|confirm|validate|debug|test)"
    r".{0,240}(?:then|before|prior\s+to|pre[-\s]?change|pre[-\s]?fix|,)"
    r".{0,160}(?:patch|fix|repair|edit|change|update|modify)"
    r"|(?:before|prior\s+to|pre[-\s]?change|pre[-\s]?fix)"
    r".{0,160}(?:patch|fix|repair|edit|change|update|modify)"
    r".{0,160}(?:host[-\s]?)?browser(?:\s+tool)?"
    r"|(?:先|之前|前置).{0,120}(?:浏览器|宿主浏览器).{0,160}(?:再|然后)?.{0,120}(?:修改|修复|编辑|更新|补丁)"
    r"|(?:浏览器|宿主浏览器).{0,160}(?:复现|重现|观察|确认|验证|调试|测试).{0,160}(?:再|然后|之后).{0,120}(?:修改|修复|编辑|更新|补丁)"
    r")",
)
_BROWSER_EVIDENCE_TOOL_NAMES = {
    "browser",
    "browser_open",
    "browser_click",
    "browser_type",
    "browser_screenshot",
    "browser_eval",
    "browser_navigate",
    "browser_wait",
    "browser_use",
    "host_browser",
}
_BROWSER_EVIDENCE_COMMAND_RE = re.compile(
    r"(?is)\b(playwright|puppeteer|selenium|chromium|chrome|firefox|webkit)\b"
)
_BROWSER_EVIDENCE_FACT_RE = re.compile(
    r"(?is)"
    r"("
    r"(?:browser|host[-\s]?browser|playwright|puppeteer|selenium|chromium|chrome|firefox|webkit)"
    r".{0,220}"
    r"(?:observed|saw|showed|loaded|screenshot|trace|console|passed|failed|timeout|rendered|visited|navigated)"
    r"|"
    r"(?:observed|saw|showed|loaded|screenshot|trace|console|passed|failed|timeout|rendered|visited|navigated)"
    r".{0,220}"
    r"(?:browser|host[-\s]?browser|playwright|puppeteer|selenium|chromium|chrome|firefox|webkit)"
    r"|"
    r"(?:浏览器|宿主浏览器).{0,160}(?:观察|显示|加载|截图|控制台|通过|失败|超时|渲染|访问|导航)"
    r"|"
    r"(?:观察|显示|加载|截图|控制台|通过|失败|超时|渲染|访问|导航).{0,160}(?:浏览器|宿主浏览器)"
    r")",
)
_URL_RE = re.compile(
    r"https?://(?:(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])(?::\d+)?(?:/[^\s\"']*)?|[^\s\"']+)",
    re.IGNORECASE,
)
_SOURCE_OR_PROJECT_EXTS = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".scss",
    ".json", ".toml", ".yaml", ".yml", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".java", ".go", ".rs", ".php", ".rb", ".swift", ".kt", ".sh", ".ps1",
    ".bat", ".sql", ".xml", ".vue", ".svelte",
)
_ENV_RUN_PROJECT_WRITE_EXTS = _SOURCE_OR_PROJECT_EXTS + (".md", ".txt", ".csv", ".tsv")
_STAGED_PROJECT_WRITE_PATH_RE = re.compile(
    r"(?is)(?:"
    r"(?:open|write_text|write_bytes|Path)\s*\([^)]*['\"](?P<py>_env/[A-Za-z0-9_./\\-]+)['\"][^)]*\)"
    r"|>\s*(?P<redir>_env/[A-Za-z0-9_./\\-]+)"
    r"|(?:set-content|out-file)\s+['\"]?(?P<ps>_env/[A-Za-z0-9_./\\-]+)"
    r")"
)
_PROJECT_WRITE_PATH_RE = re.compile(
    r"(?is)(?:"
    r"(?:open|Path)\s*\(\s*['\"](?P<open>(?:_env/)?[A-Za-z0-9_./\\-]+)['\"]"
    r"\s*(?:,\s*['\"][^'\"]*[wax+][^'\"]*['\"])?[^)]*\)"
    r"\s*(?:\.\s*(?:write|write_text|write_bytes)\s*\(|;|$)"
    r"|(?P<path>(?:_env/)?[A-Za-z0-9_./\\-]+)\s*[\)]?\s*\.\s*(?:write_text|write_bytes)\s*\("
    r"|>\s*(?P<redir>(?:_env/)?[A-Za-z0-9_./\\-]+)"
    r"|(?:set-content|out-file)\s+['\"]?(?P<ps>(?:_env/)?[A-Za-z0-9_./\\-]+)"
    r")"
)


def _staged_project_write_path_from_command(command: str) -> str | None:
    text = str(command or "").replace("\\", "/")
    if not text:
        return None
    lowered = text.lower()
    write_signal = (
        "open(" in lowered
        or "write_text(" in lowered
        or "write_bytes(" in lowered
        or ">" in lowered
        or "set-content" in lowered
        or "out-file" in lowered
    )
    if not write_signal:
        return None
    for match in _STAGED_PROJECT_WRITE_PATH_RE.finditer(text):
        value = match.group("py") or match.group("redir") or match.group("ps") or ""
        norm = value.replace("\\", "/").strip().lstrip("./")
        if norm:
            return norm
    return None


def _project_write_path_from_command(command: str) -> str | None:
    text = str(command or "").replace("\\", "/")
    if not text:
        return None
    staged = _staged_project_write_path_from_command(text)
    if staged:
        return staged
    lowered = text.lower()
    write_signal = (
        ".write(" in lowered
        or ".write_text(" in lowered
        or ".write_bytes(" in lowered
        or ">" in lowered
        or "set-content" in lowered
        or "out-file" in lowered
    )
    if not write_signal:
        return None
    direct_patterns = (
        r"(?is)\bopen\s*\(\s*['\"](?P<path>(?:_env/)?[A-Za-z0-9_./\\-]+)['\"]\s*,\s*['\"][^'\"]*[wax+][^'\"]*['\"]",
        r"(?is)\bPath\s*\(\s*['\"](?P<path>(?:_env/)?[A-Za-z0-9_./\\-]+)['\"]\s*\)\s*\.\s*(?:write_text|write_bytes)\s*\(",
    )
    for pattern in direct_patterns:
        match = re.search(pattern, text)
        if match:
            norm = match.group("path").replace("\\", "/").strip().lstrip("./")
            if norm and norm.lower().endswith(_ENV_RUN_PROJECT_WRITE_EXTS):
                return norm
    for match in _PROJECT_WRITE_PATH_RE.finditer(text):
        value = (
            match.group("open")
            or match.group("path")
            or match.group("redir")
            or match.group("ps")
            or ""
        )
        norm = value.replace("\\", "/").strip().lstrip("./")
        if norm and norm.lower().endswith(_ENV_RUN_PROJECT_WRITE_EXTS):
            return norm
    return None


def _current_turn_forbids_file_writes(messages: list[dict]) -> bool:
    """Detect explicit current-turn read-only/no-modification requests."""
    latest_user = ""
    for msg in reversed(messages or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = str(msg.get("content") or "")
        if any(marker in text for marker in _CURRENT_MESSAGE_MARKERS):
            latest_user = text
            break
        if text.lstrip().startswith(_INTERNAL_DYNAMIC_USER_PREFIXES):
            continue
        latest_user = text
        break
    if not latest_user:
        return False
    current = latest_user
    marker_positions = [
        latest_user.rfind(marker)
        for marker in _CURRENT_MESSAGE_MARKERS
        if latest_user.rfind(marker) >= 0
    ]
    if marker_positions:
        current = latest_user[max(marker_positions):]
        tail_positions = [
            current.find(marker)
            for marker in _CURRENT_SEGMENT_INTERNAL_TAIL_MARKERS
            if current.find(marker) > 0
        ]
        if tail_positions:
            current = current[:min(tail_positions)]
    return bool(_READ_ONLY_WRITE_FORBID_RE.search(current[-3000:]))


def _latest_current_user_segment(messages: list[dict], *, max_chars: int = 6000) -> str:
    latest_user = ""
    for msg in reversed(messages or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = str(msg.get("content") or "")
        if any(marker in text for marker in _CURRENT_MESSAGE_MARKERS):
            latest_user = text
            break
        if text.lstrip().startswith(_INTERNAL_DYNAMIC_USER_PREFIXES):
            continue
        latest_user = text
        break
    if not latest_user:
        return ""
    current = latest_user
    marker_positions = [
        latest_user.rfind(marker)
        for marker in _CURRENT_MESSAGE_MARKERS
        if latest_user.rfind(marker) >= 0
    ]
    if marker_positions:
        current = latest_user[max(marker_positions):]
        tail_positions = [
            current.find(marker)
            for marker in _CURRENT_SEGMENT_INTERNAL_TAIL_MARKERS
            if current.find(marker) > 0
        ]
        if tail_positions:
            current = current[:min(tail_positions)]
    return current[-max_chars:]


def _active_task_explicitly_requires_browser_repro(messages: list[dict]) -> bool:
    """Detect explicit current-task browser/host-browser reproduction requirements."""
    current = _latest_current_user_segment(messages)
    if not current:
        return False
    if _BROWSER_REPRO_REQUEST_RE.search(current):
        return True
    try:
        snapshot = _current_task_plan_focus_snapshot(max_chars=2400)
    except Exception:
        snapshot = ""
    return bool(snapshot and _BROWSER_REPRO_REQUEST_RE.search(snapshot))


def _active_task_requires_browser_pre_edit_evidence(messages: list[dict]) -> bool:
    """Detect explicit browser evidence that is ordered before edits/fixes."""
    current = _latest_current_user_segment(messages)
    if current and _BROWSER_PRE_EDIT_REQUEST_RE.search(current):
        return True
    try:
        snapshot = _current_task_plan_focus_snapshot(max_chars=2400)
    except Exception:
        snapshot = ""
    return bool(snapshot and _BROWSER_PRE_EDIT_REQUEST_RE.search(snapshot))


def _active_task_has_browser_evidence_fact(messages: list[dict]) -> bool:
    """Detect already supplied browser-family evidence facts, not requests."""
    parts: list[str] = []
    current = _latest_current_user_segment(messages, max_chars=9000)
    if current:
        parts.append(current)
    try:
        snapshot = _current_task_plan_focus_snapshot(max_chars=4000)
    except Exception:
        snapshot = ""
    if snapshot:
        parts.append(snapshot)
    text = "\n".join(parts)
    if not text:
        return False
    return bool(_BROWSER_EVIDENCE_FACT_RE.search(text))


def _current_task_plan_focus_snapshot(max_chars: int = 2400) -> str:
    """Return the main-process maintained active-task focus, excluding historical contracts."""
    try:
        from app.core.core_processes import get_current_thread_context
        ctx = get_current_thread_context()
    except Exception:
        ctx = None
    if not ctx:
        return ""
    parts: list[str] = []
    user_message = str(getattr(ctx, "user_message", "") or "").strip()
    if user_message:
        parts.append("[current user request]\n" + user_message[:900])
    intent = str(getattr(ctx, "plan_intent", "") or "").strip()
    key_points = list(getattr(ctx, "plan_key_points", None) or [])
    deliverables = list(getattr(ctx, "plan_deliverables", None) or [])
    if intent:
        parts.append("[current plan intent]\n" + intent[:600])
    if key_points:
        parts.append("[current plan key_points]\n" + json.dumps(key_points[:10], ensure_ascii=False))
    if deliverables:
        parts.append("[current plan deliverables]\n" + json.dumps(deliverables[:8], ensure_ascii=False))
    text = "\n".join(parts).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 80] + "\n[current task plan focus summarized by length]"
    return text


def _tool_call_is_pre_edit_mutation(tool_name: str, args: dict) -> bool:
    """Return true for source/workspace mutations that can invalidate baseline evidence."""
    name = str(tool_name or "").strip()
    if name in {"edit_file", "insert_in_file", "multi_edit", "write_file"}:
        return True
    if name == "workspace":
        action = str((args or {}).get("action") or "").strip().lower()
        path = str((args or {}).get("path") or "").replace("\\", "/").strip().lower()
        if action in {"write", "mkdir"}:
            projectish_ext = (
                ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".scss",
                ".json", ".toml", ".yaml", ".yml", ".md", ".c", ".cpp", ".h",
                ".java", ".go", ".rs", ".php", ".rb", ".swift", ".kt",
            )
            return path.startswith("_env/") or path.endswith(projectish_ext)
        if action == "run":
            return _project_write_path_from_command(_main_verifier_command_text(name, args)) is not None
    if name in {"env_run", "bash"}:
        return _project_write_path_from_command(_main_verifier_command_text(name, args)) is not None
    if name in {"env_apply_create", "env_apply_replace"}:
        return True
    if name == "office":
        action = str((args or {}).get("action") or "").strip().lower()
        return action in {"write", "append", "create", "insert_image", "edit", "save"}
    return False


def _delegate_tasks_for_boundary(args: dict) -> list[dict]:
    """Return delegate task specs, including common single-task top-level shape."""
    if not isinstance(args, dict):
        return []
    tasks = args.get("tasks") or []
    if isinstance(tasks, list) and tasks:
        return [task for task in tasks if isinstance(task, dict)]
    top_level = {
        key: args.get(key)
        for key in (
            "task_id",
            "prompt",
            "kind",
            "mode",
            "framework",
            "dispatch_reason",
            "input_files",
            "expected_outputs",
            "acceptance_checks",
        )
        if args.get(key) not in (None, "", [], {})
    }
    return [top_level] if top_level else []


def _delegate_call_is_pre_edit_mutation(args: dict) -> bool:
    """Return true when a delegate call is likely to author/edit deliverables."""
    if not isinstance(args, dict):
        return False
    action = str(args.get("action") or "").strip().lower()
    if action not in {"spawn", "spawn_async", ""}:
        return False
    tasks = _delegate_tasks_for_boundary(args)
    if not tasks:
        return False
    authoring_kinds = {"code", "edit", "draw", "tts"}
    authoring_verbs = re.compile(
        r"(?is)\b(patch|fix|repair|edit|modify|update|write|create|generate|build|implement|save)\b"
        r"|(?:修复|修改|编辑|更新|编写|创建|生成|实现|保存)"
    )
    source_or_artifact_exts = _ENV_RUN_PROJECT_WRITE_EXTS + (
        ".docx", ".pptx", ".xlsx", ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".mp3", ".wav",
    )
    for task in tasks:
        if not isinstance(task, dict):
            continue
        kind = str(task.get("kind") or "").strip().lower()
        prompt = str(task.get("prompt") or "")
        expected = task.get("expected_outputs") or []
        if kind in authoring_kinds:
            return True
        if isinstance(expected, list):
            for item in expected:
                norm = str(item or "").replace("\\", "/").strip().lower()
                if norm and norm.endswith(source_or_artifact_exts):
                    return True
        if authoring_verbs.search(prompt):
            return True
    return False


def _delegate_call_declares_browser_pre_edit_boundary(args: dict) -> bool:
    """Detect helper contracts that explicitly carry browser evidence before edits."""
    tasks = _delegate_tasks_for_boundary(args)
    if not tasks:
        return False
    browser_token = re.compile(
        r"(?is)\b(browser|host[-\s]?browser|playwright|puppeteer|selenium|chromium|chrome|firefox|webkit)\b"
        r"|(?:浏览器|宿主浏览器)"
    )
    evidence_token = re.compile(
        r"(?is)\b(evidence|browse|open|visit|navigate|load|observe|inspect|screenshot|confirm|verify)\b"
        r"|(?:证据|浏览|打开|访问|加载|观察|检查|截图|确认|验证)"
    )
    order_token = re.compile(
        r"(?is)\b(before|prior\s+to|first|pre[-\s]?(?:change|edit|fix)|then|only\s+then)\b"
        r"|(?:先|之前|前置|再|然后)"
    )
    for task in tasks:
        if not isinstance(task, dict):
            continue
        parts: list[str] = []
        for key in ("prompt", "framework", "dispatch_reason"):
            value = task.get(key)
            if isinstance(value, (dict, list)):
                parts.append(json.dumps(value, ensure_ascii=False))
            else:
                parts.append(str(value or ""))
        for key in ("acceptance_checks", "input_files", "expected_outputs"):
            value = task.get(key)
            if isinstance(value, (dict, list)):
                parts.append(json.dumps(value, ensure_ascii=False))
        text = "\n".join(parts)
        if browser_token.search(text) and evidence_token.search(text) and order_token.search(text):
            return True
    return False


def _attach_browser_pre_edit_fact_to_delegate(args: dict, *, warning_count: int) -> bool:
    """Attach a factual browser-evidence boundary to delegate tasks without blocking."""
    if not isinstance(args, dict):
        return False
    tasks = args.get("tasks")
    top_level = False
    if isinstance(tasks, list) and tasks:
        target_tasks = [task for task in tasks if isinstance(task, dict)]
    else:
        target_tasks = [args]
        top_level = True
    if not target_tasks:
        return False
    fact = (
        "Runtime fact: the active task asks for browser/host-browser evidence before edits, "
        "and the current tool loop has not yet seen browser/Playwright/Selenium/Puppeteer evidence. "
        "Source reads, static diagnosis, and plain docs-file reads are different evidence. "
        "This is an active-task evidence requirement, not an optional enhancement. Decide inside this helper "
        "boundary how to collect browser automation/screenshot/page-observation evidence before editing, or "
        "report the concrete blocker/evidence boundary before editing instead of treating source/docs/HTTP reads "
        "as that evidence. If this helper lacks browser-family tools that the main process may have, return that "
        "fact so the main process can collect the narrow browser evidence and resume or apply from compact facts."
    )
    changed = False
    for task in target_tasks:
        existing_reason = str(task.get("dispatch_reason") or "").strip()
        if fact not in existing_reason:
            task["dispatch_reason"] = (existing_reason + "\n\n" + fact).strip() if existing_reason else fact
            changed = True
        checks = task.get("acceptance_checks")
        if not isinstance(checks, list):
            checks = []
        check_fact = (
            "Satisfy the active browser/host-browser evidence requirement before artifact edits, using "
            "browser automation/screenshot/page observation; if infeasible, report the exact blocker/evidence boundary."
        )
        if check_fact not in [str(item) for item in checks]:
            task["acceptance_checks"] = [*checks, check_fact]
            changed = True
    args["_browser_pre_edit_fact_attached"] = True
    args["_browser_pre_edit_fact_warning_count"] = warning_count
    if top_level:
        args["_browser_pre_edit_fact_top_level_task"] = True
    return changed


def _browser_pre_edit_missing_fact_payload(
    *,
    tool_name: str,
    path: str,
    warning_count: int,
    pre_edit_required: bool,
) -> dict:
    return {
        "warning": "browser_reproduction_evidence_missing_before_edit",
        "blocked_once": False,
        "blocked_until_browser_evidence": False,
        "warning_count": warning_count,
        "tool_name": tool_name,
        "path": path,
        "pre_edit_required_by_active_task": bool(pre_edit_required),
        "fact": (
            "The active task explicitly asks for browser/host-browser reproduction or verification, "
            "and this is an edit-like tool call before any browser/Playwright/Selenium/Puppeteer evidence "
            "has appeared in the current tool loop. Source reads, static diagnosis, and plain HTTP checks "
            "are not the same evidence. Browser evidence can come from an actual browser tool or from "
            "a command that runs Playwright, Puppeteer, Selenium, Chromium, Chrome, Firefox, or WebKit "
            "against the target URL. The tool call was allowed to execute; use this fact when deciding "
            "whether the active contract still needs browser evidence or a follow-up "
            "verification step."
        ),
        "事实": (
            "当前任务显式要求浏览器/宿主浏览器复现或验证；本次是编辑类调用，"
            "但当前工具链尚未出现 browser/Playwright/Selenium/Puppeteer 证据。"
            "源码阅读、静态诊断、普通 HTTP 检查不等同于该证据。浏览器证据可来自实际浏览器工具，"
            "或运行 Playwright/Puppeteer/Selenium/Chromium/Chrome/Firefox/WebKit 访问目标 URL 的命令。"
            "本次工具调用已放行；请据此判断当前契约是否仍需浏览器证据或后续验证。"
        ),
    }


def _browser_pre_edit_predecision_guidance(*, iteration: int) -> str:
    """Return a one-step factual reminder before the model chooses mutation tools."""
    return (
        "[SYSTEM_HINT/browser_pre_edit_evidence_boundary]\n"
        f"Iteration {iteration}: the active task asks for browser/host-browser evidence before edits, "
        "and the current tool loop has no browser-family evidence yet. Browser-family evidence means an "
        "actual browser tool result or a command using Playwright, Puppeteer, Selenium, Chromium, Chrome, "
        "Firefox, or WebKit against the target. Source reads, static diagnosis, docs-file reads, curl/plain "
        "HTTP checks, and final text claims are different evidence. If the target URL/path is already known, "
        "a narrow browser probe is a routing/evidence fact that can be collected before edits; the resulting "
        "compact browser facts can then be used by the next implementation or verification step. "
        "This is a fact checkpoint, not a forced decision: decide whether to gather browser evidence now, "
        "choose a browser-capable route, or record the concrete infeasible "
        "evidence boundary before browser-dependent edits/apply/deliverable writes.\n\n"
        "当前任务要求浏览器/宿主浏览器证据先于编辑；当前工具链尚无 browser-family 证据。源码读取、静态诊断、文档读取、curl/普通 HTTP 和文字声明不等同；URL/路径已知时，可先收集窄浏览器证据事实，供后续实现或验证步骤使用。"
    )


def _main_source_edit_delegation_fact_payload(
    *,
    tool_name: str,
    path: str,
) -> dict:
    return {
        "warning": "main_source_edit_should_delegate",
        "blocked_once": False,
        "tool_name": tool_name,
        "path": path,
        "fact": (
            "This main-process tool call authored or modified a source/project-like file. "
            "The current workflow expects source/project authoring and verification to stay at the "
            "implementation boundary, while the coordinator owns task planning, work coordination, diff/apply, "
            "deliverable mapping, and acceptance accounting. No staged project output "
            "for this path had appeared in the current tool loop before this call. The tool call was "
            "allowed to execute; use this fact when deciding whether to keep the direct edit, request "
            "verification, or route follow-up implementation work."
        ),
        "事实": (
            "本次主进程调用编写或修改了源码/项目类文件；当前流程期望源码/项目编写和验证留在实现边界，"
            "协调侧负责计划、派发、diff/apply、交付映射和验收记账。调用前当前工具链尚未出现该路径的暂存产物。"
            "本次调用已放行；请据此判断是否保留直接编辑、请求验证或路由后续实现工作。"
        ),
    }


def _merge_tool_result_facts(result: str, facts: list[dict]) -> str:
    """Attach runtime facts to a real tool result without changing execution."""
    clean_facts = [fact for fact in facts if isinstance(fact, dict) and fact]
    if not clean_facts:
        return result
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        existing = parsed.get("runtime_facts")
        merged = list(existing) if isinstance(existing, list) else []
        merged.extend(clean_facts)
        parsed["runtime_facts"] = merged
        warnings = list(parsed.get("warnings")) if isinstance(parsed.get("warnings"), list) else []
        for fact in clean_facts:
            warning = str(fact.get("warning") or "").strip()
            if warning and warning not in warnings:
                warnings.append(warning)
        if warnings:
            parsed["warnings"] = warnings
        return json.dumps(parsed, ensure_ascii=False)
    return json.dumps({
        "ok": True,
        "content": result,
        "runtime_facts": clean_facts,
        "warnings": [
            str(fact.get("warning") or "").strip()
            for fact in clean_facts
            if str(fact.get("warning") or "").strip()
        ],
    }, ensure_ascii=False)


def _main_source_edit_path(tool_name: str, args: dict) -> str | None:
    """Return the edited project/source-like path when main should prefer helper authorship."""
    name = str(tool_name or "").strip()
    if name in {"edit_file", "insert_in_file", "multi_edit", "write_file"}:
        path = str((args or {}).get("path") or "").replace("\\", "/").strip().lstrip("./")
    elif name == "workspace" and str((args or {}).get("action") or "").strip().lower() == "write":
        path = str((args or {}).get("path") or "").replace("\\", "/").strip().lstrip("./")
    elif name == "workspace" and str((args or {}).get("action") or "").strip().lower() == "run":
        path = _project_write_path_from_command(_main_verifier_command_text(name, args)) or ""
    elif name in {"env_run", "bash"}:
        path = _project_write_path_from_command(_main_verifier_command_text(name, args)) or ""
    else:
        return None
    if not path:
        return None
    low = path.lower()
    project_path = low[5:] if low.startswith("_env/") else low
    accepted_exts = _ENV_RUN_PROJECT_WRITE_EXTS if name in {"env_run", "bash"} else _SOURCE_OR_PROJECT_EXTS
    if low.startswith("_env/") and project_path.endswith(accepted_exts):
        return path
    if project_path.endswith(accepted_exts):
        return path
    return None


def _delegate_result_staged_paths(result: str | None) -> set[str]:
    """Extract helper-produced staged project paths from delegate result facts."""
    if not result:
        return set()
    try:
        parsed = json.loads(result)
    except Exception:
        return set()
    if isinstance(parsed, dict) and isinstance(parsed.get("content"), str):
        try:
            inner = json.loads(parsed["content"])
            if isinstance(inner, dict):
                parsed = inner
        except Exception:
            pass
    if not isinstance(parsed, dict):
        return set()
    paths: set[str] = set()

    def add(value) -> None:
        if isinstance(value, str):
            norm = value.replace("\\", "/").strip().lstrip("./")
            if norm:
                paths.add(norm)
                if norm.startswith("_env/"):
                    paths.add(norm[len("_env/"):])
        elif isinstance(value, list):
            for item in value:
                add(item)
        elif isinstance(value, dict):
            for key in ("env_copied_files", "copied_files", "copied_project_files"):
                add(value.get(key))

    for key in ("main_available_files", "staged_project_files", "copied_project_files"):
        add(parsed.get(key))
    add(parsed.get("copy_stats"))
    for item in parsed.get("result_items") or []:
        if isinstance(item, dict):
            for key in ("main_available_files", "staged_project_files", "copied_project_files"):
                add(item.get(key))
            add(item.get("copy_stats"))
    return paths


def _delegate_result_helper_owned_paths(result: str | None) -> set[str]:
    """Extract paths whose content ownership remains with a clean helper result.

    These paths are safe for main-thread apply ownership tracking only when the
    helper's own output contract says the producer boundary was clean.
    """
    if not result:
        return set()
    try:
        parsed = json.loads(result)
    except Exception:
        return set()
    if isinstance(parsed, dict) and isinstance(parsed.get("content"), str):
        try:
            inner = json.loads(parsed["content"])
            if isinstance(inner, dict):
                parsed = inner
        except Exception:
            pass
    if not isinstance(parsed, dict):
        return set()

    paths: set[str] = set()

    def add(value) -> None:
        if isinstance(value, str):
            norm = value.replace("\\", "/").strip().lstrip("./")
            if norm:
                paths.add(norm)
                if norm.startswith("_env/"):
                    paths.add(norm[len("_env/"):])
        elif isinstance(value, list):
            for item in value:
                add(item)
        elif isinstance(value, dict):
            for key in ("env_copied_files", "copied_files", "copied_project_files"):
                add(value.get(key))

    def item_is_clean_helper_owned(item: dict) -> bool:
        if _delegate_item_is_incomplete(item):
            return False
        outputs_check = item.get("outputs_check")
        if isinstance(outputs_check, dict):
            if outputs_check.get("producer_self_verified") is True:
                return True
            if outputs_check.get("producer_boundary_fact") and outputs_check.get("outputs_complete") is True:
                return True
        return False

    def add_item(item: dict) -> None:
        if not item_is_clean_helper_owned(item):
            return
        for key in ("main_available_files", "workspace_files", "files", "staged_project_files", "copied_project_files"):
            add(item.get(key))
        add(item.get("copy_stats"))
        outputs_check = item.get("outputs_check")
        if isinstance(outputs_check, dict):
            for key in ("matched_files", "delivered_files"):
                add(outputs_check.get(key))

    for item in _delegate_items_from_result(parsed):
        add_item(item)
    for item in parsed.get("result_items") or []:
        if isinstance(item, dict):
            add_item(item)
    if not paths and item_is_clean_helper_owned(parsed):
        add_item(parsed)
    return paths


def _norm_project_path_fact(value: object) -> str:
    norm = str(value or "").replace("\\", "/").strip().lstrip("./")
    if norm.startswith("_env/"):
        norm = norm[len("_env/"):]
    return norm


def _env_apply_path_from_result(parsed_result: dict, args: dict) -> str:
    for value in (
        parsed_result.get("path"),
        args.get("path") if isinstance(args, dict) else "",
    ):
        norm = _norm_project_path_fact(value)
        if norm:
            return norm
    return ""


def _env_apply_uses_helper_staged_source(parsed_result: dict, args: dict, helper_paths: set[str]) -> bool:
    """Return true when the apply only transfers a clean helper-staged file."""
    if not helper_paths:
        return False
    source = str(args.get("workspace_path") or "").replace("\\", "/").strip().lstrip("./") if isinstance(args, dict) else ""
    target = _env_apply_path_from_result(parsed_result, args)
    candidates = {source, _norm_project_path_fact(source), target}
    return any(path and path in helper_paths for path in candidates)


def _tool_result_is_browser_repro_evidence(tool_name: str, args: dict, result: str | None = None) -> bool:
    """Recognize browser-family evidence without treating ordinary HTTP/source reads as browser evidence."""
    name = str(tool_name or "").strip().lower()
    if name in _BROWSER_EVIDENCE_TOOL_NAMES or name.startswith("browser_"):
        return True
    text_parts = [json.dumps(args or {}, ensure_ascii=False)]
    if result:
        text_parts.append(str(result)[:5000])
    text = "\n".join(text_parts)
    low = text.lower()
    command = ""
    if name in {"bash", "bash_execute", "env_run", "workspace"}:
        command = _main_verifier_command_text(name, args)
    if command and _BROWSER_EVIDENCE_COMMAND_RE.search(command):
        return True
    if command and result and _BROWSER_EVIDENCE_COMMAND_RE.search(str(result)[:5000]):
        return True
    if command and result and "_family_evidence" in low and "browser" in low:
        return True
    if name == "delegate" and result:
        try:
            parsed = json.loads(result) if isinstance(result, str) else result
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            summary = _delegate_workflow_result_summary(parsed)
            if summary and summary.get("browser_evidence_facts"):
                return True
    return False


def _retryable_delegate_facts_from_result(result) -> list[dict]:
    """Extract delegate failures that should not be finalized without recovery."""
    facts: list[dict] = []
    for item in _delegate_items_from_result(result):
        if _delegate_item_is_race_lost(item):
            continue
        if _delegate_item_is_nonblocking_readonly_evidence(item):
            continue
        if _delegate_item_is_read_no_evidence_loop(item):
            continue
        if _delegate_item_is_terminal_tts_generation_fact(item):
            continue
        if not _delegate_item_is_incomplete(item):
            continue
        next_action = item.get("next_action")
        if not isinstance(next_action, dict):
            next_action = {}
        action_type = str(next_action.get("type") or "")
        terminal_reason = str(item.get("terminal_reason") or "").lower()
        if action_type and action_type not in _RETRYABLE_DELEGATE_NEXT_ACTIONS:
            continue
        if terminal_reason and terminal_reason not in _RECOVERABLE_DELEGATE_REASONS:
            continue
        if not terminal_reason:
            if item.get("resource_required") or item.get("needs_resource"):
                terminal_reason = "resource_required"
            elif item.get("stuck"):
                terminal_reason = "stuck"
            elif item.get("interrupted"):
                terminal_reason = "interrupted"
            elif item.get("quality_blocked"):
                terminal_reason = "quality_blocked"
            elif item.get("error") or item.get("error_kind") or item.get("ok") is False:
                terminal_reason = "failed"
            elif not _delegate_item_outputs_complete(item):
                terminal_reason = "outputs_missing"
            else:
                terminal_reason = "failed"
        if terminal_reason not in _RECOVERABLE_DELEGATE_REASONS:
            continue
        params = next_action.get("params")
        if not isinstance(params, dict):
            params = {}
        task_id = str(item.get("task_id") or params.get("task_id") or "?")
        kind = item.get("suggested_retry_kind") or item.get("kind") or params.get("kind")
        mode = item.get("suggested_retry_mode") or params.get("mode") or item.get("mode")
        if not params and task_id != "?":
            retry_kind = str(kind or "code").lower()
            retry_mode = "hard" if terminal_reason in {"stuck", "failed", "error", "crashed"} else (mode or "easy")
            if terminal_reason == "output_format_invalid" or action_type == "resume_same_task_fix_output_format":
                retry_prompt = (
                    "Resume the same helper task from the preserved workspace, keeping the existing task identity instead of creating a v2 task. "
                    "Inspect existing files only if needed, then repair the final report format so it contains "
                    "`## Output files` followed by a fenced JSON block with the exact existing relative paths. "
                    "If a declared file is missing, report the missing path honestly instead of claiming completion."
                    "\n\n续作同一个 helper 任务并修正输出格式；保留 task_id，列出真实存在的相对路径。"
                )
                retry_mode = mode or "easy"
                action_type = "resume_same_task_fix_output_format"
            else:
                retry_prompt = (
                    "Continue the same task from the preserved workspace. Review the previous report, current files, "
                    "and latest failure evidence; complete the missing deliverables, run the appropriate verification, "
                    "and report only verified outcomes."
                    "\n\n从保留工作区继续同一任务，补齐缺失产物并只报告已验证结果。"
                )
            if retry_kind in {"read", "ocr"} and action_type != "resume_same_task_fix_output_format":
                retry_prompt = (
                    "Continue the same source-reading task from the preserved workspace. First locate the exact "
                    "source files or directories, using directory/search tools instead of reading a directory as a "
                    "file. Read text and visual/OCR streams as needed, save a compact evidence .txt, and report "
                    "VERDICT, source_files, coverage_summary, unread spans, line_ranges, needs_escalation, and "
                    "next_action. If the same source path is unavailable, return a verified missing-resource report "
                    "with the searched paths instead of finalizing from partial evidence."
                    "\n\n继续同一阅读任务；先定位真实文件，分离文本和视觉证据，保存证据摘要，缺资源时报告搜索过的路径。"
                )
            params = {
                "action": "spawn",
                "task_id": task_id,
                "resume": True,
                "kind": retry_kind,
                "mode": retry_mode,
                "prompt": retry_prompt,
            }
            if not action_type:
                action_type = "resume_upgraded" if params.get("mode") == "hard" else "resume_after_crash"
        outputs_check = item.get("outputs_check")
        if not isinstance(outputs_check, dict):
            outputs_check = {}
        facts.append({
            "task_id": task_id,
            "terminal_reason": terminal_reason,
            "stuck_reason": str(item.get("stuck_reason") or ""),
            "next_action_type": action_type or "resume_after_failure",
            "rationale": str(next_action.get("rationale") or ""),
            "params": params,
            "outputs_missing": outputs_check.get("outputs_missing") or [],
            "outputs_complete": outputs_check.get("outputs_complete"),
            "quality_blocked": bool(outputs_check.get("quality_blocked") or item.get("quality_blocked")),
            "blocking_quality_warnings": outputs_check.get("blocking_quality_warnings") or [],
            "delivered_count": outputs_check.get("delivered_count"),
            "kind": kind,
            "mode": params.get("mode") or mode,
        })
    return facts


def _delegate_gap_facts_from_result(result) -> list[dict]:
    """Extract nonblocking helper gaps that should remain visible to final JSON."""
    facts: list[dict] = []
    for item in _delegate_items_from_result(result):
        if _delegate_item_is_race_lost(item):
            continue
        if _delegate_item_is_terminal_tts_generation_fact(item):
            resource = item.get("resource_required") if isinstance(item.get("resource_required"), dict) else {}
            outputs_check = item.get("outputs_check")
            if not isinstance(outputs_check, dict):
                outputs_check = {}
            task_id = str(item.get("task_id") or "?")
            facts.append({
                "task_id": task_id,
                "kind": item.get("kind") or item.get("helper_kind") or "tts",
                "terminal_reason": item.get("terminal_reason") or "resource_required",
                "gap_kind": "tts_generation_not_completed",
                "stuck_reason": str(
                    item.get("stuck_reason")
                    or item.get("error")
                    or item.get("report")
                    or resource.get("blocked_reason")
                    or ""
                )[:700],
                "outputs_missing": outputs_check.get("outputs_missing") or [],
                "outputs_complete": outputs_check.get("outputs_complete"),
                "nonblocking_tts_generation_fact": True,
                "note": (
                    "TTS generation/authorization failure is visible status evidence for the final plan, "
                    "but it should not force an automatic same-helper retry."
                ),
            })
            continue
        if not _delegate_item_is_read_no_evidence_loop(item):
            continue
        outputs_check = item.get("outputs_check")
        if not isinstance(outputs_check, dict):
            outputs_check = {}
        task_id = str(item.get("task_id") or "?")
        facts.append({
            "task_id": task_id,
            "kind": item.get("kind") or item.get("helper_kind"),
            "terminal_reason": item.get("terminal_reason") or "stuck",
            "gap_kind": "read_no_evidence_loop",
            "stuck_reason": str(item.get("stuck_reason") or item.get("report") or "")[:700],
            "outputs_missing": outputs_check.get("outputs_missing") or [],
            "outputs_complete": outputs_check.get("outputs_complete"),
            "note": (
                "Read helper stopped after many read-class calls without writing an evidence file. "
                "This is not an automatic same-kind retry requirement, but it is a helper coverage/quality fact."
            ),
        })
    return facts


def _delegate_spawn_task_ids(args) -> set[str]:
    """Return task ids that a delegate call is actively spawning/resuming."""
    if not isinstance(args, dict):
        return set()
    action = str(args.get("action") or "").lower()
    task_ids: set[str] = set()
    tasks = args.get("tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if isinstance(task, dict) and task.get("task_id"):
                task_ids.add(str(task.get("task_id")))
    if args.get("task_id") and action in ("spawn", "start", "resume"):
        task_ids.add(str(args.get("task_id")))
    if action == "spawn" or tasks:
        return task_ids
    return set()


def _committed_files_from_recent_tools(messages: list[dict], *, limit: int = 30) -> list[str]:
    """Return files successfully promoted by recent commit_to_main tool results.

    A committed file is acceptance evidence for the target deliverable. Pending
    helper recovery should not keep blocking finalization once the main thread
    has verified and promoted the requested artifact.

    已提交主区的文件是验收证据；目标产物完成后不应被旧 helper 续作强制阻塞。
    """
    files: list[str] = []
    for m in messages[-limit:]:
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        content = str(m.get("content", "") or "")
        if (
            "commit_to_main" not in content
            and '"promoted"' not in content
            and "committed_files" not in content
            and "env_apply_replace" not in content
            and "env_apply_create" not in content
        ):
            continue
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = None
        candidates: list = []
        if isinstance(parsed, dict):
            if parsed.get("action") == "commit_to_main" and parsed.get("ok") is False:
                continue
            action = str(parsed.get("action") or "").lower()
            if action in {"env_apply_replace", "env_apply_create"} and parsed.get("ok") is not False:
                if parsed.get("path"):
                    candidates.append(parsed.get("path"))
            for key in ("promoted", "committed_files"):
                value = parsed.get(key)
                if isinstance(value, list):
                    candidates.extend(value)
        else:
            for match in re.finditer(r'"(?:promoted|committed_files)":\s*\[([^\]]+)\]', content):
                candidates.extend(m.group(1) for m in re.finditer(r'"([^"]+)"', match.group(1)))
        for item in candidates:
            fn = str(item or "").split("/")[-1].split("\\")[-1].strip()
            if fn and fn not in files:
                files.append(fn)
    return files[:20]


def _unresolved_project_write_block_from_result(tool_name: str, result: str | dict | None) -> dict | None:
    if str(tool_name or "") != "workspace":
        return None
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except Exception:
        parsed = None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("blocked_reason") != "environment_workspace_write_not_project_file":
        return None
    path = str(parsed.get("blocked_path") or parsed.get("path") or "").strip()
    if not path:
        return None
    suggested_args: dict = {}
    suggested_tool = ""
    recovery = parsed.get("recovery_facts")
    if isinstance(recovery, dict):
        recovery_args = recovery.get("arguments")
        if not isinstance(suggested_args, dict) or not suggested_args:
            if isinstance(recovery_args, dict):
                suggested_args = recovery_args
            elif recovery.get("matching_tool_shape") == "env_apply_create":
                suggested_args = {
                    "path": path,
                    "content": recovery.get("attempted_content") or recovery.get("content") or "",
                }
        if not suggested_tool:
            suggested_tool = str(recovery.get("matching_tool_shape") or recovery.get("matching_tool") or "")
    return {
        "path": path,
        "tool_name": "workspace",
        "blocked_reason": "environment_workspace_write_not_project_file",
        "project_file_created": False,
        "matching_tool_shape": suggested_tool or "env_apply_create",
        "observed_recovery_shape": {
            "tool": suggested_tool or "env_apply_create",
            "arguments": suggested_args if isinstance(suggested_args, dict) else {},
        },
        "suggested_tool": suggested_tool or "env_apply_create",
        "suggested_arguments": suggested_args if isinstance(suggested_args, dict) else {},
        "fact": (
            f"workspace.write did not create project file `{path}`. "
            "If that attempted content is still the desired project deliverable, env_apply_create is the matching real-project create shape; "
            "otherwise record why no project file is required."
        ),
    }


def _resolved_project_write_path_from_result(tool_name: str, result: str | dict | None) -> str | None:
    if str(tool_name or "") != "env_apply_create":
        return None
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except Exception:
        parsed = None
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        return None
    path = str(parsed.get("path") or "").strip()
    return path or None


_DELIVERABLE_EXT_RE = re.compile(
    r"\.(?:c|cpp|h|hpp|py|md|txt|json|docx|pptx|xlsx|pdf|png|jpg|jpeg|gif|webp|bmp|svg|html|css|js|ts|zip|csv|wav|mp3|ogg|m4a)$",
    re.IGNORECASE,
)


def _looks_like_internal_deliverable_name(name: str) -> bool:
    if not name:
        return True
    norm = str(name or "").replace("\\", "/").strip()
    base = norm.split("/")[-1].lower()
    if norm.startswith(("_helpers_shared/", "_shared/")):
        return True
    if base.startswith(".") or base.startswith("helper_"):
        return True
    if re.match(r"^_py_cmd_[0-9a-f]{6,}\.py$", base):
        return True
    return False


def _deliverable_basename(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip().strip("`'\"")
    if not text or not _DELIVERABLE_EXT_RE.search(text):
        return ""
    if _looks_like_internal_deliverable_name(text):
        return ""
    return text.replace("\\", "/").split("/")[-1]


def _delegate_verified_files_from_result(result) -> list[str]:
    files: list[str] = []
    for item in _delegate_items_from_result(result):
        if _delegate_item_is_incomplete(item):
            continue
        values: list[object] = []
        for key in ("main_available_files", "workspace_files", "files"):
            value = item.get(key)
            if isinstance(value, list):
                values.extend(value)
        outputs_check = item.get("outputs_check")
        if isinstance(outputs_check, dict):
            for key in ("matched_files", "delivered_files"):
                value = outputs_check.get(key)
                if isinstance(value, list):
                    values.extend(value)
        file_map = item.get("file_map")
        if isinstance(file_map, list):
            for entry in file_map:
                if isinstance(entry, dict):
                    values.append(entry.get("main_name"))
        for value in values:
            normalized_value = value
            if isinstance(value, str):
                norm = value.replace("\\", "/").strip()
                if norm.startswith("_env/"):
                    normalized_value = norm[len("_env/"):]
            bn = _deliverable_basename(normalized_value)
            if bn and bn not in files:
                files.append(bn)
    return files


def _delegate_completion_checkpoint_from_result(result) -> dict | None:
    """Extract compact completion evidence from a clean delegate result.

    This is a model-visible convergence fact, not a hidden finalization rule.
    It deliberately avoids suppressing warnings or deciding whether the LLM
    should perform another targeted verification.
    """
    files: list[str] = []
    facts: list[str] = []
    staged_project_files: list[str] = []
    warning_count = 0
    completed_items = 0
    for item in _delegate_items_from_result(result):
        if _delegate_item_is_incomplete(item):
            continue
        completed_items += 1
        for key in ("staged_project_files", "main_available_files", "workspace_files", "files"):
            value = item.get(key)
            if not isinstance(value, list):
                continue
            for path in value:
                norm = str(path or "").replace("\\", "/").strip()
                if norm.startswith("_env/") and norm not in staged_project_files:
                    staged_project_files.append(norm)
        for value in _delegate_verified_files_from_result({"results": [item]}):
            if value and value not in files:
                files.append(value)
        outputs_check = item.get("outputs_check")
        if isinstance(outputs_check, dict):
            warnings = outputs_check.get("quality_warnings") or []
            if isinstance(warnings, list):
                warning_count += len(warnings)
                for warning in warnings[:4]:
                    if not isinstance(warning, dict):
                        continue
                    issue = str(warning.get("issue") or "").strip()
                    file_name = str(warning.get("file") or "").strip()
                    details = str(warning.get("details") or warning.get("message") or "").strip()
                    if issue or details:
                        facts.append(
                            "quality_warning="
                            + json.dumps(
                                {
                                    "issue": issue[:120],
                                    "file": file_name[:160],
                                    "details": details[:260],
                                },
                                ensure_ascii=False,
                            )
                        )
            delivered = outputs_check.get("delivered_count")
            if delivered is not None:
                facts.append(f"delivered_count={delivered}")
            if outputs_check.get("outputs_complete") is True:
                facts.append("outputs_complete=true")
            if outputs_check.get("producer_self_verified") is True:
                facts.append("producer_self_verified=true")
        report_text = str(item.get("summary") or item.get("report") or item.get("content") or "")
        if "recommend: no" in report_text.lower():
            facts.append("available evidence includes recommend: no")
        if "Output files" in report_text or '"files"' in report_text:
            facts.append("available evidence includes Output files")
    if staged_project_files:
        facts.append(
            "staged_project_files_not_real_project_apply="
            + json.dumps(staged_project_files[:8], ensure_ascii=False)
        )
    if completed_items <= 0:
        return None
    return {
        "files": files[:8],
        "staged_project_files": staged_project_files[:8],
        "facts": list(dict.fromkeys(facts))[:6],
        "warning_count": warning_count,
        "completed_items": completed_items,
    }


def _recent_verified_files_from_tools(messages: list[dict], *, limit: int = 30) -> list[str]:
    """Collect current verified file evidence for forced-finalize summaries.

    Failed/interrupted helper reports may mention intended paths. Those are
    recovery facts, not deliverables. This function only promotes files from
    successful delegate items or explicit commit_to_main results.
    """
    files: list[str] = []
    for value in _committed_files_from_recent_tools(messages, limit=limit):
        bn = _deliverable_basename(value)
        if bn and bn not in files:
            files.append(bn)
    for m in messages[-limit:]:
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        content = str(m.get("content", "") or "")
        for value in _delegate_verified_files_from_result(content):
            bn = _deliverable_basename(value)
            if bn and bn not in files:
                files.append(bn)
    return files[:20]


def _normalize_committed_filename(value: object) -> str:
    return str(value or "").split("/")[-1].split("\\")[-1].strip().lower()


def _pending_retry_tasks_blocking_finalize(
    pending_retry_tasks: list[str],
    retry_facts: list[dict],
    committed_files: list[str],
) -> list[str]:
    """Return pending helper retry tasks that still block finalization.

    A committed deliverable may clear a retry only when it directly covers every
    explicit missing output for that helper. A broad final artifact must not hide
    an interrupted or failed helper whose required evidence files are still
    absent.

    已提交文件只有直接覆盖 helper 缺失产物时才解除阻塞；最终报告不能掩盖失败分支。
    """
    if not pending_retry_tasks:
        return []
    committed = {
        _normalize_committed_filename(item)
        for item in committed_files
        if _normalize_committed_filename(item)
    }
    latest_by_task: dict[str, dict] = {}
    for fact in retry_facts:
        tid = str((fact or {}).get("task_id") or "").strip()
        if tid:
            latest_by_task[tid] = fact

    blocking: list[str] = []
    for tid in pending_retry_tasks:
        fact = latest_by_task.get(tid) or {}
        if fact.get("nonblocking_readonly_evidence"):
            continue
        if fact.get("nonblocking_tts_generation_fact"):
            continue
        missing = [
            _normalize_committed_filename(item)
            for item in (fact.get("outputs_missing") or [])
            if _normalize_committed_filename(item)
        ]
        if missing:
            if all(item in committed for item in missing):
                continue
            blocking.append(tid)
            continue
        terminal = str(fact.get("terminal_reason") or "").strip().lower()
        if terminal in {
            "failed",
            "error",
            "crashed",
            "interrupted",
            "stuck",
            "timeout",
            "resource_required",
            "quality_blocked",
            "outputs_missing",
        }:
            if committed:
                continue
            blocking.append(tid)
            continue
        if fact.get("outputs_complete") is False:
            if committed:
                continue
            blocking.append(tid)
    return blocking


def _collector_has_streamed_tool_call(collector) -> bool:
    """Return whether a timed-out stream had started any tool call."""
    try:
        return bool(getattr(collector, "tool_calls", None))
    except Exception:
        return False


def _collector_has_continuable_text(collector) -> bool:
    """Return whether a timed-out stream has text/reasoning worth continuing."""
    try:
        return bool(getattr(collector, "content", None) or getattr(collector, "reasoning_content", None))
    except Exception:
        return False


def _collector_has_named_tool_call(collector) -> bool:
    """Return whether a timed-out stream already identified a tool to call."""
    try:
        for tc in (getattr(collector, "tool_calls", None) or {}).values():
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            if str(fn.get("name") or "").strip():
                return True
    except Exception:
        return False
    return False


async def chat_with_tools_loop(
    messages: list[dict],
    tools: list[dict],
    *,
    dispatcher,
    reasoning: str = "high",
    progress_cb=None,
    tool_result_cb=None,
    abort_event: asyncio.Event | None = None,
    lite: bool = False,
    upgrade_signal: dict | None = None,
    max_iter: int | None = None,
    finalize_kind: str = "json_plan",
    parallelizable: bool = False,
    reasoning_callback=None,  # 2026-05-03 v14:动态 reasoning / 模型切换
    task_id: str | None = None,  # 2026-05-03 v18.x:用于跨 helper 累计 timeout 计数
    helper_kind: str | None = None,  # 2026-05-04 v19.1:legacy hard-mode 路径不做 partial 续写
    chunk_callback=None,  # 2026-05-05: async cb() per stream chunk (API stall detection)
    stream_event_cb=None,  # 2026-05-09: cb("open"|"close", reason?) — 让调用方区分"stream 中"vs"工具派发中",stall 监控只对 stream 中计时,避免误抓 delegate(wait_window=600) 这种长合法等待
    model_spec=None,  # ModelSpec override（优先于 lite/reasoning）
    require_first_tool_call: bool = True,
) -> tuple[str, list[dict]]:
    """
    Round2 风格的工具调用循环（无限轮，靠用户打断或 LLM 自行停止）。

    progress_cb: 可选异步回调 (iteration, msgs, event_kind: str)。
        **拟人化反馈**——只在剧情节点触发,不是机械时间计数:
        - "stuck"       连续失败 ≥5 次且 ≥90s,该报告挫折
        - "breakthrough" 失败几次后突破成功,可以欢呼
        - "long_silence" 任务长但没说话,话痨人设可能想插一句
        progress_cb 内部用人设生成符合性格的话(也可决定不说)。
    tool_result_cb: 可选同步回调 (tool_name: str, result: str) → None。
        每个 tool 的 dispatcher 返回结果后立即调用一次,**注入时间戳之前的原始 result**。
        用于 macro signals 跟踪等场景:_round2 在 cb 内扫单条 delegate 结果取
        batch_timeout_majority,避免在 _iter_progress 里反复扫 msgs[-6:]。
        cb 失败要捕获不抛(用 try/except 包),否则会破坏 dispatch 流程。
    abort_event: 可选 asyncio.Event，set 后在下一轮迭代开始时截断工具链。
    upgrade_signal: 可选共享 dict，旁路 lite meta-judge 把"是否需要升级"写入。
        {"should_upgrade": bool, "reason": str, "evaluated_at_iter": int}
        主流程不读它（不打断），由外层 _round2 在循环结束后读取并写入 plan。
    max_iter: 可选迭代上限。helper(delegate)用以严控,主 round2 一般不传(走 HARD_ITER_CAP=60)。
        触达后走 forced finalize,不抛异常。
    finalize_kind: forced finalize(abort/cap)路径输出格式。两个选项:
        - "json_plan"  : 默认。Round2 主流程用,要求模型输出 ResponsePlan JSON。
        - "text_summary": helper 用。要求模型输出纯文本进度报告,不强制 JSON。
        正常退出(模型自然停手)路径**不受此参数影响**——模型按 system prompt 自然输出。
    """
    _sync_client_globals()

    # 2026-05-17 P150: 把 task_id 绑定到 office 工具的自适应 key,让本 helper 的
    # 失败收缩与其他 helper 隔离。ContextVar 在 asyncio task 内隔离,并行 helper
    # 各自独立。
    try:
        from app.llm.tools.office import set_office_adaptive_key
        set_office_adaptive_key(task_id)
    except Exception:
        pass
    if model_spec is not None:
        model = model_spec.model
        reasoning = model_spec.reasoning
        _cli_container = [_client_for_spec(model_spec)]
    else:
        model_spec = _legacy_model_spec(lite=lite, reasoning=reasoning)
        model = model_spec.model
        reasoning = model_spec.reasoning
        _cli_container = [_client_for_spec(model_spec)]
    debug.log(
        "llm.tools.start",
        f"model={model} reasoning={reasoning} tools={len(tools)} max_iter={max_iter}",
        {"messages": messages, "tool_names": [t["function"]["name"] for t in tools]},
    )
    _available_tool_names = _tool_names_from_schemas(tools)
    extra_body = _thinking_extra_body(reasoning, model_spec.provider if model_spec else None)
    _provider = model_spec.provider if model_spec else None  # captured for retry paths
    msgs = [m.copy() if isinstance(m, dict) else m for m in messages]
    _tool_result_spill_root = _infer_tool_result_spill_root(dispatcher)

    def _shape_suffix_tag(suffix: str = "") -> tuple[str, str]:
        clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(suffix or "").strip(". "))[:50]
        tag = _tools_loop_usage_tag(task_id)
        return (
            _tools_loop_shape_label(it, task_id, clean),
            f"{tag}.{clean}" if clean else tag,
        )

    def _log_nonstream_prompt_shape(
        *,
        suffix: str,
        call_messages: list[dict],
        call_tools: list[dict] | None = None,
    ) -> str:
        label, tag = _shape_suffix_tag(suffix)
        try:
            _log_prompt_cache_shape(
                label=label,
                model=model,
                messages=call_messages,
                tools=call_tools,
            )
        except Exception:
            pass
        return tag

    def _record_nonstream_response_usage(resp, *, tag: str) -> None:
        try:
            _record_response_usage(resp, model=model, tag=tag)
        except Exception:
            pass

    def _clear_transient_guidance_before_return(reason: str) -> None:
        _clear_transient_tool_loop_guidance(msgs, reason=reason)
        _clear_unused_schema_retry_guidance_after_response(
            [],
            trace_id=debug.current_trace_id(),
            reason=reason,
        )

    # 2026-05-04 v19:每次进入 chat_with_tools_loop 清零本 task 的累计 timeout 数。
    # 实测 trace 1fbb00b6 主线程刚启动就显示 cumulative_timeouts=2,因为
    # ContextVar 隔离: 入口处清零本 task 的累计,不影响其他并行 task。
    _get_timeout_dict().pop(task_id, None)

    # ── 拟人化反馈状态追踪 ──
    # 设计:本地状态机检测"剧情节点"(挫折/突破/长时间静默),
    # 触发时调 progress_cb 让 orchestrator 用人设生成自然语言反馈。
    # progress_cb 还可以根据人设决定不说话(安静的人设遇到平淡事件就保持沉默)。
    _start_time = time.monotonic()
    progress_state = _ToolProgressState(_start_time)

    # ── 硬上限：防真死循环耗光 API 配额 ──
    # 用户明确要求"放宽限制,工具调用没有上限,不要自动结束"。这里保留一个
    # 极宽的 9999 兜底护栏(防真死循环跑到无穷)而非常态结束门。
    # 2026-05-02 重构:helper 完全无时间硬墙(asyncio.wait_for 已撤),也无迭代上限,
    #   主进程通过 processes.list 看心跳决定是否 kill。9999 这道护栏纯粹防 LLM 出
    #   病态死循环(连续 9999 次工具调用无产出)。实测 hard 任务最高 ~80 iter,
    #   实际触达 9999 概率为 0,等同于无限。
    # 实际控制由 token budget watchdog 通过 _emergency_compact_msgs 间接做 —
    # context 永远不会爆,所以可以无限跑直到用户 abort / 协作中断 / LLM 自然停手。
    # ── 2026-05-02 part12:彻底移除轮数硬上限(用户要求"不要任何轮数限制")──
    # max_iter 参数保留(向后兼容)但不再 enforce 任何上限。
    # 真正的护栏是 token budget watchdog + abort_event + meta-judge 升级。
    HARD_ITER_CAP = None  # None = 无轮数上限
    effective_cap = max_iter  # caller 仍可传(目前已无 caller 传值,保留参数路径)

    # ── 2026-05-11 A1 核心改: token budget 大幅下调到检索绿区 ──
    # 之前的阈值 786K/838K/944K 来自"1M window × 75%~90%"假设,但 DeepSeek V4 官方
    # 数据:MRCR 8-needle 在 256K tokens 时 0.82,1M 时仅 0.59。窗口够大但检索质量
    # 在 256K 之后断崖式下降。
    # 实测 trace 822f2aaa: bptree helper iter 80+ 时上下文 400-500K,模型对早期
    # decisions / 自己的 todo list / 自己写的代码"看不见"——这正是 0.6 MRCR 区段。
    # 旧阈值让上下文飙到 600K+ 才压缩,等模型已经"失明"才救场。
    # 新阈值放在 256K 基准内主动维护:
    #   _PROACTIVE: 60K  — 提早开始软压(老 tool result fold)
    #   _SOFT:     120K — 常规压缩,target ~90K
    #   _HARD:     180K — 紧急压缩,target ~110K
    #   _PANIC:    240K — 256K 前最后抢救,target ~140K
    # 1M 物理上限只在高资源 helper / veryhard (Think Max) 等少数场景接近。
    # 2026-05-11 实测调整 (主进程起始 53KB, 跑长后工具链膨胀快):
    # 2026-06-06: 用户将主进程目标上限调为 256K,因此把所有门限继续前移。
    # 压缩仍先走语义 fold / 冗余 fold, emergency compact 只作为后段恢复。
    _TOKEN_BUDGET_PROACTIVE = 60_000     # 早期软压老工具链
    _TOKEN_BUDGET_SOFT = 120_000         # 软警戒线 — 常规压缩
    _TOKEN_BUDGET_HARD = 180_000         # 主动 emergency compact
    _TOKEN_BUDGET_PANIC = 240_000        # 256K 基准前的兜底抢救

    # 2026-05-12 P44: 工具结果预算 (参考 Claude Code applyToolResultBudget)
    # 不同 tool 的合理输出上限不同:
    # - delegate (helper done report): 50K, 真实任务报告大但要给主线程压缩空间
    # - workspace.run (bash 输出): 10K, 一般够看错误/前几行 + 末尾
    # - workspace.read / read_file: 20K, 读单文件分段
    # - search_*: 5K, 搜索匹配片段
    # - 其他: 默认 16K
    # 超出 budget 时 P44 截首+尾保留, 中间用 [...truncated N...] 标记
    _P44_DEFAULT_BUDGET = 16 * 1024
    _P44_TOOL_RESULT_BUDGET: dict[str, int] = {
        "delegate":            50 * 1024,
        "workspace":           20 * 1024,
        "read_file":           20 * 1024,
        "read_function":       16 * 1024,
        "search_files":         5 * 1024,
        "search_in_file":       8 * 1024,
        "search_across_files":  8 * 1024,
        "code_index":           8 * 1024,
        "fetch_to_temp":       30 * 1024,  # 网页/文档抓取可能较大
        "fetch_indexed_file":   30 * 1024,
        "fetch_group_file":    30 * 1024,
        "expand_warm":         12 * 1024,
        "expand_cold":         12 * 1024,
        "expand_kb":           12 * 1024,
        "recall_thread":       12 * 1024,
        "processes":            8 * 1024,
        "env_inventory":       12 * 1024,  # 160-row JSON ~24-48KB → force structured summary
        "env_list_tree":       10 * 1024,
        "env_run":             16 * 1024,
        "env_read":            16 * 1024,
    }
    _P44_HELPER_TOOL_RESULT_BUDGET: dict[str, int] = {
        # Helper loops benefit more from a stable, compact tool history than
        # from replaying large raw reads every iteration. Full evidence should
        # live in helper workspace files and be re-read by targeted range when
        # needed.
        #
        # helper 工具历史优先保持紧凑；完整证据写文件，需要时定向分段读取。
        "workspace":           8 * 1024,
        "read_file":           8 * 1024,
        "read_function":       8 * 1024,
        "search_files":        4 * 1024,
        "search_in_file":      4 * 1024,
        "search_across_files": 4 * 1024,
        "code_index":          6 * 1024,
        "fetch_to_temp":       8 * 1024,
        "fetch_indexed_file":   8 * 1024,
        "fetch_group_file":    8 * 1024,
        "processes":           4 * 1024,
    }

    def _tool_result_budget_for_loop(tool_name: str) -> int:
        if task_id is not None:
            return _P44_HELPER_TOOL_RESULT_BUDGET.get(
                tool_name,
                min(_P44_DEFAULT_BUDGET, 6 * 1024),
            )
        return _P44_TOOL_RESULT_BUDGET.get(tool_name, _P44_DEFAULT_BUDGET)

    def _should_structurally_summarize_tool_result(tool_name: str, result: str, budget: int) -> bool:
        if not isinstance(result, str):
            return False
        if len(result) > budget:
            return True
        if task_id is None:
            return False
        helper_thresholds = {
            "read_file": 1200,
            "read_function": 1200,
            "workspace": 1600,
            "search_files": 1000,
            "search_in_file": 1000,
            "search_across_files": 1000,
            "code_index": 1200,
            "fetch_to_temp": 1600,
            "fetch_indexed_file": 1600,
            "fetch_group_file": 1600,
        }
        if tool_name in helper_thresholds:
            return len(result) >= helper_thresholds[tool_name]
        if len(result) < max(1800, budget // 3):
            return False
        return False

    def _budget_tool_result_for_model_context(
        tc_id: str,
        tc_name: str,
        result: str,
        *,
        iteration: int,
    ) -> str:
        _budget = _tool_result_budget_for_loop(tc_name)
        result = _spill_large_tool_result_for_context(
            tc_name,
            result,
            spill_root=_tool_result_spill_root,
            iteration=iteration,
            call_id=str(tc_id or ""),
            total_threshold=_budget,
            field_threshold=min(8_000, max(1_000, _budget // 2)),
            field_head_chars=min(3_000, max(800, _budget // 4)),
        )
        # 2026-05-12 P44: 工具结果预算
        # 病因(参考工程师审查): Claude Code 用 applyToolResultBudget 统一限单
        # 个 tool 输出. 我们之前只在 _fold_old_tool_messages 用 force_fold_size=24K
        # 兜底, 是事后压缩. P44 是事前限流: 不同 tool 不同 budget, 超出自动摘要,
        # 主线程能立刻看到 "原始 N 字节, 已截断" 警告而不是事后被静默 fold.
        if _should_structurally_summarize_tool_result(tc_name, result, _budget):
            _orig_size = len(result)
            _orig_over_budget = len(result) > _budget
            _summary_result = _summarize_large_tool_result(
                tc_name,
                result,
                _budget,
                force=not _orig_over_budget,
                compact=task_id is not None,
            )
            if _summary_result is not None:
                result = _summary_result
            elif _orig_over_budget:
                # Fallback: keep only the head. The preceding spill layer has
                # already saved the full result whenever this exceeds the
                # model-visible budget.
                result = _head_only_tool_result_fallback(
                    result,
                    original_chars=_orig_size,
                    budget_chars=_budget,
                )
            else:
                # Medium helper results that cannot be structurally summarized stay intact.
                # They are below the hard budget and should not go through head/tail fallback.
                pass
            if _summary_result is not None or _orig_over_budget:
                debug.log(
                    "llm.tools.p44_truncated",
                    f"P44: 工具 {tc_name} 输出 {_orig_size}c "
                    f"{'>' if _orig_over_budget else '~'} budget {_budget}c, "
                    f"used={'structured_summary' if _summary_result is not None else 'head_tail_fallback'}",
                )
        if tc_name == "delegate":
            result = _sanitize_delegate_result_for_model_context(result)
        else:
            result = _sanitize_tool_result_internal_terms_for_model_context(result)
        debug.log(
            f"llm.tools.result",
            f"name={tc_name} id={tc_id}",
            _try_parse_json(result),
        )
        return result

    # ── meta-judge 旁路启动条件追踪 ──
    # 不阻塞主流程：每隔 N 轮 fire-and-forget 一个 lite 旁路评估，
    # 让 lite 看完整调用链回答"主模型是不是在原地打转/能力不够"。
    # 旁路把结果写入 upgrade_signal，由外层在循环结束后读取（不打断当前迭代）。
    META_JUDGE_FIRST_AT = 8     # 第 8 轮起开始评估
    META_JUDGE_INTERVAL = 6     # 此后每 6 轮再评估一次
    last_meta_judge_iter = 0

    # ── Bug 2 修:track 升级信号被写入后又有多少轮成功调用 ──
    # 历史教训(trace 96071c40):iter 8 时模型在反复 edit_file 失败,meta_judge 触发
    # should_upgrade=True。但模型从 iter 9 起恢复,iter 9-31 全部成功完成任务。
    # 然而旧逻辑只看到 sticky 的 upgrade_signal,在 loop 结束后照样升级——
    # 把 medium 跑出来的好计划丢掉,让 hard 重做(8.5 分钟里有 7 分钟是浪费)。
    # 修复:循环结束后,若已被打上升级标记,但之后大部分调用都成功了,
    # 就在末尾再做一次 meta_judge 重评估(此时有完整的"恢复"时间线供 lite 看)。
    # 见循环结束后的 _maybe_clear_stale_upgrade。
    successful_after_signal = 0  # signal 设 True 后,连续成功(不含失败)的工具调用数

    # ── 主线程 stuck detector (2026-05-02 加,trace 150eb2f2 教训) ──
    # 反复同种错误失败(如 lite 看不懂 gcc 报错 hint)时注入反思提示并触发升级。
    # 内部用 helper 的 StuckDetector 实现(已有完善的窗口/签名/同错重复检测逻辑)。
    # 2026-05-02 part19:传 parallelizable 给 detector,启用 long-no-delegate 检测
    # (主线程在可并行任务上跑久了仍没 spawn helper 时注入提醒)
    from app.llm.tools.delegate import StuckDetector as _StuckDetector
    stuck_main = _StuckDetector("main_thread", parallelizable=parallelizable)
    _stuck_trigger_count = 0    # 主线程 stuck 触发次数(跨检测窗口计数)
    _prev_stuck_state = False   # 上一轮的 stuck 状态,用于检测 Flase→True 跳变
    _stuck_consecutive_iters = 0  # ── Bug #20 修复:同周期连续 stuck 计数 ──
    _retryable_next_action_iter = -1
    _retryable_next_action_tasks: list[str] = []
    _retryable_next_action_facts: list[dict] = []
    _retryable_next_action_deferred = 0
    _retryable_next_action_max_defers = 2
    _retryable_next_action_forced_nudges = 0
    _retryable_next_action_max_nudges = 3
    _force_delegate_for_retry_gap = False
    _retryable_next_action_spawned: set[str] = set()
    _delegate_gap_facts: list[dict] = []
    _delegate_gap_facts_injected = False
    _read_only_write_warning_paths: set[str] = set()
    _browser_repro_pre_edit_warning_paths: set[str] = set()
    _browser_repro_pre_edit_warning_counts: dict[str, int] = {}
    _browser_repro_evidence_seen = False
    _browser_repro_predecision_hint_count = 0
    _browser_repro_predecision_hint_max = 4
    _browser_repro_helper_gap_tasks: set[str] = set()
    # Round 17 (#1): unified guidance dedup tracker. New hints must use this
    # instead of hand-rolled module-level flags; existing flags migrate as
    # they are touched (5 migrated so far).
    from app.llm.guidance_tracker import GuidanceTracker
    _guidance = GuidanceTracker()
    _main_source_edit_delegation_warning_paths: set[str] = set()
    _helper_staged_project_paths_seen: set[str] = set()
    _helper_owned_project_paths_seen: set[str] = set()
    _main_tainted_helper_staged_paths: set[str] = set()
    _unresolved_project_write_blocks: dict[str, dict] = {}
    _executed_tool_result_count = 0
    _main_helper_completion_checkpoint_emitted = False
    _llm_recovery_nudges = 0
    _llm_recovery_max_nudges = 3 if helper_kind is None else 2
    _helper_tool_bloat_count = 0

    idle_detector = _IdleDetector()

    # ── #13 修:per-tool 调用计数,防止死循环刷同一工具 ──
    # 不同工具有不同合理上限(read_file 调 30 次合理,edit_file 调 30 次基本是死循环)。
    # 触达上限时:在 tool result 里追加一条警告,让模型 LLM 看到自己卡住,转换思路。
    # 不直接 break——给模型一次机会优雅转向。
    _tool_call_counts: dict[str, int] = {}
    # ── 2026-05-02 part12:移除所有工具调用次数硬上限 ──
    # 用户明确要求"不要有任何硬限制了"。实测 trace 74b1295b 显示:
    # delegate=6 上限对 fan-out=7+ 任务的多轮 resume 续作明显不够,
    # 主线程被卡死后无法管理 helper(processes.list / kill 也救不了)。
    # 现在 _PER_TOOL_LIMITS 留空 dict,_tool_call_counts 仍记录(给 trace 看次数),
    # 但下方的 `if limit and ...` 永远不会触发 rate_limited。
    _PER_TOOL_LIMITS: dict[str, int] = {}

    # ── 后台任务追踪 ──
    # 修复 abort 之后 background tasks 仍在跑的问题:
    # meta-judge 旁路、progress 反馈生成 都是 fire-and-forget 的 LLM 调用,
    # 如果不追踪,abort 之后它们仍会消耗 API 配额,甚至写额外消息到 progress_queue。
    # 用 list 收集所有 create_task,finally 里统一 cancel + await 退出。
    bg_tasks: list[asyncio.Task] = []

    # ── 2026-05-07 Opt B: early lite switch tracking ──
    from collections import deque as _deque
    _recent_tool_names: _deque[str] = _deque(maxlen=6)
    _last_commit_to_main_iter: int = -1
    _last_delegate_result_summary: dict = {}  # {helpers_done: bool, ...}
    _artifact_acceptance_counts: dict[str, int] = {}
    _artifact_acceptance_hint_emitted: set[str] = set()
    _office_write_counts: dict[str, int] = {}
    _office_write_hint_emitted: set[str] = set()
    _office_read_counts: dict[str, int] = {}
    _office_read_hint_emitted: set[str] = set()
    _helper_completed_todos_handoff_emitted = False
    _helper_read_only_streak = 0
    _helper_read_to_write_hint_emitted = False
    _main_acceptance_script_facts: dict[str, dict] = {}
    _main_project_state_mutation_seen = False
    _main_verifier_command_seen = False
    _main_verifier_command_success_seen = False
    _main_last_project_state_mutation_iter = -1
    _main_last_successful_verifier_iter = -1
    _main_last_project_state_mutation_seq = -1
    _main_last_successful_verifier_seq = -1
    _main_last_project_state_mutation_helper_owned = False
    _main_last_project_state_mutation_path = ""
    _main_tool_result_sequence = 0
    _main_post_apply_verifier_checkpoint_nudges = 0
    _main_post_apply_verifier_checkpoint_pair: tuple[int, int, int, int] | None = None
    _main_post_apply_verifier_checkpoint_max_nudges = 3
    _main_verifier_visible_artifact_nudges = 0
    _main_verifier_visible_artifact_max_nudges = 2
    _main_existing_verifier_visible_artifacts: set[str] = set()
    _main_env_run_family_counts: dict[str, int] = {}
    _main_env_run_family_hint_emitted: set[str] = set()
    _main_discovered_source_test_paths: set[str] = set()
    _main_source_path_handoff_hint_emitted = False
    _main_helper_handoff_paths: set[str] = set()
    _main_helper_handoff_fact_iter = -1
    _main_text_material_handoff_hint_emitted = False
    _main_helper_handoff_direct_work_count = 0
    _main_helper_handoff_recent_labels: list[str] = []
    _main_helper_handoff_overwork_hint_emitted = False
    _main_helper_handoff_useful_delegate_seen = False
    _audit_evidence_final_check_original_content = ""
    _task_focus_refresh_iters: set[int] = set()

    def _merge_delegate_gap_facts_into_response_plan_json(content: str) -> str:
        """Preserve nonblocking processing gap facts in final ResponsePlan JSON."""
        if not _delegate_gap_facts:
            return content
        try:
            parsed = json.loads((content or "").strip())
        except Exception:
            return content
        if not isinstance(parsed, dict):
            return content
        gap_lines: list[str] = []
        for fact in _delegate_gap_facts[-6:]:
            task = str(fact.get("task_id") or "?")
            kind = _neutral_round3_gap_text(fact.get("kind") or "?")
            gap_kind = _neutral_round3_gap_text(fact.get("gap_kind") or "processing_gap")
            reason = _neutral_round3_gap_text(fact.get("terminal_reason") or "")
            missing = _round3_gap_missing_items(fact.get("outputs_missing"))
            missing_text = f"; missing={', '.join(missing)}" if missing else ""
            gap_lines.append(f"processing gap {task} ({kind}): {gap_kind}/{reason}{missing_text}")
        existing_joined = " ".join(str(x) for x in (parsed.get("key_points") or []))
        existing_joined += " " + str(parsed.get("internal_note") or "")
        new_lines = [line for line in gap_lines if line not in existing_joined]
        if not new_lines:
            return content
        key_points = parsed.get("key_points")
        if not isinstance(key_points, list):
            key_points = []
        key_points.extend("Nonblocking processing gap fact: " + line for line in new_lines)
        parsed["key_points"] = key_points
        note = str(parsed.get("internal_note") or "")
        gap_note = (
            " Nonblocking processing gap facts were preserved for Round3 wording; "
            "they do not decide completion by themselves."
        )
        if "Nonblocking processing gap facts" not in note:
            parsed["internal_note"] = (note + gap_note).strip()
        return json.dumps(parsed, ensure_ascii=False)

    def _check_aborted() -> bool:
        """abort 检测点统一入口,日志同时记录。"""
        if abort_event and abort_event.is_set():
            return True
        return False

    it = 0
    hit_cap = False
    forced_finalize_trigger = "unknown"
    # 2026-05-17 P159: helper iter 硬上限 + 软警告
    # 病因: bptree_db helper 153 iter 螺旋(同 tool 反复)直到 wait_window 超时,
    # 系统从未给"你已转太多 iter"信号。
    # 设计: helper (task_id is set) 在 iter=80 注入软警告,iter=200 强制 finalize。
    # 主线程也需要安全收束:不把失败硬改成成功,而是在长链里程碑后要求输出
    # 当前证据的 JSON 计划,避免外层 scenario timeout 直接杀掉整轮成果。
    _HELPER_ITER_SOFT_WARN = 50
    _HELPER_ITER_FINALIZE_WARN = 90
    _HELPER_ITER_HARD_CAP = 140
    _helper_iter_warned = False
    _helper_iter_finalize_warned = False
    _MAIN_ITER_MILESTONE_WARN = 36
    _MAIN_ITER_FINALIZE_WARN = 60
    _MAIN_ITER_HARD_CAP = 120
    _main_iter_milestone_warned = False
    _main_iter_finalize_warned = False
    try:
        while True:
            # 检查 abort 信号(轮开始)
            if _check_aborted():
                forced_finalize_trigger = "abort_event_loop_top"
                debug.log("llm.tools.abort", f"aborted at iter {it} (loop top)")
                break

            # 硬上限(2026-05-02 part12 后默认 None = 无上限,跳过此分支):
            # caller 显式传 max_iter 数值时仍生效(向后兼容,但当前所有 caller 都传 None)
            if effective_cap is not None and it >= effective_cap:
                debug.log(
                    "llm.tools.cap",
                    f"hit iteration cap ({effective_cap}); forcing finalize",
                )
                log.warning("tools loop hit cap %d (max_iter=%s); forcing finalize",
                            effective_cap, max_iter)
                hit_cap = True
                forced_finalize_trigger = "iteration_cap"
                break

            # P159: helper iter 硬上限(只对 helper 生效)
            if task_id is not None and it >= _HELPER_ITER_HARD_CAP:
                debug.log(
                    "llm.tools.helper_iter_hard_cap",
                    f"helper {task_id} hit hard iter cap {_HELPER_ITER_HARD_CAP}; forcing finalize",
                )
                log.warning(
                    "helper %s hit hard iter cap %d; forcing finalize",
                    task_id, _HELPER_ITER_HARD_CAP,
                )
                hit_cap = True
                forced_finalize_trigger = "helper_iteration_cap"
                break

            if task_id is None and it >= _MAIN_ITER_HARD_CAP:
                debug.log(
                    "llm.tools.main_iter_hard_cap",
                    f"main tool loop hit hard cap {_MAIN_ITER_HARD_CAP}; forcing json finalize",
                )
                log.warning("main tool loop hit hard cap %d; forcing finalize", _MAIN_ITER_HARD_CAP)
                hit_cap = True
                forced_finalize_trigger = "main_iteration_cap"
                break

            it += 1
            debug.log("llm.tools.iter", f"iter {it}")

            if (
                task_id is None
                and not _browser_repro_evidence_seen
                and _browser_repro_predecision_hint_count < _browser_repro_predecision_hint_max
                and _active_task_requires_browser_pre_edit_evidence(msgs)
                and not _active_task_has_browser_evidence_fact(msgs)
            ):
                _browser_repro_predecision_hint_count += 1
                _append_tool_loop_dynamic_guidance(
                    msgs,
                    _browser_pre_edit_predecision_guidance(iteration=it),
                )
                debug.log(
                    "llm.tools.browser_repro_predecision_fact",
                    f"injected at iter {it} count={_browser_repro_predecision_hint_count}",
                )

            # Quality-first task focus refresh. Long tool loops tend to bury
            # the original request and active contract behind tool transcripts.
            # Keep a compact factual snapshot near the prompt tail; this is not
            # a symbolic decision about success or routing.
            try:
                _chars_for_focus = sum(len(str(m.get("content") or "")) for m in msgs)
                if task_id is None:
                    _focus_due = (
                        it in {12, 24}
                        or (it >= 36 and it % 18 == 0)
                        or (_chars_for_focus >= 90_000 and it % 12 == 0)
                    )
                else:
                    _focus_due = (
                        it in {18, 36}
                        or (it >= 54 and it % 27 == 0)
                        or (_chars_for_focus >= 70_000 and it % 18 == 0)
                    )
                if _focus_due and it not in _task_focus_refresh_iters:
                    _task_focus_refresh_iters.add(it)
                    _append_tool_loop_dynamic_guidance(
                        msgs,
                        _task_focus_refresh_hint(
                            iteration=it,
                            task_id=task_id,
                            helper_kind=helper_kind,
                            chars_total=_chars_for_focus,
                        ),
                    )
                    debug.log(
                        "llm.tools.task_focus_refresh",
                        f"iter={it} task_id={task_id or 'main'} chars={_chars_for_focus}",
                    )
            except Exception as _focus_exc:
                debug.log("llm.tools.task_focus_refresh.error", repr(_focus_exc))

            if task_id is None and not _main_iter_milestone_warned and it >= _MAIN_ITER_MILESTONE_WARN:
                _main_iter_milestone_warned = True
                debug.log(
                    "llm.tools.main_iter_milestone_warn",
                    f"main loop reached iter {it}; asking model to converge on a verified milestone",
                )
                _append_tool_loop_dynamic_guidance(
                    msgs,
                    main_milestone_checkpoint(it, _MAIN_ITER_HARD_CAP),
                )
            if task_id is None and not _main_iter_finalize_warned and it >= _MAIN_ITER_FINALIZE_WARN:
                _main_iter_finalize_warned = True
                debug.log(
                    "llm.tools.main_iter_finalize_warn",
                    f"main loop reached iter {it}; asking model to finalize current evidence soon",
                )
                _append_tool_loop_dynamic_guidance(
                    msgs,
                    main_finalize_window(it, _MAIN_ITER_HARD_CAP),
                )

            # P159: helper iter 软警告 (只对 helper 生效, 仅警告一次)
            if (task_id is not None
                    and not _helper_iter_warned
                    and it >= _HELPER_ITER_SOFT_WARN):
                _helper_iter_warned = True
                debug.log(
                    "llm.tools.helper_iter_soft_warn",
                    f"helper {task_id} reached iter {it} (soft warn at {_HELPER_ITER_SOFT_WARN}); "
                    f"will hard-finalize at {_HELPER_ITER_HARD_CAP}",
                )
                _append_tool_loop_dynamic_guidance(
                    msgs,
                    helper_iter_checkpoint(it, _HELPER_ITER_HARD_CAP, helper_kind),
                )

            if (task_id is not None
                    and not _helper_iter_finalize_warned
                    and it >= _HELPER_ITER_FINALIZE_WARN):
                _helper_iter_finalize_warned = True
                debug.log(
                    "llm.tools.helper_iter_finalize_warn",
                    f"helper {task_id} reached iter {it}; asking model to finalize a handoff artifact",
                )
                _append_tool_loop_dynamic_guidance(msgs, helper_finalize_window(it))

            # 每个 iter 重置 hard timeout 重试标志(2026-05-03)
            _llm_hard_timeout_retried = False

            # 2026-05-11 Tier 2.B + F + J: 主线程上下文注入
            # 仅对主线程 (helper_kind is None), 不影响 helper loop。
            #
            # 设计理由(实测 trace 56 分钟教训):
            # - 主线程长跑后 LLM 上下文太长 → "忘记"已 done helper, 又派同名
            # - helper stuck 时 retry_instruction 在 tool result 里, 但 LLM 可能漏看
            # - 原始任务在最早 messages, 长跑后被淹没
            #
            # 2026-05-15 P97 修 (实测 排序论文 trace cache miss 分析):
            #   病因: tier2b 每 5 iter 注入 helper summary, tier2j 在 80K chars 注入 recall。
            #     这些注入 append 到 msgs 末尾, **破坏 prefix cache** — cache hit 从 87%
            #     掉到 3% 持续到下次 cache 重建。trace 实测 5 次 tier2b 注入 + 1 次 tier2j
            #     → 6 次 cache miss → 浪费约 3 分钟 + 大量 prompt token 重算。
            #   修法:
            #     1. tier2b 频率从每 5 iter 改为每 10 iter (减少 50% 注入)
            #     2. 增加 _last_tier2b_ledger_size 守门 — ledger 没新增完成不重复注入
            #     3. tier2j 阈值从 80K 提到 120K chars (主线程很多时候 80K 是正常工作量)
            #     4. tier2j 也加防重 (一次会话只注一次)
            if helper_kind is None:
                try:
                    from app.llm.tools.delegate import _get_completion_ledger
                    _trace_id_now = debug.current_trace_id() or ""
                    # Tier 2.F 已移至紧贴 tool result 追加位置(本文件下方 ~2576 行)
                    # 那个位置触发更及时(tool 一返回就追加 hint),且按 task_id 防重而非按 iter 防重

                    # 2026-05-15 P102: tier2 注入改造 — 不再 msgs.append 新 system msg
                    # 病因(深度 trace 分析): msgs.append 在主线程长会话中段插 system msg
                    # → 破坏 prefix cache → iter 命中率从 87% 掉到 3% (实测 1920/58097)
                    # → 浪费 ~30-50K tokens prefill × N 次 = ~40-60 秒。
                    # 修法: 把 hint 内容 **追加到最后一条 tool_result 的 content 末尾**,
                    # 不引入新 msg position。这样 prefix cache 仍能命中 msgs[0..N-2],
                    # 仅 msgs[N-1] (本身就要变) 的内容稍微长一点。
                    # 取舍: hint 看起来像"工具结果一部分"而不是显眼的 system msg,
                    # 但配合 [SYSTEM_*] 前缀标记, LLM 仍能识别。
                    def _inject_into_last_tool_result(hint_text: str) -> bool:
                        """把 hint 追加到 msgs 最后一条 tool role 的 content。
                        返回 True 表示注入成功, False 表示找不到 tool msg (兜底 append)。
                        """
                        for _i in range(len(msgs) - 1, -1, -1):
                            _m = msgs[_i]
                            if _m.get("role") == "tool":
                                _old = _m.get("content", "") or ""
                                # 保留 JSON 结构: 如果原 content 是 JSON, 在末尾加 \n\n[hint]
                                _m["content"] = _old + "\n\n" + hint_text
                                return True
                        return False

                    # B: 主 loop iter > 10 后每 10 iter 注入 helper 摘要(ledger 历史)
                    # P97: 5→10, 加 ledger size 守门
                    if it > 10 and it % 10 == 0 and _trace_id_now:
                        _ledger = _get_completion_ledger(_trace_id_now, last_n=8)
                        # P97 守门: 只在 ledger 自上次注入后新增 helper 才注入
                        _last_size = locals().get("_last_tier2b_ledger_size", -1)
                        if _ledger and len(_ledger) > _last_size:
                            _lines = [f"[SYSTEM_HELPER_SUMMARY/iter{it}]"]
                            _lines.append(f"{len(_ledger)} helpers have completed in this session (latest 8):")
                            for _e in _ledger:
                                _flags = []
                                if _e.get("outputs_complete") is True:
                                    _flags.append("outputs_complete")
                                elif _e.get("outputs_missing"):
                                    _flags.append(f"missing {len(_e['outputs_missing'])} outputs")
                                if _e.get("terminal_reason") == "stuck":
                                    _flags.append("stuck")
                                elif _e.get("terminal_reason") == "crashed":
                                    _flags.append("crashed")
                                # 2026-05-11 P12.I: quality_warnings 也显示
                                _qw = _e.get("quality_warnings") or []
                                if _qw:
                                    _flags.append(f"{len(_qw)} quality warnings")
                                _summary = _e.get("delivered_summary") or {}
                                _cats = ",".join(
                                    f"{k}({len(v)})" for k, v in _summary.items() if v
                                )
                                _evidence_files = _e.get("internal_evidence_files") or []
                                _read_summary = _e.get("read_evidence_summary") or {}
                                _read_verdicts = _read_summary.get("verdicts") if isinstance(_read_summary, dict) else []
                                _lines.append(
                                    f"  - {_e['task_id']}: "
                                    f"{_e['terminal_reason']}, "
                                    f"{_e.get('delivered_count', 0)} files [{_cats or 'none'}] "
                                    f"{' '.join(_flags)}"
                                )
                                if _evidence_files:
                                    _lines.append(
                                        "      internal_evidence_files: "
                                        + ", ".join(str(p) for p in _evidence_files[:8])
                                    )
                                if isinstance(_read_verdicts, list) and _read_verdicts:
                                    _verdict_bits = []
                                    for _rv in _read_verdicts[:4]:
                                        if not isinstance(_rv, dict):
                                            continue
                                        _verdict_bits.append(
                                            f"{_rv.get('file','?')}={_rv.get('verdict') or '?'}"
                                        )
                                    if _verdict_bits:
                                        _lines.append(
                                            "      read_evidence_verdicts: "
                                            + "; ".join(_verdict_bits)
                                        )
                                # quality_warnings 详情(逐项)
                                for _qw_item in _qw[:3]:  # 最多显示 3 个
                                    _lines.append(
                                        f"      warning {_qw_item.get('file','?')}: "
                                        f"{_qw_item.get('issue','?')} — "
                                        f"{_qw_item.get('details','')[:120]}"
                                    )
                                if len(_qw) > 3:
                                    _lines.append(f"      ... {len(_qw)-3} more quality warnings in outputs_check.quality_warnings")
                            _lines.append(
                                "Use outputs_complete as file-presence evidence. Treat producer_self_verified=true with no blocking warnings as the clean producer completion boundary. For clean results, consume the result report, output map, and check facts without re-reading or re-validating producer-owned artifact bodies. For missing, warning-bearing, contradictory, stale, or explicitly independent-QA boundaries, use the report, warnings, and active contract to decide producer resume, independent verification, repair, or partial delivery."
                            )
                            _lines.append(
                                "For quality warnings, inspect details and decide whether repair or verification is needed.\n\n"
                                "已完成 helper 摘要，用于判断是否需要继续、修复或新派任务。"
                            )
                            _hint = "\n".join(_lines)
                            # P102: 优先注入到最后 tool result, 兜底 append
                            _injected_to_tool = _inject_into_last_tool_result(_hint)
                            if not _injected_to_tool:
                                _append_tool_loop_dynamic_guidance(msgs, _hint)
                            _last_tier2b_ledger_size = len(_ledger)
                            debug.log("llm.tools.tier2b.injected",
                                      f"iter {it}: helper summary injected ({len(_ledger)} entries, "
                                      f"ledger grew from {_last_size} → {len(_ledger)}, "
                                      f"via={'tool_result' if _injected_to_tool else 'system_msg'})")

                    # J: 按上下文长度阈值触发 recall(代替原"30 min")
                    # 主线程跑久后, 原始任务在最早 msgs, 容易被淹没。
                    # P97: 80K → 120K chars (avoid premature cache invalidation)
                    if (not locals().get("_tier2j_recall_injected")
                            and _trace_id_now):
                        _chars_total = sum(
                            len(m.get("content", "") or "") for m in msgs
                        )
                        # P97: 阈值 80K → 120K chars (~30K tokens)
                        if _chars_total >= 120_000:
                            _ledger_count = len(_get_completion_ledger(_trace_id_now, last_n=100))
                            _contract_snapshot = _current_task_contract_snapshot()
                            try:
                                from app.core import agent_state as _agent_state
                                _state = _agent_state.structured_status(_trace_id_now)
                                _contracts = _state.get("contracts") or []
                                _ready_artifacts = _state.get("artifacts_ready") or []
                            except Exception:
                                _contracts = []
                                _ready_artifacts = []
                            _recall_text = auto_recall_checkpoint(
                                chars_kb=_chars_total // 1024,
                                iteration=it,
                                contract_snapshot=_contract_snapshot,
                                helper_count=_ledger_count,
                                contract_count=len(_contracts),
                                ready_artifact_count=len(_ready_artifacts),
                            )
                            # P102: 同样注入到最后 tool result, 兜底 append
                            _injected_to_tool = _inject_into_last_tool_result(_recall_text)
                            if not _injected_to_tool:
                                _append_tool_loop_dynamic_guidance(msgs, _recall_text)
                            _tier2j_recall_injected = True
                            debug.log("llm.tools.tier2j.injected",
                                      f"iter {it}: recall injected (ctx={_chars_total} chars, "
                                      f"via={'tool_result' if _injected_to_tool else 'system_msg'})")
                except Exception as _e_tier2:
                    # Tier 2 注入失败不能影响主流程
                    debug.log("llm.tools.tier2.error", repr(_e_tier2))

            # 2026-05-10 Patch 85: helper 长跑反复探索警告
            # 病因(trace 822f2aaaff0e4998):bptree helper 跑 100 iter,
            # 2879 次工具调用(read_function 506, todo_write 466, read_file 372...),
            # 大量"反复读代码+反复编辑"循环,44 分钟跑不完。LLM 在 iter 30+ 后
            # 上下文累积,容易忘了任务核心,陷入探索-编辑死循环。
            #
            # 修法:在 helper(helper_kind != None) iter 达阈值时,追加一个
            # system reminder,引导 LLM 收敛节奏。每个阈值只追加一次。
            if helper_kind is not None and helper_kind != "legacy_hard":
                # 用闭包变量,首次进 loop 时初始化
                if not locals().get("_p85_warned_30") and it == 30:
                    _append_tool_loop_dynamic_guidance(msgs, helper_pace_check(it))
                    _p85_warned_30 = True
                elif not locals().get("_p85_warned_60") and it == 60:
                    _append_tool_loop_dynamic_guidance(msgs, helper_long_run(it))
                    _p85_warned_60 = True
                elif not locals().get("_p85_warned_90") and it == 90:
                    _append_tool_loop_dynamic_guidance(msgs, helper_finalize_window(it))
                    _p85_warned_90 = True

            # ── 动态 reasoning / model(2026-05-03 v14)──
            # 默认 reasoning 整 loop 不变,但高资源 helper 可用 reasoning_callback
            # 实现三阶段切换。callback 接 (it, msgs) 返回:
            #   - str → 当前 reasoning 字符串 ("disabled" / "high" / "max") (向后兼容)
            #   - dict {"reasoning": ..., "lite": ..., "model_spec": ...}
            #     model_spec 优先,其次 lite/reasoning
            if reasoning_callback is not None:
                try:
                    cb_result = reasoning_callback(it, msgs)
                    new_reasoning: str | None = None
                    new_lite: bool | None = None
                    new_model_spec = None
                    if isinstance(cb_result, str):
                        new_reasoning = cb_result
                    elif isinstance(cb_result, dict):
                        if "model_spec" in cb_result:
                            new_model_spec = cb_result["model_spec"]
                        if isinstance(cb_result.get("reasoning"), str):
                            new_reasoning = cb_result["reasoning"]
                        if "lite" in cb_result and isinstance(cb_result["lite"], bool):
                            new_lite = cb_result["lite"]
                    if new_model_spec is not None:
                        if new_model_spec.model != model or new_model_spec.reasoning != reasoning:
                            debug.log(
                                "llm.tools.model_spec_switch",
                                f"model_spec: {model}/{reasoning} -> "
                                f"{new_model_spec.model}/{new_model_spec.reasoning} at iter {it}",
                            )
                            model = new_model_spec.model
                            reasoning = new_model_spec.reasoning
                            extra_body = _thinking_extra_body(reasoning, new_model_spec.provider)
                            _cli_container[0] = _client_for_spec(new_model_spec)
                    elif new_reasoning and new_reasoning != reasoning:
                        debug.log(
                            "llm.tools.reasoning_switch",
                            f"reasoning: {reasoning} -> {new_reasoning} at iter {it}",
                        )
                        reasoning = new_reasoning
                        extra_body = _thinking_extra_body(reasoning)
                    if new_lite is not None and new_model_spec is None:
                        _new_spec = _legacy_model_spec(lite=new_lite, reasoning=reasoning)
                        new_model = _new_spec.model
                        if new_model != model:
                            debug.log(
                                "llm.tools.model_switch",
                                f"model: {model} -> {new_model} at iter {it} (lite={new_lite})",
                            )
                            model = new_model
                            reasoning = _new_spec.reasoning
                            extra_body = _thinking_extra_body(reasoning, _new_spec.provider)
                            _cli_container[0] = _client_for_spec(_new_spec)
                except Exception as _cb_e:
                    debug.log(
                        "llm.tools.reasoning_callback.error",
                        f"reasoning_callback raised: {_cb_e!r}; keeping current",
                    )

            # ── helper 心跳汇报 (2026-05-02 加) ──
            # 让主线程通过 processes.list 看到 helper 当前 iter / 最近思考。
            # Patch 05: 主线程上下文(无 helper proc_id)直接跳过,避免每 round 200+ noop task
            # (旧版无脑 create_task -> coroutine first line `if not pid: return False`,
            # 创建+调度+await 累积成本)。
            try:
                from app.core.core_processes import (
                    report_helper_progress, current_helper_proc_id,
                )
                if current_helper_proc_id() is not None:
                    # iter 数 + 最近一段 assistant thought (从前一轮 msgs 末尾抽)
                    _last_thought = ""
                    for _m in reversed(msgs):
                        if _m.get("role") == "assistant":
                            _c = str(_m.get("content") or "").strip()
                            if _c and len(_c) > 20:
                                _last_thought = _c[:200]
                                break
                    # fire-and-forget,不阻塞主循环 (单 asyncio loop 下 update 也很快)
                    from app.core.bg_tasks import schedule

                    bg_tasks.append(schedule(
                        report_helper_progress(iter_num=it, thought=_last_thought),
                        name=f"helper_progress:iter:{task_id or 'unknown'}",
                    ))
            except Exception:
                pass  # helper 心跳失败永不阻塞主循环

            # B4 修复: token budget watchdog — 主动估算 + 紧急压缩
            # 不依赖 BadRequest 错误兜底;在每轮主 LLM 调用前提前检查防爆。
            # 比旧的"it > 8 才 fold"更主动:任何时候只要 token 用量上来了就压。
            #
            # 2026-05-03 加:**任务边界折叠**先于 token budget 检查。
            # 这是语义级压缩 — todo 完成时把该任务期间的 tool call 折叠成
            # "完成了 X" 一条,信息量保留(知道做过什么),细节不保留(过程不重要)。
            # 比按年龄/大小压更精准 — 一个 todo 完成 = 一个语义闭环。
            try:
                _fold_completed_task_window(msgs)
            except Exception as e:
                debug.log("llm.tools.task_boundary_fold.error", str(e))
            try:
                _soft_compact_redundant_tool_results(msgs)
            except Exception:
                log.exception("pre-budget soft compact failed (non-fatal)")

            est_tokens = _estimate_msgs_token_size(msgs)
            # 2026-05-11 A1: 四级渐进压缩,不硬截只软退化
            if est_tokens >= _TOKEN_BUDGET_PANIC:
                debug.warn(
                    f"token budget PANIC: est={est_tokens} >= {_TOKEN_BUDGET_PANIC} "
                    f"(near 256K baseline); emergency compact target 140K"
                )
                _emergency_compact_msgs(msgs, target_token_budget=140_000)
            elif est_tokens >= _TOKEN_BUDGET_HARD:
                debug.warn(
                    f"token budget HARD: est={est_tokens} >= {_TOKEN_BUDGET_HARD} "
                    f"(above 256K baseline comfort zone); emergency compact target 110K"
                )
                _emergency_compact_msgs(msgs, target_token_budget=110_000)
            elif est_tokens >= _TOKEN_BUDGET_SOFT:
                # 软警戒线 — 激进 fold (keep_recent 收到 2,force size 降到 8K)
                _fold_old_tool_messages(msgs, keep_recent_iters=2,
                                         force_fold_size=8 * 1024)
                debug.log(
                    "llm.tools.fold.aggressive",
                    f"aggressive fold at est={est_tokens} (target stay <{_TOKEN_BUDGET_HARD})",
                )
            elif est_tokens >= _TOKEN_BUDGET_PROACTIVE:
                # 主动 fold — keep 3 iters,目标维持在绿区中段
                _fold_old_tool_messages(msgs, keep_recent_iters=3,
                                         force_fold_size=16 * 1024)
            elif task_id is None and it > 5:
                # 2026-05-11 优化: 默认 fold 起点从 it > 8 → it > 5
                # 主进程起始 ctx 约 53KB, 跑 5 iter 后工具链膨胀快 → 提早压老 tool result
                # keep_recent=4 保留最近 4 轮的完整工具调用历史
                # force_fold_size 24K → 单条 tool result 超过会被强制压(原 32K)
                # 这样 helper 完成 result 含大段 files/report 也能更早压
                #
                # 2026-06-03 cache 修复:
                #   routine fold 会改写历史中部消息。主进程长链需要它控制上下文,
                #   但 helper 链通常较短且前缀命中目标更高;实测 read helper iter 10
                #   因该路径触发短间隔 prefix change,命中率从 90%+ 掉到 66%。
                #   因此 helper 只保留上面的预算压力折叠,不做无压力 routine fold。
                #
                # 2026-05-15 P102 修复 cache miss (实测排序论文 trace):
                #   病因: 每 iter (> 5) 都跑 routine fold → 即使只折 1 条, 也改变中部 msg
                #   token 序列 → prefix cache 失效。trace 35 次 fold.routine, 每次配
                #   一次 cache 大跌 (40%+)。
                #   修法: 改为**每 5 iter 跑一次 routine** + **est_tokens 较上次增长 >25%**
                #   才跑。这样 cache miss 频率从每 iter 1 次降到 ~每 5 iter 0-1 次。
                #   注意: fold.aggressive 和 fold.task_boundary 不变 (软警戒/任务边界必须压)。
                _last_routine_est = locals().get("_last_routine_fold_est", 0)
                _should_routine_fold = (
                    it % 5 == 0  # 每 5 iter 跑一次
                    or est_tokens > _last_routine_est * 1.25  # 或 ctx 显著增长
                )
                if _should_routine_fold:
                    _fold_n = _fold_old_tool_messages(msgs, keep_recent_iters=4,
                                                       force_fold_size=20 * 1024)
                    _last_routine_fold_est = est_tokens
                    if _fold_n and current_helper_proc_id() is None:
                        debug.log(
                            "llm.tools.fold.routine",
                            f"P43: 主进程 iter {it} 折叠 {_fold_n} 条老 tool result "
                            f"(est={est_tokens}, prev={_last_routine_est})",
                        )

            # 2026-05-12 P43: 主进程 ctx 大小监控 log
            # 病因(实测 21:05 trace): 主进程 51 iter / 65min, fold 静默运行无 log,
            # 无法验证 ctx 是否真的膨胀。用户要求"维持主进程前后文复杂度",
            # 必须让 ctx 演化可观测。每 5 iter 输出一次 ctx 总 size + msg 数。
            if current_helper_proc_id() is None and (it == 1 or it % 5 == 0):
                _ctx_msgs = len(msgs)
                _ctx_size = sum(
                    len(str(m.get("content") or "")) +
                    len(str(m.get("tool_calls") or ""))
                    for m in msgs
                )
                _folded_count = sum(
                    1 for m in msgs
                    if m.get("_folded") or m.get("_task_boundary_fold")
                )
                debug.log(
                    "llm.tools.ctx_size",
                    f"P43: 主进程 iter {it} ctx_size={_ctx_size}c "
                    f"msgs={_ctx_msgs} folded={_folded_count} "
                    f"est_tokens={est_tokens}",
                )

            # ── Bug A 预防: 折叠后主动修复孤儿 tool_calls ──
            # 折叠函数只压 content 不动 tool_call_id 配对,但多轮折叠叠加 +
            # 大 delegate 结果可能导致 API 侧校验失败。不等 API 报错再修,
            # 每次折叠后主动检查一次,成本极低(扫一遍 msgs 找配对)。
            _pre_repair = _repair_tool_call_pairing(msgs)
            if _pre_repair > 0:
                debug.log(
                    "llm.tools.pre_repair",
                    f"proactive repair fixed {_pre_repair} orphaned "
                    f"tool_calls at iter {it} (after folding)",
                )

            # ── meta-judge 旁路触发（与主 LLM 调用并行执行）──
            # 仅在还没有升级信号、且当前是 lite/main(disabled) 路径时评估
            # （已经在 max thinking 顶档了就没必要评估）
            should_run_meta = (
                upgrade_signal is not None
                and not upgrade_signal.get("should_upgrade", False)
                and reasoning != "max"
                and it >= META_JUDGE_FIRST_AT
                and (it - last_meta_judge_iter) >= META_JUDGE_INTERVAL
            )
            meta_task = None
            if should_run_meta:
                last_meta_judge_iter = it
                msgs_snapshot = [m.copy() for m in msgs]
                meta_task = asyncio.create_task(_meta_judge_should_upgrade(
                    msgs_snapshot, current_iter=it,
                    current_lite=lite, current_reasoning=reasoning,
                    signal=upgrade_signal,
                ))

            # 进度反馈不再"每 8 轮固定触发"——改在 _run 里基于剧情节点触发。
            # 剧情节点 = 卡住 / 突破 / 长时间静默(详见 _ToolProgressState)
            # 检查 abort(LLM 等响应是大头,这里再查一次缩短感知延迟)
            if _check_aborted():
                forced_finalize_trigger = "abort_event_pre_llm"
                debug.log("llm.tools.abort", f"aborted at iter {it} (pre-LLM)")
                break

            # 主 LLM 调用，与 meta-judge 并行（meta_task 非 None 时一起跑）
            # ── prefix 续写 detection(2026-05-03 v12 / v17 修订)──
            # 原 v12 设计:msgs 末尾是 {"role":"assistant", "prefix":True} 时走 beta。
            # **v17 修复关键约束**:DeepSeek API 不允许 prefix + tools 同时使用
            #   (返回 400 "Function call should not be used with prefix")。
            # 所以**只有 tools 为空时才允许 prefix**(目前所有 helper 都带 tools,
            # 实际不会走到 _use_beta=True 路径,但保留逻辑以防未来无 tools 场景)。
            _has_prefix_msg = (
                len(msgs) > 0
                and isinstance(msgs[-1], dict)
                and msgs[-1].get("role") == "assistant"
                and msgs[-1].get("prefix") is True
            )
            _use_beta = _has_prefix_msg and not tools  # 关键:tools 非空时禁用 prefix
            # 如果 msgs 末尾是 prefix 但 tools 非空,把那条 prefix 当普通 assistant 用
            # (LLM 看到也无害 — 它会把 prefix content 当历史发言)。这里不主动改 msgs,
            # 因为 prefix 字段透传给 API 时如果有 tools 会被 reject;让调用方自己保证不传。
            if _has_prefix_msg and tools:
                debug.log(
                    "llm.tools.prefix.skipped_due_to_tools",
                    f"prefix assistant detected at iter {it} but tools present; "
                    f"removing prefix flag (API incompatible)",
                )
                # 防御:把 prefix 字段抹掉,避免传给 API 触发 400
                try:
                    msgs[-1].pop("prefix", None)
                except (AttributeError, TypeError):
                    pass
                _has_prefix_msg = False

            # ── 2026-05-04 v19: streaming 实现替代 stream=False ──
            # 旧实现:stream=False + 上层 asyncio.wait_for(90s) 总长截断
            #   → 写论文 reasoning 100-200s 是正常的 → 90s 误杀
            # 新实现:stream=True + idle-based timeout(每 chunk 重置)
            #
            # v19.1 timeout 策略二分(基于实测教训):
            # - thinking_disabled:模型直接吐 token,99.9% 不会卡死,
            #   只要 chunks 持续来就一直读 → idle 用极大值兜底(1h)
            # - thinking enabled/high/max:reasoning chain 偶尔死循环
            #   (实测 trace 9b3a6a/85e330/94fe17),需 90s 兜底,
            #   超时后续写(用 partial reasoning_content 作提示)
            _is_thinking = _is_thinking_enabled(extra_body)
            if _is_thinking:
                _stream_idle = _THINK_STREAM_IDLE_TIMEOUT
                _stream_first_chunk = _configured_stream_first_chunk_timeout(
                    _THINK_STREAM_FIRST_CHUNK_TIMEOUT
                )
            else:
                _stream_idle = _NOTHINK_STREAM_IDLE_TIMEOUT
                _stream_first_chunk = _configured_stream_first_chunk_timeout(
                    _NOTHINK_STREAM_FIRST_CHUNK_TIMEOUT
                )
            debug.log(
                "llm.tools.timeout_budget",
                f"iter {it} task={task_id}: "
                f"thinking={_is_thinking} "
                f"idle={_stream_idle}s first_chunk={_stream_first_chunk}s"
            )
            try:
                from app.core import toolchain_cache as _toolchain_cache
                _trace_id_for_schema = debug.current_trace_id()
                _tools_for_iter = _toolchain_cache.filter_tools_for_trace(
                    tools,
                    _trace_id_for_schema,
                )
                _schema_retry_guidance = _toolchain_cache.tool_schema_retry_guidance(
                    tools,
                    _trace_id_for_schema,
                )
            except Exception:
                _tools_for_iter = tools
                _schema_retry_guidance = ""
            if _schema_retry_guidance:
                _msgs_for_iter = msgs + [{
                    "role": "user",
                    "content": _schema_retry_guidance,
                }]
                debug.log(
                    "llm.tools.schema_retry_guidance",
                    f"injecting transient full-schema guidance at iter {it} "
                    f"(chars={len(_schema_retry_guidance)})",
                )
            else:
                _msgs_for_iter = msgs
            _tool_choice_for_iter = None
            if _force_delegate_for_retry_gap and _tools_for_iter:
                has_delegate_tool = any(
                    isinstance(tool, dict)
                    and ((tool.get("function") or {}).get("name") == "delegate")
                    for tool in _tools_for_iter
                )
                if has_delegate_tool:
                    _tool_choice_for_iter = {
                        "type": "function",
                        "function": {"name": "delegate"},
                    }
                    debug.log(
                        "llm.tools.retry.force_delegate",
                        "forcing delegate tool choice for unresolved retry gap",
                    )

            async def _call_llm():
                _the_client = (
                    beta_client()
                    if _use_beta and (model_spec is None or model_spec.provider.name == "deepseek")
                    else _cli_container[0]
                )
                _resp_compat, _collector, _exit_reason = await _call_llm_streaming_with_idle(
                    the_client=_the_client,
                    model=model,
                    provider=model_spec.provider if model_spec else None,
                    msgs=_msgs_for_iter,
                    tools=_tools_for_iter,
                    extra_body=extra_body,
                    abort_event=abort_event,
                    iter_no=it,
                    task_id=task_id,
                    idle_timeout=_stream_idle,
                    first_chunk_timeout=_stream_first_chunk,
                    tool_choice=_tool_choice_for_iter,
                    label_suffix=" prefix" if _use_beta else "",
                    chunk_callback=chunk_callback,
                    stream_event_cb=stream_event_cb,
                )
                # 把 collector 挂到响应上,便于上层在 idle_timeout 时拿 partial 续写
                try:
                    _resp_compat._stream_collector = _collector  # type: ignore[attr-defined]
                    _resp_compat._stream_exit_reason = _exit_reason  # type: ignore[attr-defined]
                except Exception:
                    pass
                # 真错误抛回去
                if _exit_reason == "error":
                    _orig = getattr(_collector, "last_error", None)
                    _detail = f": {_orig}" if _orig else ""
                    raise RuntimeError(
                        f"streaming failed at iter {it}{_detail}"
                    )
                if _exit_reason == "abort":
                    raise asyncio.CancelledError("aborted during stream")
                if _exit_reason == "first_chunk_timeout":
                    # API 没回第一个 chunk → 等同 connection timeout
                    raise asyncio.TimeoutError(
                        f"first chunk not received within {_stream_first_chunk}s"
                    )
                if _exit_reason in ("idle_timeout", "main_thread_source_write", "helper_tool_call_bloat"):
                    # idle 超时/本地受控早断:已经有 partial 但中途断了。**保留 partial**,
                    # 抛特殊 TimeoutError 让上层 retry 路径决定是否续写。
                    _e = asyncio.TimeoutError(
                        f"stream exit={_exit_reason} idle_budget={_stream_idle}s "
                        f"(partial: content={len(_collector.content)} "
                        f"tool_calls={len(_collector.tool_calls)})"
                    )
                    # 把 collector 挂到异常上,retry 路径可以拼续写 message
                    try:
                        _e._stream_collector = _collector  # type: ignore[attr-defined]
                        _e._stream_exit_reason = _exit_reason  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    raise _e
                # exit_reason == "ok" → 正常返回
                return _resp_compat

            async def _call_llm_legacy_nonstream():
                """fallback: 极端 SDK 兼容性问题时退回 stream=False。
                目前不主动用,保留以备将来调试。
                """
                _the_client = (
                    beta_client()
                    if _use_beta and (model_spec is None or model_spec.provider.name == "deepseek")
                    else _cli_container[0]
                )
                _tag = _log_nonstream_prompt_shape(
                    suffix="legacy_nonstream",
                    call_messages=_msgs_for_iter,
                    call_tools=_tools_for_iter,
                )
                _resp = await _retry(
                    lambda: _the_client.chat.completions.create(
                        model=model,
                        messages=_msgs_for_iter,
                        tools=_tools_for_iter,
                        stream=False,
                        extra_body=extra_body,
                    ),
                    label=f"tools loop iter={it}{' (prefix)' if _use_beta else ''} (nonstream)",
                    provider=model_spec.provider if model_spec else None,
                )
                _record_nonstream_response_usage(_resp, tag=_tag)
                return _resp

            # B4 修复: 包一层 BadRequest(context length) 兜底重试。
            # 即使有 watchdog,极端情况下 token 估算偏差/单条 tool result 过大
            # 仍可能撞 1,048,576 上限。这里捕获 BadRequest 后调 _emergency_compact_msgs
            # 把 messages 压到 500K,然后重试当前轮 — 不丢任何已有进展。
            # 旧逻辑直接 raise 导致 orchestrator 走 _fallback_plan_from_user,
            # 把整 round2 的工作全丢光,改用 user_message 重新构造一个空 plan。
            async def _call_llm_with_compact_fallback():
                nonlocal _llm_recovery_nudges
                try:
                    return await _call_llm()
                except Exception as exc:
                    msg_str = str(exc)
                    msg_lower = msg_str.lower()
                    is_context_err = (
                        "maximum context length" in msg_lower
                        or "context_length_exceeded" in msg_lower
                        or "1048576" in msg_str
                    )

                    # ── 2026-05-02 part17:诊断改善 ──
                    # 教训(trace d30b0823 实测):round2 tool loop 在 0.468s 内崩,
                    # 但异常详情只在 Python logging 里(log.exception),debug.log 看不到 →
                    # 完全无法事后定位根因。修:把所有非 context length 异常的详情都
                    # 写到 debug.log,至少能告诉运维 "为什么崩"。
                    exc_type = type(exc).__name__
                    status_code = getattr(exc, "status_code", None)
                    stream_exit_reason = getattr(exc, "_stream_exit_reason", None)
                    if (
                        isinstance(exc, asyncio.TimeoutError)
                        and stream_exit_reason == "main_thread_source_write"
                    ):
                        debug.warn(
                            f"LLM stream controlled abort at iter {it}: "
                            f"reason={stream_exit_reason} "
                            f"will continue via partial recovery; "
                            f"msg_head={msg_str[:300]}"
                        )
                    else:
                        debug.error(
                            f"LLM API call failed at iter {it}: "
                            f"type={exc_type} status={status_code} "
                            f"is_context_err={is_context_err} "
                            f"msg_head={msg_str[:300]}"
                        )

                    if is_context_err:
                        # context length:emergency compact + retry(原行为)
                        debug.log(
                            "llm.tools.recovery_attempt",
                            f"context length err -> emergency compact + retry iter {it}"
                        )
                        new_size = _emergency_compact_msgs(msgs, target_token_budget=160_000)
                        debug.log(
                            "llm.tools.context_recovery",
                            f"compacted msgs to ~{new_size} tokens; retrying iter {it}",
                        )
                        return await _call_llm()

                    # ── 2026-05-02 part17:tools schema / 4xx 降级 ──
                    # 教训:tools 列表过大 / 某 schema 不兼容 → 整个 round2 崩 → fallback plan
                    # → round3 用空 plan 装作回应("挑了挑眉,你这是高三的作业...")。
                    # 用户提的真实任务被悄悄丢掉,极差体验。
                    # 修:对其他 4xx(非 context length),尝试一次"无 tools"降级 — 让模型
                    # 至少能输出 plan JSON,而不是整个 round2 失败。这样用户起码拿到
                    # "我看了下你这个任务,确实是 6 算法对比,我得拆给 helper 来做" 这种诚实的
                    # 计划而非伪装的人设回复。
                    is_4xx = status_code is not None and 400 <= status_code < 500
                    if isinstance(exc, asyncio.TimeoutError):
                        debug.log(
                            "llm.tools.recovery_attempt",
                            f"stream timeout -> outer timeout retry path iter {it}"
                        )
                        raise
                    is_insufficient_tool = (
                        "insufficient tool messages" in msg_lower
                        or "must be followed by tool messages" in msg_lower
                    )

                    # ── Bug A 修复: 孤儿 tool_calls 修复 ──
                    # 当消息折叠/压缩导致 assistant tool_calls 缺少对应 tool 响应时,
                    # 先尝试修复 msgs(移除孤儿 tool_calls),再重试——而非直接降级 no-tools
                    # (no-tools 路径仍会因 msgs 中的配对断裂被 API 拒绝)。
                    if is_insufficient_tool:
                        debug.log(
                            "llm.tools.recovery_attempt",
                            f"insufficient tool messages -> repairing msgs at iter {it}",
                        )
                        _repaired = _repair_tool_call_pairing(msgs)
                        if _repaired > 0:
                            try:
                                return await _call_llm()
                            except Exception as _repair_exc:
                                debug.error(
                                    f"repair retry also failed at iter {it}: "
                                    f"{type(_repair_exc).__name__}: {str(_repair_exc)[:200]}"
                                )
                        # 修复失败或无孤儿 → 尝试 no-tools 降级
                        if _tools_for_iter:
                            debug.log(
                                "llm.tools.recovery_attempt",
                                f"repair insufficient → retry without tools iter {it}",
                            )
                            try:
                                _fb_client = (
                                    beta_client()
                                    if _use_beta and (model_spec is None or model_spec.provider.name == "deepseek")
                                    else _cli_container[0]
                                )
                                _tag = _log_nonstream_prompt_shape(
                                    suffix="no_tools_fallback",
                                    call_messages=msgs,
                                )
                                _resp = await _retry(
                                    lambda: _fb_client.chat.completions.create(
                                        model=model,
                                        messages=msgs,
                                        stream=False,
                                        extra_body=extra_body,
                                    ),
                                    label=f"tools loop iter={it} (no-tools fallback)",
                                    provider=model_spec.provider if model_spec else None,
                                )
                                _record_nonstream_response_usage(_resp, tag=_tag)
                                return _resp
                            except Exception as exc2:
                                debug.error(
                                    f"no-tools fallback also failed at iter {it}: "
                                    f"{type(exc2).__name__}: {str(exc2)[:200]}"
                                )
                        raise exc

                    is_likely_schema_err = is_4xx or "schema" in msg_lower or "function" in msg_lower
                    if is_likely_schema_err and _tools_for_iter:
                        debug.log(
                            "llm.tools.recovery_attempt",
                            f"4xx/schema with compact tools -> retry without tools iter {it} "
                            f"(model will output plan directly)"
                        )
                        try:
                            _fb_client = (
                                beta_client()
                                if _use_beta and (model_spec is None or model_spec.provider.name == "deepseek")
                                else _cli_container[0]
                            )
                            _tag = _log_nonstream_prompt_shape(
                                suffix="no_tools_fallback",
                                call_messages=msgs,
                            )
                            _resp = await _retry(
                                lambda: _fb_client.chat.completions.create(
                                    model=model,
                                    messages=msgs,
                                    stream=False,
                                    extra_body=extra_body,
                                ),
                                label=f"tools loop iter={it} (no-tools fallback)",
                                provider=model_spec.provider if model_spec else None,
                            )
                            _record_nonstream_response_usage(_resp, tag=_tag)
                            return _resp
                        except Exception as exc2:
                            debug.error(
                                f"no-tools fallback also failed at iter {it}: "
                                f"{type(exc2).__name__}: {str(exc2)[:200]}"
                            )
                            raise exc

                    debug.log(
                        "llm.tools.recovery_attempt",
                        f"non-timeout LLM call failure -> inject recovery hint iter {it}",
                    )
                    _llm_recovery_nudges += 1
                    if _llm_recovery_nudges <= _llm_recovery_max_nudges:
                        _append_tool_loop_dynamic_guidance(msgs, LLM_RETRY_FAILURE_RECOVERY_HINT)
                        return await _call_llm()
                    raise RuntimeError(
                        f"LLM call failed repeatedly at iter {it}: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc

            # ── kill 响应性: LLM 调用 与 abort_event 竞速 (2026-05-02 加) ──
            # 旧路径: abort_event 只在每轮顶部检查,LLM 调用本身没有 timeout 也不响应 abort,
            # 如果 DeepSeek API 卡住,helper 永远停不下来 — kill 也只能等它自然返回。
            # 新路径: 用 asyncio.wait race LLM call vs abort_event.wait(),
            # abort 触发立即 cancel LLM call,helper 在 ~50ms 内进入 forced finalize。
            # 同时给 LLM 调用本身加 5 分钟硬 timeout(防 API hang)。
            # ── 单 LLM 调用超时上限(2026-05-03 v13 → v18.x 缩短)──
            # 旧版主路径 300s, v13 缩到 120s。实测 trace 38ff46:201 个 iter 中
            # 95% < 60s, 99% < 120s。
            # v18.x 进一步缩到 90s:实测 trace 94fe1777 的 9 次 timeout 全部都是
            # reasoning chain 死循环,90s 没出 token = 几乎确定卡死。
            # retry 几秒就回 → 单次浪费 130s → 100s,9 次省 ~4.5min。
            # **关键判定**:超时后必须区分两种情况:
            #   (a) asyncio.TimeoutError = API 真卡了 → 走 prefix retry 救场
            #   (b) asyncio.CancelledError = abort_event 被设(主线程 kill / 用户 abort)
            #       → 直接 break,不要 retry(retry 也会被立刻 cancel)
            # 2026-05-04 v19:streaming 路径自己内建 abort race + idle timeout,
            # 不再需要这里加 asyncio.wait_for total-timeout 截断。保留这个常量
            # 仅用于日志显示和退路兜底(_LLM_CALL_TIMEOUT_SEC)。
            # 实际 budget 由 _stream_idle / _stream_first_chunk 控制(见 _call_llm 内)。
            #
            # 2026-05-04 v19.1:_LLM_CALL_TIMEOUT_SEC 仅用于 log/retry 路径显示,
            # 实际 streaming 内部根据 thinking 状态选 _THINK_STREAM_* / _NOTHINK_STREAM_*。
            # 旧版 90s total timeout 已彻底移除 — 真正的卡死判定迁移到 idle level。
            _LLM_CALL_TIMEOUT_SEC = _stream_first_chunk  # 同 streaming 内部用的 budget

            async def _call_llm_racing_abort():
                # streaming 实现已经内建 abort race(每 chunk 后检查 abort_event),
                # 所以这里不再加额外 wait_for。直接调用即可。
                # _call_llm_with_compact_fallback 内部 streaming 失败会抛
                # asyncio.TimeoutError(idle 或 first_chunk)/ asyncio.CancelledError(abort)
                return await _call_llm_with_compact_fallback()

            # 调用 LLM。racing 版本同时监听 abort_event,abort 触发时立即取消调用,
            # helper kill 响应时间从"等本轮 LLM 返回"压缩到 ~50ms。
            try:
                if meta_task is not None:
                    results = await asyncio.gather(
                        _call_llm_racing_abort(), meta_task, return_exceptions=True,
                    )
                    resp = results[0]
                    if isinstance(resp, BaseException):
                        raise resp
                else:
                    resp = await _call_llm_racing_abort()
            except asyncio.CancelledError:
                # Only a signalled abort belongs to the cooperative forced-finalize
                # path. External task cancellation, such as client disconnect or
                # test-runner shutdown, must propagate so it is not misreported as
                # a degraded model plan.
                if abort_event is not None and abort_event.is_set():
                    forced_finalize_trigger = "abort_event_during_llm"
                    debug.log(
                        "llm.tools.abort.during_call",
                        f"abort_event set during LLM call at iter {it}; "
                        f"breaking to forced_finalize",
                    )
                    break
                debug.log(
                    "llm.tools.cancelled.external",
                    f"external cancellation during LLM call at iter {it}; "
                    "propagating instead of forced_finalize",
                )
                raise
            except asyncio.TimeoutError as _to_exc:
                # ── streaming idle / first_chunk timeout ──
                # 区分两种 case:
                # (A) idle_timeout: 流到一半断,_to_exc._stream_collector 有 partial
                #     → 优先尝试用 partial 续写(用户提的"直接拼接"方案)
                # (B) first_chunk_timeout: API 都没回第一个 chunk
                #     → 真卡死,走传统 thinking_disabled retry
                _stream_collector = getattr(_to_exc, "_stream_collector", None)
                _stream_exit_reason = getattr(_to_exc, "_stream_exit_reason", None)
                if _stream_exit_reason == "main_thread_source_write":
                    debug.warn(
                        f"main-thread source write aborted at iter {it}; "
                        "injecting delegation recovery hint instead of retrying the same tool call"
                    )
                    _append_tool_loop_dynamic_guidance(msgs, SOURCE_WRITE_DELEGATION_HINT)
                    _llm_recovery_nudges += 1
                    continue
                if _stream_exit_reason == "helper_tool_call_bloat":
                    _largest_tool = ""
                    _largest_args = 0
                    _helper_tool_bloat_count += 1
                    if _stream_collector is not None:
                        for _tc in getattr(_stream_collector, "tool_calls", {}).values():
                            _fn = _tc.get("function") or {}
                            _args_text = _fn.get("arguments", "") or ""
                            if len(_args_text) > _largest_args:
                                _largest_args = len(_args_text)
                                _largest_tool = str(_fn.get("name") or "")
                    debug.warn(
                        f"helper tool-call bloat aborted at iter {it}; "
                        f"task={task_id} tool={_largest_tool} args={_largest_args} "
                        f"count={_helper_tool_bloat_count}; injecting convergence hint"
                    )
                    if _helper_tool_bloat_count >= 2:
                        _bloat_hint = helper_repeated_tool_call_bloat_checkpoint(
                            iteration=it,
                            helper_kind=helper_kind,
                            tool_name=_largest_tool,
                            arg_chars=_largest_args,
                            count=_helper_tool_bloat_count,
                        )
                    else:
                        _bloat_hint = helper_tool_call_bloat_checkpoint(
                            iteration=it,
                            helper_kind=helper_kind,
                            tool_name=_largest_tool,
                            arg_chars=_largest_args,
                        )
                    _append_tool_loop_dynamic_guidance(
                        msgs,
                        _bloat_hint,
                    )
                    _llm_recovery_nudges += 1
                    continue
                # v19.1: legacy hard-mode 路径不做 partial 续写,直接降级 thinking_disabled。
                # 因为它是后备代码合成器,任务规模大且 reasoning_callback
                # 控制了三阶段切换,续写干扰阶段判定;直接降级让它走完整三阶段。
                _is_legacy_hard_path = (helper_kind == "legacy_hard")
                _partial_started_tool_call = _collector_has_streamed_tool_call(_stream_collector)
                _partial_has_text = _collector_has_continuable_text(_stream_collector)
                _partial_has_named_tool_call = _collector_has_named_tool_call(_stream_collector)
                _has_useful_partial = (
                    not _is_legacy_hard_path
                    and _stream_collector is not None
                    and _stream_collector.has_partial()
                    and _partial_has_text
                    and not _partial_started_tool_call
                )

                if not _llm_hard_timeout_retried:
                    # 2026-05-03 v18.x:累计本 task 的 timeout 数,决定 retry 用什么 reasoning
                    _td = _get_timeout_dict()
                    _td[task_id] = _td.get(task_id, 0) + 1
                    _tc = _td[task_id]
                    if _tc >= 3:
                        _retry_reasoning = "disabled"  # 强制最快路径
                    elif _tc >= 2:
                        _retry_reasoning = "high" if reasoning == "max" else "disabled"
                    else:
                        _retry_reasoning = "disabled"  # 第一次仍用现行的 disabled
                    debug.warn(
                        f"LLM stream timeout (idle={_stream_idle}s "
                        f"first={_stream_first_chunk}s) at iter {it}; "
                        f"task={task_id} cumulative_timeouts={_tc} "
                        f"has_partial={_has_useful_partial}; "
                        f"partial_tool_call={_partial_started_tool_call}; "
                        f"will try {'continuation' if _has_useful_partial else 'thinking_disabled'} retry"
                    )
                    _llm_hard_timeout_retried = True

                    if _partial_has_named_tool_call:
                        debug.log(
                            "llm.tools.partial_tool_call_discarded",
                            f"iter {it} task={task_id}: stream timed out after starting a named tool_call; "
                            "discarding the interrupted call and retrying from the previous stable messages",
                        )

                    # ── 2026-05-04 v19: idle_timeout 时优先续写 ──
                    # 用户方案:把已收到的 partial(content / tool_call arguments)
                    # 作为 assistant message 拼到 msgs 里告诉模型"上次写到这里被打断,
                    # 现在继续"。不依赖 /beta prefix API(它跟 tools 冲突)。
                    async def _call_llm_continuation_retry():
                        """用 partial 续写,reasoning 仍按当前档,模型自然接续。

                        关键设计:不强制 thinking_disabled,因为续写场景 LLM 看到自己
                        已经写了一半,只需补完剩下的 — reasoning chain 短,本来就快。
                        """
                        if _stream_collector is None or not _stream_collector.has_partial():
                            raise RuntimeError("no partial to continue from")
                        prefix_msg = _build_continuation_prefix_message(_stream_collector)
                        if prefix_msg is None:
                            raise RuntimeError("partial too thin to build prefix")
                        # 拼到 msgs 末尾(普通 assistant message,无 prefix=True 字段
                        # → 走正式 /chat/completions,可继续传 tools)
                        retry_msgs = list(msgs) + [prefix_msg]
                        debug.log(
                            "llm.tools.continuation_retry",
                            f"iter {it} task={task_id}: continuing from partial "
                            f"(content={len(_stream_collector.content)}, "
                            f"tool_calls={len(_stream_collector.tool_calls)})"
                        )
                        # 续写仍走 streaming(防再次卡死时也有 partial 可用)
                        _resp_compat, _coll, _exit = await _call_llm_streaming_with_idle(
                            the_client=_cli_container[0],
                            model=model,
                            provider=model_spec.provider if model_spec else None,
                            msgs=retry_msgs,
                            tools=_tools_for_iter,
                            extra_body=extra_body,
                            abort_event=abort_event,
                            iter_no=it,
                            task_id=task_id,
                            idle_timeout=_stream_idle,
                            first_chunk_timeout=_stream_first_chunk,
                            label_suffix=" continuation",
                            chunk_callback=chunk_callback,
                            stream_event_cb=stream_event_cb,
                        )
                        if _exit == "ok":
                            # 把续写部分前面拼上原 partial(让上层看到完整内容)
                            try:
                                if _coll.content and _stream_collector.content:
                                    _coll.content = (
                                        _stream_collector.content + _coll.content
                                    )
                                if _partial_started_tool_call and not _coll.tool_calls:
                                    debug.log(
                                        "llm.tools.partial_tool_call_discarded",
                                        f"iter {it} task={task_id}: continuation did not produce a complete "
                                        "tool_call; discarding timed-out partial tool call instead of replaying it",
                                    )
                                _coll.reasoning_content = (
                                    _stream_collector.reasoning_content
                                    + _coll.reasoning_content
                                )
                            except Exception:
                                pass
                            return _coll.to_response(model)
                        if _exit == "abort":
                            raise asyncio.CancelledError("aborted during continuation")
                        # 续写也卡 → 抛 timeout 让外层降级到 thinking_disabled
                        raise asyncio.TimeoutError(
                            f"continuation also timed out (exit={_exit})"
                        )

                    # ── thinking_disabled retry(2026-05-03 v17 修复)──
                    # 原 v12 设计用 prefix + thinking_disabled 双管齐下,但 DeepSeek API
                    # 不允许 prefix + tools 同时使用,返回 400
                    # "Function call should not be used with prefix"
                    # (实测 trace 9b3a6a6cb1fd4cd8 主线程 iter 13 + 5 个 helper 全失败)。
                    # 修复:只用 thinking_disabled,不用 prefix。
                    # v18.x:不再写死 thinking_disabled — 第一次仍用 disabled(几秒回),
                    # 第二次降一级(如 max→high),第三次以上强制 disabled。
                    # thinking_disabled 已经能让 LLM 几秒返回(跳过 reasoning chain),
                    # prefix 引导只是锦上添花,不是必需。
                    # 2026-05-04 v19:升级到 streaming 实现,不再 stream=False。
                    async def _call_llm_disabled_retry():
                        """thinking_disabled fallback retry,走 streaming 路径。

                        没有 partial 可续写时(first_chunk_timeout)走这条:把
                        reasoning 降到 _retry_reasoning(disabled/high),让模型
                        快速返回。

                        v19.1: 根据 _retry_reasoning 选 timeout:
                          - disabled: 走 NOTHINK 预算(idle 1h 兜底,只要 chunks 来就读)
                          - high/max: 仍是 think 路径,90s idle
                        """
                        _retry_extra = _thinking_extra_body(_retry_reasoning, _provider)
                        if _is_thinking_enabled(_retry_extra):
                            _retry_idle = _THINK_STREAM_IDLE_TIMEOUT
                            _retry_first_chunk = _configured_stream_first_chunk_timeout(
                                _THINK_STREAM_FIRST_CHUNK_TIMEOUT
                            )
                        else:
                            _retry_idle = _NOTHINK_STREAM_IDLE_TIMEOUT
                            _retry_first_chunk = _configured_stream_first_chunk_timeout(
                                _NOTHINK_STREAM_FIRST_CHUNK_TIMEOUT
                            )
                        _resp_compat, _coll, _exit = await _call_llm_streaming_with_idle(
                            the_client=_cli_container[0],
                            model=model,
                            provider=model_spec.provider if model_spec else None,
                            msgs=msgs,
                            tools=_tools_for_iter,
                            extra_body=_retry_extra,
                            abort_event=abort_event,
                            iter_no=it,
                            task_id=task_id,
                            idle_timeout=_retry_idle,
                            first_chunk_timeout=_retry_first_chunk,
                            label_suffix=f" no-think retry reasoning={_retry_reasoning}",
                            chunk_callback=chunk_callback,
                            stream_event_cb=stream_event_cb,
                        )
                        if _exit == "ok":
                            return _resp_compat
                        if _exit == "abort":
                            raise asyncio.CancelledError("abort during no-think retry")
                        # idle / first_chunk timeout 都是 timeout
                        raise asyncio.TimeoutError(
                            f"no-think retry exit={_exit} (idle={_retry_idle}s "
                            f"first={_retry_first_chunk}s)"
                        )

                    # ── 真正的 retry 调度 ──
                    # 优先级:
                    # 1. 已有 partial → 用 partial 续写(成本低,模型自己接续)
                    # 2. 续写也失败 OR 没有 partial → thinking_disabled 降级
                    # 3. 都失败 → break to forced_finalize
                    try:
                        if _has_useful_partial:
                            # 先尝试续写(用户提的"直接拼接"方案)
                            try:
                                resp = await _call_llm_continuation_retry()
                                debug.log(
                                    "llm.tools.continuation_retry_ok",
                                    f"continuation retry succeeded at iter {it}; "
                                    f"resumed from partial"
                                )
                            except asyncio.CancelledError:
                                raise
                            except (asyncio.TimeoutError, RuntimeError) as _cont_e:
                                debug.warn(
                                    f"continuation retry failed at iter {it}: "
                                    f"{_cont_e}; falling back to thinking_disabled"
                                )
                                # 续写也失败 → 降级 thinking_disabled
                                resp = await _call_llm_disabled_retry()
                                debug.log(
                                    "llm.tools.hard_timeout.retry_ok",
                                    f"thinking_disabled retry succeeded at iter {it} "
                                    f"after continuation failed"
                                )
                        else:
                            # 没 partial 直接走 thinking_disabled
                            resp = await _call_llm_disabled_retry()
                            debug.log(
                                "llm.tools.hard_timeout.retry_ok",
                                f"thinking_disabled retry succeeded at iter {it} "
                                f"(no partial available)"
                            )
                        # retry 成功,fall through 到下方 msg = resp.choices[0].message
                    except asyncio.CancelledError:
                        if abort_event is not None and abort_event.is_set():
                            forced_finalize_trigger = "abort_event_during_retry"
                            debug.log(
                                "llm.tools.abort.during_retry",
                                f"abort_event set during retry at iter {it} — "
                                f"breaking to forced_finalize (no more retry)",
                            )
                            break
                        debug.log(
                            "llm.tools.cancelled.external",
                            f"external cancellation during retry at iter {it}; "
                            "propagating instead of forced_finalize",
                        )
                        raise
                    except asyncio.TimeoutError as _retry_timeout:
                        # 续写 + thinking_disabled 都卡：先把失败作为恢复信号交还给模型，
                        # 让它缩小动作、续作/升级 helper 或诚实报告阻塞。不要把这类
                        # 上游/流式失败直接压成 final plan，否则 round3 容易基于失败事实
                        # 输出“已完成”。
                        debug.error(
                            f"LLM all retries timed out at iter {it}; "
                            f"injecting recovery hint instead of forced finalize"
                        )
                        _llm_recovery_nudges += 1
                        if _llm_recovery_nudges <= _llm_recovery_max_nudges:
                            _append_tool_loop_dynamic_guidance(msgs, LLM_TIMEOUT_RECOVERY_HINT)
                            continue
                        raise asyncio.TimeoutError(
                            f"LLM retries timed out repeatedly at iter {it}; "
                            f"recovery nudges={_llm_recovery_nudges}"
                        ) from _retry_timeout
                    except Exception as _retry_e:
                        # 非超时异常同样先交给模型调整一次；多次失败再向上抛，避免伪完成。
                        debug.error(
                            f"LLM retry hit unexpected error at iter {it}: "
                            f"{type(_retry_e).__name__}: {_retry_e}"
                        )
                        _llm_recovery_nudges += 1
                        if _llm_recovery_nudges <= _llm_recovery_max_nudges:
                            _append_tool_loop_dynamic_guidance(msgs, LLM_RETRY_FAILURE_RECOVERY_HINT)
                            continue
                        raise RuntimeError(
                            f"LLM retry failed repeatedly at iter {it}: "
                            f"{type(_retry_e).__name__}: {_retry_e}"
                        ) from _retry_e
                else:
                    # 已经 retry 过一次又超时(理论上 retry 路径自己 break 了,
                    # 这里是兜底防御 — 比如 retry 后 fall through 但下次 iter 又 timeout)
                    debug.error(
                        f"LLM call timeout at iter {it} (re-entered after retry); "
                        f"injecting recovery hint"
                    )
                    _llm_recovery_nudges += 1
                    if _llm_recovery_nudges <= _llm_recovery_max_nudges:
                        _append_tool_loop_dynamic_guidance(msgs, LLM_REPEAT_TIMEOUT_RECOVERY_HINT)
                        continue
                    raise asyncio.TimeoutError(
                        f"LLM call timed out repeatedly at iter {it}; "
                        f"recovery nudges={_llm_recovery_nudges}"
                    )
            msg = resp.choices[0].message

            # ── prefix 续写合并(2026-05-03 v12)──
            # 如果上一轮调用走了 prefix(_use_beta=True / hard_timeout retry prefix),
            # response.message.content 只是"续写"部分,不含 prefix。
            # 我们要把 msgs[-1](prefix assistant)的 content 拼到 msg.content 前面,
            # 然后**移除** msgs[-1](避免下面 _serialize 时产生两个连续 assistant)。
            #
            # 注意:retry 路径用的是临时 short_msgs(局部变量),不会影响外层 msgs;
            # 但 P1A 路径(delegate.py 注入的 prefix)直接修改 msgs,需要这里清理。
            if (
                len(msgs) > 0
                and isinstance(msgs[-1], dict)
                and msgs[-1].get("role") == "assistant"
                and msgs[-1].get("prefix") is True
            ):
                _prefix_content = msgs[-1].get("content", "") or ""
                _new_content = getattr(msg, "content", "") or ""
                # 续写内容拼到 prefix 后面,得到完整 assistant message
                # 注意:msg 是 SDK 对象,直接改它的 content 字段
                try:
                    msg.content = _prefix_content + _new_content
                except (AttributeError, TypeError):
                    # SDK 对象不让改 → 用 dict 覆盖(后续 _serialize_assistant_message 兼容)
                    pass
                # 移除原 prefix msg(它的 content 已经被合并进 msg)
                msgs.pop()
                debug.log(
                    "llm.tools.prefix.merged",
                    f"merged prefix ({len(_prefix_content)} chars) into "
                    f"continuation ({len(_new_content)} chars) at iter {it}",
                )

            tool_calls = getattr(msg, "tool_calls", None) or []

            # Dynamic guidance is cleared after it becomes an action. If the
            # model produced no tool call, the task/correction may still be
            # unresolved, so keep the hint until a later action or final exit.
            _clear_transient_tool_loop_guidance(
                msgs,
                reason=f"after_model_response_iter_{it}",
                action_taken=bool(tool_calls),
            )

            if tool_calls:
                _clear_unused_schema_retry_guidance_after_response(
                    tool_calls,
                    trace_id=debug.current_trace_id(),
                    reason=f"after_model_response_iter_{it}",
                )
            content = msg.content or ""
            if tool_calls and _force_delegate_for_retry_gap:
                called_names = {
                    str(getattr(getattr(tc, "function", None), "name", "") or "")
                    for tc in tool_calls
                }
                if "delegate" in called_names:
                    _force_delegate_for_retry_gap = False

            if not tool_calls:
                if (
                    helper_kind is None
                    and tools
                    and _unresolved_project_write_blocks
                    and not _guidance.has_fired("unresolved_project_write_block")
                    and not _check_aborted()
                ):
                    _facts = list(_unresolved_project_write_blocks.values())[:4]
                    msgs.append(_serialize_assistant_message(msg))
                    _append_tool_loop_dynamic_guidance(
                        msgs,
                        (
                            "[SYSTEM_HINT/unresolved_project_write_block]\n"
                            "Recent tool facts show attempted project-file writes that did not create real project files. "
                            "These are facts, not an automatic decision. Before finalizing, compare them with the active "
                            "task: if one is still a requested deliverable, create it with the suggested project apply "
                            "tool or another verified route; if no project file is required, state that evidence in the "
                            "plan/final response; if it remains unresolved, report it as incomplete.\n\n"
                            + json.dumps({"unresolved_project_write_blocks": _facts}, ensure_ascii=False, indent=2)
                            + "\n\n项目文件写入曾被拦截且尚未被真实项目创建解决；最终前需按当前任务判断创建、说明不需要，或报告未完成。"
                        ),
                    )
                    _guidance.should_emit("unresolved_project_write_block")
                    debug.log(
                        "llm.tools.unresolved_project_write_block",
                        f"injected before final: paths={list(_unresolved_project_write_blocks)[:4]}",
                    )
                    continue
                if (
                    helper_kind is None
                    and tools
                    and not _guidance.has_fired("browser_repro_final_evidence")
                    and not _browser_repro_evidence_seen
                    and not _active_task_has_browser_evidence_fact(msgs)
                    and _active_task_explicitly_requires_browser_repro(msgs)
                    and not _check_aborted()
                ):
                    msgs.append(_serialize_assistant_message(msg))
                    _guidance.should_emit("browser_repro_final_evidence")
                    _append_tool_loop_dynamic_guidance(
                        msgs,
                        (
                            "[SYSTEM_HINT/browser_reproduction_evidence_missing_before_final]\n"
                            "The active task explicitly asks for browser/host-browser reproduction or verification. "
                            "Current loop facts do not show browser-family evidence from an actual browser tool or from "
                            "a command running Playwright, Puppeteer, Selenium, Chromium, Chrome, Firefox, or WebKit against "
                            "the target. Source reads, static diagnosis, docs file reads, curl/plain HTTP checks, and final text "
                            "claims are not the same evidence. This is a fact checkpoint, not a forced decision: decide whether "
                            "to collect browser evidence now, delegate browser-automation evidence collection to a helper with command "
                            "capability, or state the exact browser-evidence gap as PARTIAL in the final JSON.\n\n"
                            "当前任务要求浏览器/宿主浏览器证据，但当前工具链未见 browser-family 证据；源码读取、静态诊断、docs 文件、curl/普通 HTTP 和最终文字声明不等同。"
                            "由模型决定补证据、派具备命令能力的 helper 补证据，或在最终 JSON 中按 PARTIAL 说明缺口。"
                        ),
                    )
                    debug.log(
                        "llm.tools.finalize.browser_repro_evidence_missing",
                        f"injected before final at iter {it}",
                    )
                    continue
                if (
                    helper_kind is None
                    and tools
                    and not _guidance.has_fired("main_final_contract_snapshot")
                    and not _check_aborted()
                    # 2026-06-10 Round 7: skip the snapshot interruption when the
                    # model already produced a parseable ResponsePlan. Injecting
                    # the snapshot AFTER a finished plan forced a rewrite turn
                    # that usually came back as self-assessment prose, costing
                    # 2-3 extra full-context turns plus a cleanup LLM call per
                    # task (cross-repo 20260610_154444 iters 13-15, ~8.5s).
                    and not _content_is_complete_response_plan(content)
                ):
                    _snapshot = _current_task_contract_snapshot()
                    if _snapshot and "current plan" in _snapshot:
                        msgs.append(_serialize_assistant_message(msg))
                        _append_tool_loop_dynamic_guidance(
                            msgs,
                            (
                                "[SYSTEM_HINT/main_final_contract_snapshot]\n"
                                "Before finalizing, compare the proposed completion against these active-task facts. "
                                "These are facts, not a forced decision: if the task has changed, state the evidence; "
                                "if an old acceptance point is still part of the active contract and is not covered, "
                                "either do a targeted check/repair or report the unresolved point.\n\n"
                                f"{_snapshot}\n\n"
                                "If no more tools are needed, return only one valid ResponsePlan JSON object with fields such as "
                                "intent, key_points, tone, length_hint, internal_note, and deliverables. Do not output prose, "
                                "markdown, or a completion checklist outside JSON.\n\n"
                                "最终前对照当前任务事实；如任务已变更说明证据，仍属当前契约但未覆盖的验收点需说明、补查或续作。"
                                "若无需继续调用工具，只输出一个合法 ResponsePlan JSON，不输出散文或 JSON 外清单。"
                            ),
                        )
                        _guidance.should_emit("main_final_contract_snapshot")
                        debug.log(
                            "llm.tools.final_contract_snapshot",
                            f"injected before final at iter {it}",
                        )
                        continue
                if (
                    helper_kind is None
                    and tools
                    and _main_last_successful_verifier_iter >= 0
                    and _main_last_project_state_mutation_seq > _main_last_successful_verifier_seq
                    and not _main_last_project_state_mutation_helper_owned
                    and (
                        _main_post_apply_verifier_checkpoint_nudges
                        < _main_post_apply_verifier_checkpoint_max_nudges
                    )
                    and not _check_aborted()
                ):
                    _post_apply_pair = (
                        _main_last_successful_verifier_iter,
                        _main_last_project_state_mutation_iter,
                        _main_last_successful_verifier_seq,
                        _main_last_project_state_mutation_seq,
                    )
                    if _main_post_apply_verifier_checkpoint_pair != _post_apply_pair:
                        _main_post_apply_verifier_checkpoint_pair = _post_apply_pair
                        _main_post_apply_verifier_checkpoint_nudges = 0
                    _main_post_apply_verifier_checkpoint_nudges += 1
                    msgs.append(_serialize_assistant_message(msg))
                    _append_tool_loop_dynamic_guidance(
                        msgs,
                        (
                            "[SYSTEM_HINT/post_apply_verification_fact]\n"
                            "Current tool evidence shows a successful verifier/check command before a later project/workspace state mutation. "
                            "The current loop evidence does not show a later successful verifier/check command after that latest mutation. "
                            "This is a fact checkpoint, not a forced decision: if the final claim depends on verifier-visible project state, "
                            "run the relevant verifier/check after the latest mutation or record concrete evidence for why the earlier check still applies. "
                            "If the later mutation is unrelated to the checked state, state that evidence in the final JSON.\n\n"
                            f"latest_successful_verifier_iter={_main_last_successful_verifier_iter}; "
                            f"latest_project_state_mutation_iter={_main_last_project_state_mutation_iter}; "
                            f"latest_successful_verifier_seq={_main_last_successful_verifier_seq}; "
                            f"latest_project_state_mutation_seq={_main_last_project_state_mutation_seq}; "
                            f"latest_project_state_mutation_path={_main_last_project_state_mutation_path}; "
                            f"checkpoint_nudge={_main_post_apply_verifier_checkpoint_nudges}/"
                            f"{_main_post_apply_verifier_checkpoint_max_nudges}\n\n"
                            "事实检查点：成功验收早于后续项目/工作区状态变更；若最终结论依赖变更后的状态，应复验或说明旧验收仍适用的证据。"
                        ),
                        refresh_same_tag=True,
                    )
                    debug.log(
                        "llm.tools.finalize.post_apply_verification_checkpoint",
                        (
                            f"verifier_iter={_main_last_successful_verifier_iter}; "
                            f"mutation_iter={_main_last_project_state_mutation_iter}; "
                            f"verifier_seq={_main_last_successful_verifier_seq}; "
                            f"mutation_seq={_main_last_project_state_mutation_seq}; "
                            f"mutation_path={_main_last_project_state_mutation_path}; "
                            f"nudge={_main_post_apply_verifier_checkpoint_nudges}/"
                            f"{_main_post_apply_verifier_checkpoint_max_nudges}"
                        ),
                    )
                    continue
                if (
                    helper_kind is None
                    and tools
                    and _main_acceptance_script_facts
                    and not _check_aborted()
                    and _main_verifier_visible_artifact_nudges < (
                        1 if _main_existing_verifier_visible_artifacts else _main_verifier_visible_artifact_max_nudges
                    )
                    and (
                        not _main_verifier_command_success_seen
                        and (
                            not _main_project_state_mutation_seen
                            and not _main_existing_verifier_visible_artifacts
                            or not _main_verifier_command_seen
                        )
                    )
                ):
                    _main_verifier_visible_artifact_nudges += 1
                    _verifier_lines = []
                    for _path, _fact in list(_main_acceptance_script_facts.items())[-6:]:
                        _lists = _fact.get("literal_string_lists") if isinstance(_fact, dict) else None
                        _list_bits = []
                        if isinstance(_lists, list):
                            for _item in _lists[:3]:
                                if not isinstance(_item, dict):
                                    continue
                                _strings = _item.get("strings") or []
                                if isinstance(_strings, list):
                                    _shown = ", ".join(repr(str(s)) for s in _strings[:5])
                                    _list_bits.append(f"{_item.get('name')}=[{_shown}]")
                        _source = str(_fact.get("source") or "").strip() if isinstance(_fact, dict) else ""
                        if isinstance(_fact, dict) and _fact.get("scans_project_or_workspace_text") is True:
                            _basis = "read script source; appears to scan project/workspace text files"
                        elif _source:
                            _basis = f"discovered by {_source}; script body not read in this loop"
                        else:
                            _basis = "project-provided acceptance/check script path"
                        _suffix_parts = [_basis]
                        if _list_bits:
                            _suffix_parts.append(f"checks: {'; '.join(_list_bits)}")
                        _suffix = f" ({'; '.join(_suffix_parts)})"
                        _verifier_lines.append(f"- {_path}{_suffix}")
                    msgs.append(_serialize_assistant_message(msg))
                    _append_tool_loop_dynamic_guidance(
                        msgs,
                        (
                            "[SYSTEM_HINT/verifier_visible_artifact_fact]\n"
                            "Current tool evidence includes project-provided acceptance/check script facts:\n"
                            + "\n".join(_verifier_lines)
                            + "\n\n"
                            f"Current loop facts: verifier-visible project/workspace mutation seen={_main_project_state_mutation_seen}; "
                            f"existing verifier-visible artifact path seen={sorted(_main_existing_verifier_visible_artifacts)[:6]}; "
                            f"verifier/check command run seen={_main_verifier_command_seen}. "
                            "A chat-only final response is not verifier-visible project/workspace state. "
                            "This is a fact checkpoint, not a forced decision: compare the active user request and current verifier facts. "
                            "If these scripts are acceptance evidence for the active task, create or update verifier-visible state, "
                            "then run the relevant verifier/check command before finalizing. If they are irrelevant to the active task, "
                            "record the evidence for that decision in the final JSON.\n\n"
                            "事实检查点：当前工具证据中存在项目验收/检查脚本；纯聊天回复不是项目/工作区状态，由模型结合当前任务判断是否需要写入脚本可见状态并运行验收。"
                        ),
                    )
                    debug.log(
                        "llm.tools.finalize.verifier_visible_artifact_checkpoint",
                        (
                            f"nudge={_main_verifier_visible_artifact_nudges}/"
                            f"{_main_verifier_visible_artifact_max_nudges}; "
                            f"mutated={_main_project_state_mutation_seen}; "
                            f"verifier_run={_main_verifier_command_seen}; "
                            f"verifier_success={_main_verifier_command_success_seen}; "
                            f"verifiers={list(_main_acceptance_script_facts.keys())[-6:]}"
                        ),
                    )
                    continue
                pending_retry_tasks = [
                    tid for tid in _retryable_next_action_tasks
                    if tid and tid not in _retryable_next_action_spawned
                ]
                committed_files_now = _committed_files_from_recent_tools(msgs)
                blocking_retry_tasks = _pending_retry_tasks_blocking_finalize(
                    pending_retry_tasks,
                    _retryable_next_action_facts,
                    committed_files_now,
                )
                if (
                    helper_kind is None
                    and tools
                    and blocking_retry_tasks
                    and not _check_aborted()
                ):
                    retry_lines = []
                    for fact in _retryable_next_action_facts[-4:]:
                        tid = fact.get("task_id", "?")
                        if tid not in blocking_retry_tasks:
                            continue
                        params = fact.get("params") or {}
                        missing = _round3_gap_missing_items(fact.get("outputs_missing"))
                        terminal_reason = _neutral_round3_gap_text(fact.get("terminal_reason"))
                        next_action_type = _neutral_round3_gap_text(fact.get("next_action_type"))
                        params_json = _neutral_round3_gap_text(json.dumps(params, ensure_ascii=False))
                        retry_lines.append(
                            f"- {tid}: {terminal_reason} / "
                            f"{next_action_type} / "
                            f"missing={missing} / "
                            f"retry_params={params_json}"
                        )
                    if not retry_lines:
                        retry_lines = [f"- {tid}: resume or escalate the same task_id before finalizing" for tid in blocking_retry_tasks]
                    msgs.append(_serialize_assistant_message(msg))
                    should_block_finalize = (
                        _retryable_next_action_forced_nudges
                        < _retryable_next_action_max_nudges
                    )
                    if should_block_finalize:
                        _retryable_next_action_forced_nudges += 1
                        current_nudge = _retryable_next_action_forced_nudges
                        hint_content = retry_required_before_final(
                            current_nudge,
                            _retryable_next_action_max_nudges,
                            retry_lines,
                        )
                    else:
                        current_nudge = _retryable_next_action_forced_nudges
                        hint_content = retry_still_required_before_final(retry_lines)
                    debug.log(
                        "llm.tools.finalize.retry_blocked",
                        f"model tried to finalize before retrying {blocking_retry_tasks}; "
                        f"committed_files={committed_files_now[:6]}; "
                        f"nudge {current_nudge}/{_retryable_next_action_max_nudges}",
                    )
                    _append_tool_loop_dynamic_guidance(msgs, hint_content)
                    if should_block_finalize:
                        continue
                    debug.log(
                        "llm.tools.finalize.retry_checkpoint_exhausted",
                        (
                            f"allowing model decision after "
                            f"{_retryable_next_action_max_nudges} recovery checkpoints; "
                            f"blocking_tasks={blocking_retry_tasks}"
                        ),
                    )
                if pending_retry_tasks and committed_files_now and not blocking_retry_tasks:
                    debug.log(
                        "llm.tools.finalize.retry_allowed_after_commit",
                        (
                            f"pending retry tasks {pending_retry_tasks} no longer block finalization; "
                            f"committed files={committed_files_now[:6]}"
                        ),
                    )
                if (
                    helper_kind is None
                    and tools
                    and _delegate_gap_facts
                    and not _delegate_gap_facts_injected
                    and not _check_aborted()
                ):
                    msgs.append(_serialize_assistant_message(msg))
                    _delegate_gap_facts_injected = True
                    _gap_lines = []
                    for fact in _delegate_gap_facts[-6:]:
                        missing = _round3_gap_missing_items(fact.get("outputs_missing"))
                        missing_text = f"; missing={missing}" if missing else ""
                        gap_kind = _neutral_round3_gap_text(fact.get("gap_kind"))
                        terminal_reason = _neutral_round3_gap_text(fact.get("terminal_reason"))
                        if fact.get("nonblocking_tts_generation_fact"):
                            _gap_lines.append(
                                f"- voice output status {fact.get('task_id')}: "
                                f"{gap_kind} / {terminal_reason}{missing_text}"
                            )
                            continue
                        _gap_lines.append(
                            f"- processing gap {fact.get('task_id')} ({_neutral_round3_gap_text(fact.get('kind'))}): "
                            f"{gap_kind} / {terminal_reason}{missing_text}"
                        )
                    _append_tool_loop_dynamic_guidance(
                        msgs,
                        (
                            "[SYSTEM_HINT/processing_gap_facts]\n"
                            "Some work produced nonblocking gap facts. These facts do not force retry or stop, "
                            "but they should remain visible in the final JSON if relevant to the user's requested coverage:\n"
                            + "\n".join(_gap_lines)
                            + "\n\n"
                            "If the main thread independently covered the task, say so as the evidence basis; do not hide processing gaps when they affect coverage or confidence.\n"
                            "处理缺口事实不强制续作；如主流程已独立覆盖，应说明证据边界。"
                        ),
                    )
                    debug.log(
                        "llm.tools.helper_gap_facts_injected",
                        f"count={len(_delegate_gap_facts)} iter={it}",
                    )
                    continue
                # 2026-05-07: 首轮零工具调用 = 模型输出文字计划而未动手。
                # 追加一条强指令让模型调用工具，再给一次迭代机会。
                if it == 1 and require_first_tool_call and tools:
                    debug.log(
                        "llm.tools.first_iter_noop",
                        f"iter 1 produced 0 tool calls — injecting nudge and retrying",
                    )
                    msgs.append(_serialize_assistant_message(msg))
                    _append_tool_loop_dynamic_guidance(
                        msgs,
                        (
                            "No tool has been called yet. Start with the appropriate tool call, then produce the final JSON after tool work is complete.\n\n"
                            "需要工具的任务先调用工具，再输出最终 JSON。"
                        ),
                    )
                    continue
                if (
                    tools
                    and require_first_tool_call
                    and _executed_tool_result_count == 0
                    and not _guidance.has_fired("empty_evidence")
                    and len(content.strip()) > 20
                ):
                    _guidance.should_emit("empty_evidence")
                    debug.log(
                        "llm.tools.no_evidence_finalize_blocked",
                        f"iter {it} tried to finalize before any tool result; injecting evidence nudge",
                        content[:500],
                    )
                    msgs.append(_serialize_assistant_message(msg))
                    _append_tool_loop_dynamic_guidance(
                        msgs,
                        (
                            "[SYSTEM_HINT/evidence_required]\n"
                            "This round has no tool evidence yet. For requests that require reading, counting, verifying, "
                            "modifying, or producing artifacts, get checkable evidence with the appropriate tool before "
                            "making concrete claims. If the request truly does not need tools, state the basis and finish.\n\n"
                            "涉及读取、统计、验证或产物时先取得工具证据。"
                        ),
                    )
                    continue
                if tools and _looks_like_unparsed_tool_markup(content):
                    debug.log(
                        "llm.tools.unparsed_tool_markup",
                        f"iter {it} emitted tool-call markup as text; retrying with format nudge",
                        content[:500],
                    )
                    _append_tool_loop_dynamic_guidance(
                        msgs,
                        (
                            "The previous output looked like a tool call written as plain text. Use the official tool_calls "
                            "interface for tool use; if no tool is needed, output only valid final JSON.\n\n"
                            "工具调用要走正式接口；不需要工具时只输出最终 JSON。"
                        ),
                    )
                    continue
                # P2a 修复（trace 063b5abd）：lite 模型在决定停手时常在 JSON 前
                # 写大量"让我想想…""等一下…"等思考文本。_parse_json_strict 通常能挖
                # 出 JSON 但偶尔挖偏。检测到非干净 JSON 开头时再问一次（强制 JSON 模式）。
                #
                # 2026-05-02 Bug G 修:实测 trace e4eeb133 一个任务触发 284 次 cleanup,
                # 每次额外 1 次 LLM 调用(5-30s/次)。改造为两步:
                #   1. 先本地尝试提取(strip markdown 包裹 / 找第一个 {...} 块 + json.loads 验证)
                #   2. 本地提取失败才降级到 LLM cleanup
                # 实测 80%+ 的 cleanup 都能本地解决(模型只是写了前缀文字 + ```json 包裹)。
                stripped = content.strip().lstrip("`").lstrip()
                looks_like_json = stripped.startswith("{") or stripped.startswith("```json")
                if finalize_kind == "text_summary":
                    debug.log(
                        "llm.tools.final.text_summary",
                        f"no more tool calls (iter {it}); returning helper text summary without JSON cleanup",
                        content,
                    )
                    _maybe_clear_stale_upgrade(
                        upgrade_signal, successful_after_signal,
                        natural_stop=True,
                    )
                    _clear_transient_guidance_before_return("return_text_summary")
                    return (content, msgs)
                if not looks_like_json and len(content) > 30:
                    extracted = _try_extract_json_locally(content)
                    if extracted is not None:
                        debug.log(
                            "llm.tools.finalize.local_extract",
                            f"extracted clean JSON locally (saved 1 LLM call); "
                            f"original len={len(content)} → extracted len={len(extracted)}",
                        )
                        content = extracted
                    else:
                        if _looks_like_final_plan_self_assessment(content):
                            debug.log(
                                "llm.tools.finalize.self_assessment_retry",
                                "model stopped with a contract/self-assessment instead of final JSON; "
                                "asking same context for the actual plan facts",
                                content[:500],
                            )
                            _append_tool_loop_dynamic_guidance(
                                msgs,
                                (
                                    "The previous assistant text evaluated whether a final JSON plan would satisfy the contract, "
                                    "but it did not provide a final JSON that carries the answer. Return the actual final JSON now. "
                                    "key_points must contain the answer content itself: for analysis/audit tasks the findings and "
                                    "evidence; for requested structured output the complete content verbatim as one intact string. "
                                    "Earlier tool-loop assistant text never reaches the user — content referenced as 'produced above' "
                                    "must be reproduced in key_points in full. "
                                    "If coverage is partial or a claim is only a hypothesis, state that in key_points. "
                                    "No more tools are needed unless a specific missing fact must be checked.\n\n"
                                    "上一条只是自评或引用上文；key_points 必须携带答案本体，上文输出不会到达用户。"
                                ),
                            )
                            continue
                        debug.log(
                            "llm.tools.finalize.cleanup",
                            f"non-JSON tail (len={len(content)}); local extract failed, "
                            f"requesting clean JSON via LLM",
                            content[:300],
                        )
                        # 2026-05-15 修(trace 83f2d643 12:17):旧版用
                        #   msgs + [serialized_assistant] + [trailing system]
                        # 让模型 "continue conversation 但只出 JSON"。失败模式实测:
                        #   - strict (response_format=json_object + prefill): 模型放弃,
                        #     输出 131 字符空白
                        #   - bare: 模型继续散文模式, 又输出 240 字符散文
                        # 然后 `content = cleanup ... or content` **用空白覆盖了原散文**,
                        # 下游所有 fallback (_plan_dict_from_round2_text 的 salvage)
                        # 都没法从空白里 salvage 出任何东西。
                        #
                        # 修法 1:用全新的隔离对话做 format conversion (没有 round2
                        #         system prompt 干扰, 没有散文上下文,模型只能照 system
                        #         instruction 输出 JSON)
                        # 修法 2:cleanup 完出来后 **验证产物**,如果还是 non-JSON / 比
                        #         原 content 短一半 / 全空白,**保留原 content**, 让
                        #         下游 fallback 至少能从原 content 里 salvage 出内容。
                        from app.llm import aux_prompts as _aux
                        cleanup_msgs = [
                            {"role": "system", "content": _aux.JSON_CONVERTER_SYSTEM},
                            {"role": "user", "content": _aux.JSON_CONVERTER_USER_TEMPLATE.format(content=content)},
                        ]
                        try:
                            cleanup_extra_body = {
                                **(extra_body or {}),
                                "response_format": {"type": "json_object"},
                            }
                            cleanup_tag = _log_nonstream_prompt_shape(
                                suffix="final_cleanup",
                                call_messages=cleanup_msgs,
                            )
                            cleanup = await _retry(
                                lambda: _cli_container[0].chat.completions.create(
                                    model=model, messages=cleanup_msgs, stream=False,
                                    extra_body=cleanup_extra_body,
                                ),
                                label=f"tools final cleanup iter={it}",
                                provider=model_spec.provider if model_spec else None,
                            )
                            _record_nonstream_response_usage(cleanup, tag=cleanup_tag)
                            cleanup_content = (cleanup.choices[0].message.content or "").strip()
                            # 验收:cleanup 必须产生合理的 JSON-looking string。
                            #   - 必须以 `{` 开头
                            #   - 至少有一对引号(避免 "{}"  这种 degenerate 输出)
                            # 任一不满足 → **保留原 content**, 让下游 salvage 处理。
                            fidelity_ok, fidelity_reason = _cleanup_preserves_exact_tokens(
                                content,
                                cleanup_content,
                            )
                            if (cleanup_content.startswith("{")
                                    and '"' in cleanup_content
                                    and len(cleanup_content) >= 10
                                    and fidelity_ok):
                                content = cleanup_content
                            else:
                                debug.log(
                                    "llm.tools.finalize.cleanup.rejected",
                                    f"cleanup output unfit (len={len(cleanup_content)}, "
                                    f"starts_with_brace={cleanup_content[:1]!r}, "
                                    f"fidelity={fidelity_ok} {fidelity_reason}); "
                                    f"keeping original content for downstream salvage",
                                )
                        except Exception:
                            log.exception("finalize cleanup failed; falling back to original content")

                if helper_kind is None and tools and _looks_like_final_plan_self_assessment(content):
                    debug.log(
                        "llm.tools.finalize.self_assessment_retry",
                        "model stopped with a JSON contract/self-assessment instead of a ResponsePlan; "
                        "asking same context for actual answer facts",
                        content[:500],
                    )
                    msgs.append(_serialize_assistant_message(msg))
                    _append_tool_loop_dynamic_guidance(
                        msgs,
                        (
                            "The previous assistant output evaluated completion or acceptance, but it was not a final "
                            "ResponsePlan that carries the answer. Return the actual final JSON now with fields like intent, "
                            "key_points, tone, length_hint, avoid, callbacks, internal_note, and deliverables. key_points must "
                            "contain the answer content itself: for analysis/audit tasks the findings and evidence; for "
                            "requested structured output (scoring JSON, code, CSV) the complete content verbatim as one intact "
                            "string. Earlier tool-loop assistant text never reaches the user, so referring to content 'produced "
                            "above' loses it — reproduce it in key_points in full. Use existing evidence; call a tool only for "
                            "one narrow missing fact.\n\n"
                            "上一条是自评或仅引用了上文内容；最终 ResponsePlan 的 key_points 必须携带答案本体（结构化输出需完整原文），上文输出不会到达用户。"
                        ),
                    )
                    continue

                if (
                    helper_kind is None
                    and tools
                    and _executed_tool_result_count > 0
                    and not _guidance.has_fired("audit_evidence_final_check")
                    and _response_plan_needs_audit_evidence_review(content)
                    and not _check_aborted()
                ):
                    _guidance.should_emit("audit_evidence_final_check")
                    _audit_evidence_final_check_original_content = content
                    msgs.append(_serialize_assistant_message(msg))
                    _append_tool_loop_dynamic_guidance(
                        msgs,
                        (
                            "[SYSTEM_HINT/audit_evidence_final_review]\n"
                            "The previous assistant message is a final ResponsePlan for an audit/review/optimization task. "
                            "Before accepting it, re-check each mechanism, optimization, risk, or root-cause claim against the "
                            "tool evidence already in this conversation. This is an evidence-boundary review, not a forced stop "
                            "or retry: return a revised final JSON if any claim lacks direct implementation, caller/callee, "
                            "data-flow, command-output, or line/path evidence. If fewer strong findings are supported, include "
                            "fewer definite findings and label weak leads with the missing direct evidence. If the JSON is already "
                            "properly evidence-bounded, return the same JSON. Preserve the substantive answer facts that are still "
                            "supported; do not replace the answer with a coverage checklist or a note that only says which files/symbols "
                            "were read. Use the existing evidence for this review; do not "
                            "start broad additional exploration. Call a tool only for one narrow missing fact that directly blocks "
                            "correcting or downgrading a specific claim.\n\n"
                            "最终前证据边界复核：主要用已有工具证据；不要展开新一轮大范围探索。证据不足的优化/风险/根因降级为假设或减少确定结论；保留仍有证据支撑的实际答案，不要退化成覆盖清单。"
                        ),
                    )
                    debug.log(
                        "llm.tools.audit_evidence_final_review",
                        f"injected before accepting audit final JSON at iter {it}",
                        content[:1200],
                    )
                    continue
                if _audit_review_content_regressed(
                    _audit_evidence_final_check_original_content,
                    content,
                ):
                    debug.log(
                        "llm.tools.audit_evidence_final_review_rejected",
                        "review output regressed into a checklist; keeping pre-review ResponsePlan",
                        {
                            "previous_chars": len(_audit_evidence_final_check_original_content),
                            "current_chars": len(content or ""),
                        },
                    )
                    content = _audit_evidence_final_check_original_content
                debug.log(
                    "llm.tools.final",
                    f"no more tool calls (iter {it})",
                    content,
                )
                content = _merge_delegate_gap_facts_into_response_plan_json(content)
                # Bug 2: 撤销 stale upgrade signal —
                # 如果升级信号被设过 True,但之后模型连续成功了若干轮,说明那是
                # snapshot lag(meta_judge 看到的"卡住"快照已被后续恢复推翻)。
                # 触发条件:signal=True 且之后 ≥8 轮工具调用全部成功(B5 提高阈值),
                # 或模型自然停手且 plan 不是降级语义(B5 严格化)。
                # ── 从 content 提取 intent 给清除函数判断是否"放弃式停手" ──
                _intent_for_clear = ""
                try:
                    _stripped = content.strip().lstrip("`").lstrip()
                    if _stripped.startswith("{"):
                        _parsed = json.loads(_stripped)
                        if isinstance(_parsed, dict):
                            _intent_for_clear = str(_parsed.get("intent", ""))
                except Exception:
                    pass
                _maybe_clear_stale_upgrade(
                    upgrade_signal, successful_after_signal,
                    natural_stop=True,
                    plan_intent=_intent_for_clear,
                )
                _clear_transient_guidance_before_return("return_natural_final")
                return (content, msgs)

            debug.log(
                "llm.tools.calls",
                f"{len(tool_calls)} parallel call(s)",
                [
                    {"name": tc.function.name, "args_raw": tc.function.arguments}
                    for tc in tool_calls
                ],
            )

            # #13 修:per-tool 调用次数限制
            # 防止模型死循环调同一工具(trace 里见过模型 connected edit_file 失败 30+ 次仍重试)
            # 单 tool 累计 ≥ _PER_TOOL_CAP 时,在 tool result 里塞警告让模型转向其他方法。
            # 不直接 break loop——模型可能自然停止或换工具。
            for tc in tool_calls:
                _tool_call_counts[tc.function.name] = _tool_call_counts.get(tc.function.name, 0) + 1
                if tc.function.name == "delegate":
                    try:
                        _delegate_args, _delegate_json_err, _ = _normalize_tool_call_args_for_dispatch(
                            tc.function.arguments or "{}"
                        )
                        if _delegate_json_err is None:
                            spawned_ids = _delegate_spawn_task_ids(_delegate_args)
                            covered_retry_ids = spawned_ids.intersection(
                                set(_retryable_next_action_tasks)
                            )
                            if covered_retry_ids:
                                _retryable_next_action_spawned.update(covered_retry_ids)
                                debug.log(
                                    "llm.tools.next_action_retry_spawned",
                                    f"delegate call covers retry tasks: {sorted(covered_retry_ids)}",
                                )
                    except Exception:
                        pass

            # 把 assistant 的 tool_call 消息追加到历史。
            # 官方文档强制要求：tool_call 轮次必须完整回传 reasoning_content
            # （即使为空字符串），否则 API 返回 400。_serialize_assistant_message
            # 已经处理了这个边界。
            msgs.append(_serialize_assistant_message(msg))

            # 并行执行所有工具调用
            async def _run(tc):
                # 2026-05-02 part8:每个 tool result 顶层注入 _ts_iso / _started_at_iso /
                # _tool_elapsed_sec 三个时间戳字段,让模型直接判断"X 工具跑了多久 / 这是
                # 多久之前的结果",不用心算。比如对比同批 helper 的 running_for_sec 能直接
                # 看出谁跑久了;再比如多个 workspace.run 的 _tool_elapsed_sec 一对比就知道
                # 哪个命令耗时异常。
                nonlocal _last_commit_to_main_iter, lite, model, reasoning, extra_body
                _t_start = time.monotonic()
                _t_start_iso = _now_iso()
                # 2026-05-12 P39: 修复 LLM 输出 args 时未 escape 内层引号导致 args={} 的 bug
                # 病因(实测 18:14 trace): LLM 输出
                #   "prompt": "...报告\"SSL 自测通过\"..."  正确
                #   "prompt": "...报告"SSL 自测通过"..."  错误(未 escape)
                # 主线程被告知 "0 tasks" → 误以为自己传空数组, 重派同样错的 JSON, 死循环。
                _args_raw_str = tc.function.arguments or "{}"
                args, _json_err, _args_repaired = _normalize_tool_call_args_for_dispatch(_args_raw_str)
                pending_runtime_facts: list[dict] = []
                _dispatch_blocked_by_json = False
                if _args_repaired:
                    debug.log(
                        f"llm.tools.json_repaired",
                        f"P39: tool args normalized before dispatch (len={len(_args_raw_str)})",
                    )
                if _json_err is not None:
                    # 2026-05-12 P39 加强 (实测 21:05 trace): 检测错误类型
                    # 病因: P39 早期对 `Unterminated string starting at` 错误也加 \"
                    # 实际 pos 指向字符串*开始*位置, 加 \" 会破坏字符串导致更糟. 这种错误
                    # 是 LLM 输出未 escape 的换行/控制字符, 系统无法修, 让主线程重生成.
                    _dispatch_blocked_by_json = True
                    _err_msg = str(_json_err)
                    _is_unterminated = "Unterminated string" in _err_msg
                    # 2026-05-15 P100: 检测 "Extra data" — LLM 生成多 JSON 拼接
                    # 病因(实测 00:16:41 trace): tool args 4483 chars, char 4482 报 Extra data,
                    # 即 LLM 输出了类似 {...}\n{...} 的两个 JSON 拼接。
                    # 修法: 截取到 _pos 位置 (第一个 JSON 结尾), 试解析。
                    _is_extra_data = "Extra data" in _err_msg
                    if _is_extra_data:
                        _pos = getattr(_json_err, 'pos', None)
                        if _pos and 0 < _pos <= len(_args_raw_str):
                            try:
                                args = json.loads(_args_raw_str[:_pos])
                                _json_err = None
                                _dispatch_blocked_by_json = False
                                debug.log(
                                    f"llm.tools.json_repaired",
                                    f"P100: 'Extra data' 修复成功 — 截取到 char {_pos}/{len(_args_raw_str)} "
                                    f"(LLM 输出了多个 JSON 拼接, 取第一个)",
                                )
                            except json.JSONDecodeError:
                                pass
                    if _is_unterminated and _dispatch_blocked_by_json:
                        # 不要尝试修, 直接 fall through, 给主线程清晰错误
                        debug.log(
                            f"llm.tools.json_broken",
                            f"P39: Unterminated string (LLM 输出未 escape 换行/控制字符), "
                            f"args_raw 长度={len(_args_raw_str)}, 系统无法修, 主线程下轮应重生成 valid JSON. "
                            f"err='{_err_msg[:100]}'",
                        )
                        # 2026-05-17 P150: office 调用的 json_broken → 触发自适应缩小
                        if tc.function.name == "office":
                            try:
                                from app.llm.tools.office import report_office_failure
                                report_office_failure(
                                    task_id,
                                    reason=f"unterminated_string in office args (len={len(_args_raw_str)})",
                                )
                            except Exception:
                                pass
                    elif _dispatch_blocked_by_json and not _is_extra_data:
                        # P39 启发式修复: 在 error.pos 附近找未 escape 的 ", 加 \ escape
                        # 仅对 escape 错位置类问题有效
                        _fixed = _args_raw_str
                        _orig_err = _json_err
                        for _attempt in range(5):
                            try:
                                args = json.loads(_fixed)
                                _json_err = None
                                _dispatch_blocked_by_json = False
                                debug.log(
                                    f"llm.tools.json_repaired",
                                    f"P39: JSON 修复成功 (尝试 {_attempt+1} 轮, "
                                    f"orig_err='{_orig_err}', len {len(_args_raw_str)}→{len(_fixed)})",
                                )
                                break
                            except json.JSONDecodeError as _e:
                                _pos = getattr(_e, 'pos', None)
                                if _pos is None or _pos == 0 or _pos >= len(_fixed):
                                    break
                                if _fixed[_pos-1] == '"':
                                    _fixed = _fixed[:_pos-1] + '\\"' + _fixed[_pos:]
                                else:
                                    if _pos < len(_fixed) and _fixed[_pos] == '"':
                                        _fixed = _fixed[:_pos] + '\\"' + _fixed[_pos+1:]
                                    else:
                                        break
                        if _json_err is not None:
                            debug.log(
                                f"llm.tools.json_broken",
                                f"P39: args JSON 解析失败 (char {getattr(_json_err, 'pos', '?')}: {_json_err}), "
                                f"args_raw 长度={len(_args_raw_str)}, 主线程下轮应重生成 valid JSON",
                            )
                            # 2026-05-17 P150: office 调用 → 触发自适应缩小
                            if tc.function.name == "office":
                                try:
                                    from app.llm.tools.office import report_office_failure
                                    report_office_failure(
                                        task_id,
                                        reason=f"json_broken char {getattr(_json_err, 'pos', '?')}: "
                                               f"{str(_json_err)[:80]} (args_len={len(_args_raw_str)})",
                                    )
                                except Exception:
                                    pass
                tool_name = tc.function.name
                if (
                    task_id is None
                    and tool_name == "workspace"
                    and str(args.get("action") or "").strip().lower() in {"write", "mkdir"}
                    and _current_turn_forbids_file_writes(msgs)
                ):
                    _path_key = str(args.get("path") or "").strip() or "<no-path>"
                    _warn_key = f"{str(args.get('action') or '').lower()}:{_path_key}"
                    if _warn_key not in _read_only_write_warning_paths:
                        _read_only_write_warning_paths.add(_warn_key)
                        result = json.dumps({
                            "ok": False,
                            "warning": "current_turn_explicitly_read_only",
                            "blocked_once": True,
                            "tool_name": tool_name,
                            "action": args.get("action"),
                            "path": args.get("path"),
                            "fact": (
                                "The current user request explicitly appears to forbid file modifications "
                                "(for example, read-only / do not modify files / analysis only). This write was not executed once. "
                                "Decide whether to answer in text, ask for permission, or repeat the write only if it is still necessary and consistent with the user request."
                            ),
                            "事实": (
                                "当前用户请求含只读/不要修改/仅分析含义。本次写入已被暂缓一次；"
                                "请重新判断是否改为文字回复、请求许可，或在确有必要且符合请求时再次调用。"
                            ),
                        }, ensure_ascii=False)
                        debug.log(
                            "llm.tools.read_only_write_warning",
                            f"workspace {args.get('action')} {args.get('path')} blocked_once at iter {it}",
                        )
                        return tc.id, tool_name, result, args
                if (
                    not _browser_repro_evidence_seen
                    and not _active_task_has_browser_evidence_fact(msgs)
                    and tool_name == "delegate"
                    and _delegate_call_is_pre_edit_mutation(args)
                    and _active_task_requires_browser_pre_edit_evidence(msgs)
                ):
                    _warn_key = "delegate:browser_pre_edit_boundary"
                    _browser_repro_pre_edit_warning_paths.add(_warn_key)
                    _warning_count = _browser_repro_pre_edit_warning_counts.get(_warn_key, 0) + 1
                    _browser_repro_pre_edit_warning_counts[_warn_key] = _warning_count
                    _attach_browser_pre_edit_fact_to_delegate(args, warning_count=_warning_count)
                    debug.log(
                        "llm.tools.browser_repro_pre_delegate_fact",
                        f"attached browser pre-edit fact to delegate at iter {it} count={_warning_count}",
                    )
                if (
                    not _browser_repro_evidence_seen
                    and not _active_task_has_browser_evidence_fact(msgs)
                    and _tool_call_is_pre_edit_mutation(tool_name, args)
                    and _active_task_explicitly_requires_browser_repro(msgs)
                ):
                    _browser_pre_edit_required = _active_task_requires_browser_pre_edit_evidence(msgs)
                    _path_key = str(
                        (args or {}).get("path")
                        or (args or {}).get("workspace_path")
                        or (args or {}).get("project_path")
                        or (args or {}).get("file")
                        or "<no-path>"
                    ).strip()
                    _warn_key = f"{tool_name}:{_path_key}"
                    if _warn_key not in _browser_repro_pre_edit_warning_paths:
                        _browser_repro_pre_edit_warning_paths.add(_warn_key)
                        _warning_count = _browser_repro_pre_edit_warning_counts.get(_warn_key, 0) + 1
                        _browser_repro_pre_edit_warning_counts[_warn_key] = _warning_count
                        pending_runtime_facts.append(_browser_pre_edit_missing_fact_payload(
                            tool_name=tool_name,
                            path=_path_key,
                            warning_count=_warning_count,
                            pre_edit_required=_browser_pre_edit_required,
                        ))
                        debug.log(
                            "llm.tools.browser_repro_pre_edit_fact",
                            f"{tool_name} {_path_key} fact attached at iter {it} count={_warning_count} "
                            f"pre_edit_required={_browser_pre_edit_required}",
                        )
                if task_id is None:
                    _source_edit_path = _main_source_edit_path(tool_name, args)
                    if (
                        _source_edit_path
                        and _source_edit_path not in _helper_staged_project_paths_seen
                        and _source_edit_path not in _main_source_edit_delegation_warning_paths
                    ):
                        _main_source_edit_delegation_warning_paths.add(_source_edit_path)
                        pending_runtime_facts.append(_main_source_edit_delegation_fact_payload(
                            tool_name=tool_name,
                            path=_source_edit_path,
                        ))
                        debug.log(
                            "llm.tools.main_source_edit_delegation_fact",
                            f"{tool_name} {_source_edit_path} fact attached at iter {it}",
                        )
                _publish_main_tool_event(
                    "main_tool_start",
                    tool=tool_name,
                    iteration=it,
                    status="running",
                    args=args,
                    call_id=getattr(tc, "id", "") or "",
                )
                if _dispatch_blocked_by_json:
                    result = json.dumps({
                        "ok": False,
                        "error": "tool_call_args_json_broken",
                        "tool_name": tool_name,
                        "hint": TOOL_ARGS_JSON_BROKEN_HINT,
                        "args_parse_error": str(_json_err),
                        "raw_args_excerpt": _args_raw_str[:400],
                    }, ensure_ascii=False)
                    _publish_main_tool_event(
                        "main_tool_done",
                        tool=tool_name,
                        iteration=it,
                        status="error",
                        args=args,
                        result=result,
                        elapsed_sec=time.monotonic() - _t_start,
                        call_id=getattr(tc, "id", "") or "",
                    )
                    return tc.id, tool_name, result, args
                if _available_tool_names and tool_name not in _available_tool_names:
                    _replacement_facts = []
                    if tool_name == "workflow":
                        for _candidate in ("task_plan", "agent_state", "todo_write", "todo_read", "delegate"):
                            if _candidate in _available_tool_names:
                                _replacement_facts.append(_candidate)
                    result = json.dumps({
                        "ok": False,
                        "error": "tool_not_available_in_this_context",
                        "tool_name": tool_name,
                        "available_tools": sorted(_available_tool_names),
                        "replacement_facts": (
                            f"The called tool {tool_name!r} is not available. Current planning/state tools present: "
                            + ", ".join(_replacement_facts)
                            if _replacement_facts else None
                        ),
                        "hint": (
                            "Use only tools listed for the current role. If a needed capability is missing, "
                            "report the missing resource or ask the main process to reroute the task. "
                            "For active task changes, use the available planning/state tools listed in this result."
                            "\n只能使用当前角色列出的工具；缺能力时请求主进程改派或补资源。"
                        ),
                    }, ensure_ascii=False)
                    debug.log(
                        "llm.tools.unavailable_blocked",
                        f"blocked unavailable tool {tool_name!r}; available={sorted(_available_tool_names)}",
                    )
                    _publish_main_tool_event(
                        "main_tool_done",
                        tool=tool_name,
                        iteration=it,
                        status="error",
                        args=args,
                        result=result,
                        elapsed_sec=time.monotonic() - _t_start,
                        call_id=getattr(tc, "id", "") or "",
                    )
                    return tc.id, tool_name, result, args
                # #13: per-tool 调用次数限制 — 仅主线程生效。
                # helper 沙箱内不限(大型编程任务单 helper 可能需 100+ workspace 调用)。
                limit = _PER_TOOL_LIMITS.get(tool_name)
                if limit and _tool_call_counts.get(tool_name, 0) > limit and current_helper_proc_id() is None:
                    warning_result = json.dumps({
                        "ok": False,
                        "rate_limited": True,
                        "_ts_iso": _now_iso(),
                        "_started_at_iso": _t_start_iso,
                        "_tool_elapsed_sec": round(time.monotonic() - _t_start, 3),
                        "error": (
                            f"Tool {tool_name} reached its per-round limit: "
                            f"{_tool_call_counts[tool_name]} call(s), limit {limit}. Change approach before retrying. "
                            f"{'Read the relevant file region before another edit. ' if tool_name in ('edit_file', 'insert_in_file') else ''}"
                            f"{'Inspect the file type before another plain-text read. ' if tool_name == 'read_file' else ''}"
                            f"{'Finish with a plan if no tool path remains. ' if tool_name in ('python', 'workspace') else ''}"
                            "Record the blocker in internal_note when the task cannot advance with the available tools.\n\n"
                            "工具达到本轮调用上限；请换方法，必要时记录卡点或输出计划。"
                        ),
                    }, ensure_ascii=False)
                    debug.log(
                        f"llm.tools.rate_limited",
                        f"{tool_name} blocked at count={_tool_call_counts[tool_name]} (limit={limit})",
                    )
                    _publish_main_tool_event(
                        "main_tool_done",
                        tool=tool_name,
                        iteration=it,
                        status="error",
                        args=args,
                        result=warning_result,
                        elapsed_sec=time.monotonic() - _t_start,
                        call_id=getattr(tc, "id", "") or "",
                    )
                    return tc.id, tool_name, warning_result, args
                result = await dispatcher(tool_name, args)
                if pending_runtime_facts:
                    result = _merge_tool_result_facts(result, pending_runtime_facts)
                # ══ 后处理(progress/lite switch/时间戳注入)══
                # 2026-05-08 Fix: 这些辅助逻辑不能因异常而丢弃工具结果。
                # 工具已成功执行(result 已产出),后处理失败只记录不拦截。
                try:
                    # 拟人化反馈：剧情节点检测(挫折/突破/长时间静默)
                    # progress_state 自己判断,只在节点触发时返回 event,
                    # progress_cb 内部由 lite 用人设生成自然语言(也可决定不说)
                    ok, _ = _tool_result_signal(result)
                    event = progress_state.update(ok=ok, kind=tool_name)
                    if progress_cb is not None and tool_name == "delegate":
                        try:
                            _parsed_delegate = json.loads(result) if isinstance(result, str) else result
                        except (json.JSONDecodeError, TypeError):
                            _parsed_delegate = {}
                        if isinstance(_parsed_delegate, dict):
                            _summary = _delegate_workflow_result_summary(_parsed_delegate) or {}
                            _success = int(_summary.get("success_count") or 0)
                            _incomplete = int(_summary.get("incomplete_count") or 0)
                            _running = int(_parsed_delegate.get("helpers_still_running") or 0)
                            _spawned = int(_parsed_delegate.get("helpers_initially_spawned") or 0)
                            if _success > 0:
                                event = "helper_done"
                            elif _incomplete > 0:
                                event = "stuck"
                            elif _running > 0 or _spawned > 0:
                                event = "helper_start"
                    if event and progress_cb is not None:
                        try:
                            from app.core.bg_tasks import schedule

                            bg_tasks.append(schedule(
                                _safe_progress(progress_cb, it, msgs, event),
                                name=f"progress_callback:{task_id or 'main'}",
                            ))
                        except Exception:
                            pass
                    # ── 2026-05-02 part9 #7:tool_result_cb 单条 tool 结果钩子 ──
                    # _round2 用此 cb 跟踪 macro signals(只扫单条 delegate 结果是否含
                    # batch_timeout_majority,不再每个 iter_progress 都扫 msgs[-6:])。
                    # 时间戳还没注入,所以传给 cb 的是原始 result;cb 内部自己 string match。
                    if tool_result_cb is not None:
                        try:
                            tool_result_cb(tool_name, result)
                        except Exception:
                            log.exception("tool_result_cb raised (suppressed)")
                    # ── 2026-05-07 Opt B: track recent tools & delegate results ──
                    _recent_tool_names.append(tool_name)
                    if tool_name == "commit_to_main":
                        _last_commit_to_main_iter = it
                    if tool_name == "delegate":
                        try:
                            _parsed = json.loads(result) if isinstance(result, str) else result
                            if isinstance(_parsed, dict):
                                _delegate_summary = _delegate_workflow_result_summary(_parsed) or {}
                                _last_delegate_result_summary["helpers_still_running"] = (
                                    _parsed.get("helpers_still_running", 0)
                                )
                                _last_delegate_result_summary["iter"] = it
                                _browser_gap_facts = _delegate_summary.get("browser_evidence_gap_facts") or []
                                if (
                                    task_id is None
                                    and _browser_gap_facts
                                    and not _browser_repro_evidence_seen
                                    and not _active_task_has_browser_evidence_fact(msgs)
                                    and _active_task_requires_browser_pre_edit_evidence(msgs)
                                ):
                                    _new_gap_lines = []
                                    for _gap in _browser_gap_facts[:4]:
                                        if not isinstance(_gap, dict):
                                            continue
                                        _tid = str(_gap.get("task_id") or "unknown").strip()
                                        if _tid in _browser_repro_helper_gap_tasks:
                                            continue
                                        _browser_repro_helper_gap_tasks.add(_tid)
                                        _urls = _gap.get("urls") if isinstance(_gap.get("urls"), list) else []
                                        _url_text = ", ".join(str(u) for u in _urls[:3]) or "target URL/path not stated"
                                        _new_gap_lines.append(
                                            f"- branch={_tid}; urls={_url_text}; fact={_neutral_round3_gap_text(_gap.get('fact'))}"
                                        )
                                    if _new_gap_lines:
                                        _append_tool_loop_dynamic_guidance(
                                            msgs,
                                            (
                                                "[SYSTEM_HINT/browser_evidence_gap_from_parallel_branch]\n"
                                                "A processing branch returned artifact facts, but also reported that the active "
                                                "browser/host-browser evidence boundary was not satisfied in that execution environment. "
                                                "This is a separate task-evidence fact, not a content-quality rejection of the artifact output. "
                                                "If the main process has a narrower browser-family route available, collect that evidence "
                                                "before treating browser-dependent apply/finalization as covered; otherwise record the concrete "
                                                "infeasible boundary and decide from the active contract.\n"
                                                + "\n".join(_new_gap_lines)
                                                + "\n\n并行处理步骤报告了产物事实，同时报告浏览器证据边界未满足；这不是否定产物质量，而是独立任务证据事实。"
                                            ),
                                        )
                                        debug.log(
                                            "llm.tools.browser_repro_helper_gap_fact",
                                            f"delegate browser evidence gap tasks={sorted(_browser_repro_helper_gap_tasks)} iter={it}",
                                        )
                        except (json.JSONDecodeError, TypeError):
                            pass
                    # ── early lite switch triggers ──
                    _maybe_early_lite = False
                    _switch_reason = ""
                    # Trigger 1: 5 consecutive todo-only tool calls
                    _TODO_ONLY_TOOLS = {"todo_write", "todo_read"}
                    if (len(_recent_tool_names) >= 5
                            and all(n in _TODO_ONLY_TOOLS for n in list(_recent_tool_names)[-5:])):
                        _maybe_early_lite = True
                        _switch_reason = "5 consecutive todo-only calls"
                    # Trigger 2: no helpers still running + commit_to_main 2+ iters ago
                    if (not _maybe_early_lite
                            and _last_delegate_result_summary.get("helpers_still_running") == 0
                            and _last_delegate_result_summary.get("iter", -1) > 0
                            and _last_commit_to_main_iter >= 0
                            and (it - max(_last_commit_to_main_iter,
                                          _last_delegate_result_summary.get("iter", 0))) >= 2):
                        _maybe_early_lite = True
                        _switch_reason = "no helpers still running + deliverables committed"
                    if _maybe_early_lite and not lite and task_id:
                        lite = True
                        from app.llm.model_pool import resolve as _mp_resolve
                        _lite_spec = _mp_resolve(think=False, tier="low")
                        model = _lite_spec.model
                        reasoning = _lite_spec.reasoning
                        extra_body = _thinking_extra_body(reasoning, _lite_spec.provider)
                        _cli_container[0] = _client_for_spec(_lite_spec)
                        debug.log(
                            "llm.tools.early_lite_switch",
                            f"{_switch_reason}; switching to lite ({model}) at iter {it}",
                        )
                    elif _maybe_early_lite and not lite:
                        debug.log(
                            "llm.tools.early_lite_switch_skipped",
                            f"{_switch_reason}; keeping main-thread model for final plan quality",
                        )
                except Exception:
                    log.exception(
                        "post-dispatcher processing failed for %s; "
                        "tool result preserved, raw result will be used",
                        tool_name,
                    )
                # ── 注入时间戳到 result(2026-05-02 part8)──
                # result 是 dispatcher 返回的字符串(各 tool handler 一般 json.dumps),
                # 这里 parse → 加字段 → re-dump。失败/非 JSON object 不动原文,
                # 包一层 envelope 也不太合适(会破坏现有读字段逻辑)。
                _elapsed = round(time.monotonic() - _t_start, 3)
                try:
                    _ts_mode = getattr(settings, "tool_result_timestamp_mode", "full") or "full"
                    result = _inject_tool_timestamps(
                        result, started_at_iso=_t_start_iso,
                        finished_at_iso=_now_iso(), elapsed_sec=_elapsed,
                        mode=_ts_mode,
                    )
                except Exception:
                    log.exception(
                        "_inject_tool_timestamps failed for %s; using raw result",
                        tool_name,
                    )
                try:
                    _tool_ok, _ = _tool_result_signal(result)
                except Exception:
                    _tool_ok = True
                try:
                    _record_main_tool_facts_in_agent_state(tool_name, args, result, helper_kind)
                except Exception:
                    log.exception(
                        "agent_state factual mirror wrapper failed for %s (non-fatal)",
                        tool_name,
                    )
                _publish_main_tool_event(
                    "main_tool_done",
                    tool=tool_name,
                    iteration=it,
                    status="done" if _tool_ok else "error",
                    args=args,
                    result=result,
                    elapsed_sec=time.monotonic() - _t_start,
                    call_id=getattr(tc, "id", "") or "",
                )
                # part20:返回 args 给 stuck detector(用于 same-file edit 跟踪)
                return tc.id, tool_name, result, args

            # Bug #30: 每个 iter 开始前清空 FIX_HINT 重复计数,同一 iter 内相同 hint 第 2 次出现会升级警告。
            reset_fix_hint_counts()

            raw_results = await asyncio.gather(*[_run(tc) for tc in tool_calls], return_exceptions=True)
            results = []
            for _idx, _rr in enumerate(raw_results):
                if isinstance(_rr, BaseException):
                    # 2026-05-08 Fix: 不再丢弃工具结果。即使 _run 异常,
                    # 也构造一个 synthetic result 让 LLM 看到反馈,
                    # 避免 LLM 因看不到结果而反复重试同一条命令。
                    _tc = tool_calls[_idx] if _idx < len(tool_calls) else None
                    _tc_name = _tc.function.name if _tc else "unknown"
                    _tc_id = _tc.id if _tc else "unknown"
                    log.exception(
                        "tool _run raised unexpectedly for %s; "
                        "injecting synthetic result instead of dropping",
                        _tc_name,
                    )
                    _synthetic = json.dumps({
                        "ok": False,
                        "action": _tc_name,
                        "error": f"工具执行异常: {type(_rr).__name__}",
                        "_ts_iso": _now_iso(),
                        "_note": "工具可能已部分执行,检查工作区现状后决定下一步",
                    }, ensure_ascii=False)
                    results.append((_tc_id, _tc_name, _synthetic, {}))
                    continue
                results.append(_rr)
            if results:
                _executed_tool_result_count += len(results)
                if helper_kind is None:
                    _VERIFY_COMMAND_RE = re.compile(
                        r"(?:^|[\/\s\"'])(?:verify|check|validate|grade|test|run[_-]?tests?)[\w.-]*\.(?:py|js|mjs|cjs|sh|ps1|bat|cmd)(?:$|[\s\"'])",
                        re.IGNORECASE,
                    )
                    for _, _name, _result, _args in results:
                        if _name == "delegate":
                            _paths = _delegate_result_staged_paths(_result)
                            if _paths:
                                _helper_staged_project_paths_seen.update(_paths)
                            _owned_paths = _delegate_result_helper_owned_paths(_result)
                            if _owned_paths:
                                _helper_owned_project_paths_seen.update(_owned_paths)
                    for _, _name, _result, _args in results:
                        _main_tool_result_sequence += 1
                        _result_seq = _main_tool_result_sequence
                        try:
                            _parsed_result = json.loads(_result) if isinstance(_result, str) else _result
                        except Exception:
                            _parsed_result = None
                        if isinstance(_parsed_result, dict):
                            if task_id is None:
                                _blocked_project_write = _unresolved_project_write_block_from_result(_name, _parsed_result)
                                if _blocked_project_write:
                                    _unresolved_project_write_blocks[str(_blocked_project_write["path"])] = _blocked_project_write
                                    _guidance.reset("unresolved_project_write_block")
                                _resolved_project_write = _resolved_project_write_path_from_result(_name, _parsed_result)
                                if _resolved_project_write:
                                    _unresolved_project_write_blocks.pop(_resolved_project_write, None)
                            _fact = _parsed_result.get("acceptance_script_fact")
                            if (
                                _name == "env_read"
                                and isinstance(_fact, dict)
                                and _fact.get("scans_project_or_workspace_text") is True
                            ):
                                _fact_path = str(_fact.get("path") or (_parsed_result.get("path") or "")).strip()
                                if _fact_path:
                                    _fact = dict(_fact)
                                    _fact.setdefault("source", "env_read")
                                    _main_acceptance_script_facts[_fact_path] = _fact
                            _acceptance_paths: list[str] = []
                            for _container in (
                                _parsed_result,
                                _parsed_result.get("helper_handoff_fact"),
                                _parsed_result.get("background_handoff_fact"),
                            ):
                                if not isinstance(_container, dict):
                                    continue
                                _paths = _container.get("acceptance_script_paths")
                                if isinstance(_paths, list):
                                    _acceptance_paths.extend(str(_p).strip() for _p in _paths if str(_p).strip())
                            if _name in {"env_list_tree", "env_inventory"} and _acceptance_paths:
                                for _path in _acceptance_paths[:20]:
                                    _main_acceptance_script_facts.setdefault(_path, {
                                        "kind": "acceptance_script_discovered_fact",
                                        "path": _path,
                                        "source": _name,
                                        "script_source_known": False,
                                    })
                            if _name in {"env_list_tree", "env_inventory"}:
                                _main_existing_verifier_visible_artifacts.update(
                                    _verifier_visible_artifact_paths_from_listing(_name, _parsed_result)
                                )
                        _command_text = _main_verifier_command_text(_name, _args)
                        if _command_text and _VERIFY_COMMAND_RE.search(_command_text):
                            _main_verifier_command_seen = True
                            try:
                                _ok, _ = _tool_result_signal(_result)
                            except Exception:
                                _ok = False
                            if _ok:
                                _main_verifier_command_success_seen = True
                                _main_last_successful_verifier_iter = it
                                _main_last_successful_verifier_seq = _result_seq
                        if _name in {"env_apply_create", "env_apply_replace"}:
                            try:
                                _ok, _ = _tool_result_signal(_result)
                            except Exception:
                                _ok = False
                            if _ok:
                                _main_project_state_mutation_seen = True
                                _main_last_project_state_mutation_iter = it
                                _main_last_project_state_mutation_seq = _result_seq
                                if isinstance(_parsed_result, dict):
                                    _main_last_project_state_mutation_path = _env_apply_path_from_result(_parsed_result, _args if isinstance(_args, dict) else {})
                                    _main_last_project_state_mutation_helper_owned = _env_apply_uses_helper_staged_source(
                                        _parsed_result,
                                        _args if isinstance(_args, dict) else {},
                                        _helper_owned_project_paths_seen - _main_tainted_helper_staged_paths,
                                    )
                        if _name == "workspace" and isinstance(_args, dict):
                            _action = str(_args.get("action") or "").strip().lower()
                            if _action in {"write", "mkdir"}:
                                try:
                                    _ok, _ = _tool_result_signal(_result)
                                except Exception:
                                    _ok = False
                                if _ok:
                                    _main_project_state_mutation_seen = True
                                    _main_last_project_state_mutation_iter = it
                                    _main_last_project_state_mutation_seq = _result_seq
                                    _main_last_project_state_mutation_path = _norm_project_path_fact(_args.get("path"))
                                    _main_last_project_state_mutation_helper_owned = False
                                    _write_path = str(_args.get("path") or "").replace("\\", "/").strip().lstrip("./")
                                    _write_path_project = _norm_project_path_fact(_write_path)
                                    if (
                                        _write_path in _helper_staged_project_paths_seen
                                        or _write_path_project in _helper_staged_project_paths_seen
                                        or _write_path in _helper_owned_project_paths_seen
                                        or _write_path_project in _helper_owned_project_paths_seen
                                    ):
                                        _main_tainted_helper_staged_paths.add(_write_path)
                                        _main_tainted_helper_staged_paths.add(_write_path_project)
                if any(
                    _tool_result_is_browser_repro_evidence(_name, _args, _result)
                    for _, _name, _result, _args in results
                ):
                    if not _browser_repro_evidence_seen:
                        debug.log(
                            "llm.tools.browser_repro_evidence_seen",
                            f"browser-family evidence observed at iter {it}",
                        )
                    _browser_repro_evidence_seen = True
                for _, _name, _result, _args in results:
                    if _name == "delegate":
                        _paths = _delegate_result_staged_paths(_result)
                        if _paths:
                            _helper_staged_project_paths_seen.update(_paths)
                        _owned_paths = _delegate_result_helper_owned_paths(_result)
                        if _owned_paths:
                            _helper_owned_project_paths_seen.update(_owned_paths)
                for _call_id, _name, _result, _args in results:
                    if task_id is None and _name in {"workspace", "env_apply_create"}:
                        _compact_blocked_large_write_tool_args_in_last_assistant(
                            msgs,
                            tool_name=_name,
                            call_id=str(_call_id or ""),
                            args=_args if isinstance(_args, dict) else {},
                            result=_result,
                        )
                    if task_id is None and _name == "delegate":
                        _compact_delegate_input_file_source_blocks_in_last_assistant(
                            msgs,
                            call_id=str(_call_id or ""),
                            args=_args if isinstance(_args, dict) else {},
                        )
                try:
                    from app.core import toolchain_cache as _toolchain_cache
                    _schema_retry_reasons: dict[str, str] = {}
                    _trace_id_for_schema = debug.current_trace_id()
                    _schema_attempted_tools = {
                        _name for _, _name, _result, _args in results
                        if _name in _toolchain_cache.expanded_schema_tools(_trace_id_for_schema)
                    }
                    for _, _name, _result, _args in results:
                        _reason = _tool_result_schema_retry_reason(_name, _result)
                        if _reason:
                            _schema_retry_reasons[_name] = _reason
                    for _name in _schema_attempted_tools.difference(_schema_retry_reasons.keys()):
                        _toolchain_cache.clear_tool_schema_retry(
                            _name,
                            _trace_id_for_schema,
                            reason="expanded-schema retry attempt no longer has schema-shaped error",
                        )
                    for _name, _reason in _schema_retry_reasons.items():
                        _toolchain_cache.mark_tool_schema_retry(
                            _name,
                            _reason,
                            _trace_id_for_schema,
                        )
                except Exception:
                    log.exception("tool schema expansion state update failed (non-fatal)")
                if task_id is not None:
                    _read_like_tools = {
                        "read_file",
                        "inspect_file",
                        "search_in_file",
                        "search_files",
                        "workspace",
                        "office",
                    }
                    _write_like_tools = {
                        "write_file",
                        "edit_file",
                        "insert_in_file",
                        "multi_edit",
                        "workspace",
                        "office",
                        "python",
                        "bash",
                    }

                    def _is_read_like_tool_result(name: str, args: dict) -> bool:
                        if name not in _read_like_tools:
                            return False
                        if name == "workspace":
                            action = str((args or {}).get("action") or "").strip().lower()
                            return action in {"locate"} or not action
                        if name == "office":
                            action = str((args or {}).get("action") or "").strip().lower()
                            return action in {"read", "inspect", "verify_numbers", "verify_rigor", "verify_integrity"}
                        return True

                    def _is_write_like_tool_result(name: str, args: dict) -> bool:
                        if name not in _write_like_tools:
                            return False
                        if name == "workspace":
                            action = str((args or {}).get("action") or "").strip().lower()
                            return action in {"write", "mkdir", "run"}
                        if name == "office":
                            action = str((args or {}).get("action") or "").strip().lower()
                            return action in {"write", "append", "create", "insert_image", "edit", "save"}
                        return True

                    if any(_is_write_like_tool_result(name, args) for _, name, _, args in results):
                        _helper_read_only_streak = 0
                    elif all(_is_read_like_tool_result(name, args) for _, name, _, args in results):
                        _helper_read_only_streak += len(results)
                    else:
                        _helper_read_only_streak = 0

                    if (
                        not _helper_read_to_write_hint_emitted
                        and _helper_read_only_streak >= 6
                    ):
                        _helper_read_to_write_hint_emitted = True
                        _append_tool_loop_dynamic_guidance(
                            msgs,
                            helper_read_to_write_checkpoint(
                                iteration=it,
                                helper_kind=helper_kind,
                                recent_reads=_helper_read_only_streak,
                            ),
                        )
                        debug.log(
                            "llm.tools.helper_read_to_write_checkpoint",
                            f"task={task_id} read_only_streak={_helper_read_only_streak} iter={it}",
                        )

            # Bug 2 + B5 修复: 升级信号设过 True 之后,记录"真实进展"的工具调用数。
            # B5 收紧:不是任何 ok=True 都算进展。read_file/inspect_file/processes 等
            # 只读/查询工具即使 ok=True 也不改变工作区状态,lite 卡住时反复做这些可以
            # 凑出虚假的"成功后续"误清升级信号。这里只数真正改变状态/有产出的工具。
            if upgrade_signal is not None and upgrade_signal.get("should_upgrade"):
                READ_ONLY_TOOLS = {
                    "read_file", "inspect_file", "search_in_file", "search_files",
                    "processes",  # 只查/管理进程
                    "expand_warm", "expand_cold", "expand_kb",
                    "mark_avoid_mention",  # 元操作
                }
                this_round_real_progress = all(
                    _tool_result_signal(result)[0]
                    for _, name, result, _args in results
                    if name not in READ_ONLY_TOOLS
                )
                # 至少有一个非只读工具且全部成功才算"实质进展"
                has_progress_tool = any(
                    name not in READ_ONLY_TOOLS for _, name, _, _args in results
                )
                if has_progress_tool and this_round_real_progress:
                    successful_after_signal += 1

            # 工具结果已收回,但 LLM 还没调,这里再查一次 abort
            # 否则刚跑完 5s 工具,LLM 又要跑 8s 才能停
            if _check_aborted():
                forced_finalize_trigger = "abort_event_post_tool"
                debug.log("llm.tools.abort", f"aborted at iter {it} (post-tool)")
                # 把工具结果先追加到 msgs(否则 forced finalize 看不到这一轮成果)
                for tc_id, tc_name, result, _args in results:
                    result = _budget_tool_result_for_model_context(
                        tc_id,
                        tc_name,
                        result,
                        iteration=it,
                    )
                    msgs.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": result,
                    })
                break

            # 追加 tool 结果
            _main_source_path_handoff_hint_pending = False
            _main_text_material_handoff_hint_pending = False
            for _result_index, (tc_id, tc_name, result, _args) in enumerate(results):
                result = _budget_tool_result_for_model_context(
                    tc_id,
                    tc_name,
                    result,
                    iteration=it,
                )
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result,
                })

                if task_id is None:
                    try:
                        _parsed_result_for_paths = json.loads(result) if isinstance(result, str) else result
                    except Exception:
                        _parsed_result_for_paths = None
                    _handoff_paths = _main_helper_handoff_paths_from_project_discovery(
                        tc_name,
                        _parsed_result_for_paths if isinstance(_parsed_result_for_paths, dict) else {},
                    )
                    if _handoff_paths:
                        before_handoff_count = len(_main_helper_handoff_paths)
                        _main_helper_handoff_paths.update(_handoff_paths)
                        if len(_main_helper_handoff_paths) > before_handoff_count:
                            _main_helper_handoff_fact_iter = it
                    _text_material_paths = _main_text_material_paths_from_project_discovery(
                        tc_name,
                        _parsed_result_for_paths if isinstance(_parsed_result_for_paths, dict) else {},
                    )
                    if _text_material_paths:
                        before_handoff_count = len(_main_helper_handoff_paths)
                        _main_helper_handoff_paths.update(_text_material_paths)
                        if len(_main_helper_handoff_paths) > before_handoff_count:
                            _main_helper_handoff_fact_iter = it
                        if not _main_text_material_handoff_hint_emitted:
                            _main_text_material_handoff_hint_pending = True
                    _new_source_paths = _main_source_or_test_paths_from_project_discovery(
                        tc_name,
                        _parsed_result_for_paths if isinstance(_parsed_result_for_paths, dict) else {},
                    )
                    if _new_source_paths:
                        before_count = len(_main_discovered_source_test_paths)
                        _main_discovered_source_test_paths.update(_new_source_paths)
                        if (
                            not _main_source_path_handoff_hint_emitted
                            and len(_main_discovered_source_test_paths) >= 2
                            and len(_main_discovered_source_test_paths) > before_count
                        ):
                            _main_source_path_handoff_hint_pending = True
                    _env_run_family = _main_env_run_convergence_family(tc_name, _args)
                    if _env_run_family:
                        _main_env_run_family_counts[_env_run_family] = (
                            _main_env_run_family_counts.get(_env_run_family, 0) + 1
                        )
                        _env_run_count = _main_env_run_family_counts[_env_run_family]
                        _env_run_threshold = 2 if (
                            _env_run_family.startswith("verifier:")
                            or _env_run_family.startswith("db:")
                        ) else 3
                        if (
                            _env_run_count >= _env_run_threshold
                            and _env_run_family not in _main_env_run_family_hint_emitted
                        ):
                            _main_env_run_family_hint_emitted.add(_env_run_family)
                            _append_tool_loop_dynamic_guidance(
                                msgs,
                                main_env_run_convergence_hint(
                                    family=_env_run_family,
                                    count=_env_run_count,
                                ),
                            )
                            debug.log(
                                "llm.tools.main_env_run_convergence",
                                f"family={_env_run_family} count={_env_run_count} iter={it}",
                            )
                    if _main_source_path_handoff_hint_pending and _result_index == len(results) - 1:
                        _main_source_path_handoff_hint_emitted = True
                        _main_source_path_handoff_hint_pending = False
                        _source_paths_for_hint = sorted(_main_discovered_source_test_paths)
                        _append_tool_loop_dynamic_guidance(
                            msgs,
                            _main_source_path_handoff_fact(_source_paths_for_hint),
                        )
                        debug.log(
                            "llm.tools.main_source_path_handoff_fact",
                            f"injected after discovery at iter {it}: {_source_paths_for_hint[:10]}",
                        )
                    if _main_text_material_handoff_hint_pending and _result_index == len(results) - 1:
                        _main_text_material_handoff_hint_emitted = True
                        _main_text_material_handoff_hint_pending = False
                        _material_paths_for_hint = sorted(
                            path for path in _main_helper_handoff_paths
                            if Path(path).suffix.lower() in {
                                ".txt", ".md", ".markdown", ".html", ".htm", ".csv", ".tsv",
                                ".json", ".jsonl", ".yaml", ".yml", ".xml",
                            }
                        )
                        _append_tool_loop_dynamic_guidance(
                            msgs,
                            _main_text_material_handoff_fact(_material_paths_for_hint),
                        )
                        debug.log(
                            "llm.tools.main_text_material_handoff_fact",
                            f"injected after discovery at iter {it}: {_material_paths_for_hint[:10]}",
                        )
                    if tc_name == "delegate":
                        _main_helper_handoff_useful_delegate_seen = True
                    elif _main_helper_handoff_paths and not _main_helper_handoff_useful_delegate_seen:
                        _label = _main_direct_project_work_label(
                            tc_name,
                            _args if isinstance(_args, dict) else {},
                        )
                        if _label and it >= _main_helper_handoff_fact_iter >= 0:
                            _main_helper_handoff_direct_work_count += 1
                            _main_helper_handoff_recent_labels.append(_label)
                            _main_helper_handoff_recent_labels = _main_helper_handoff_recent_labels[-12:]
                    if (
                        not _main_helper_handoff_overwork_hint_emitted
                        and not _main_helper_handoff_useful_delegate_seen
                        and len(_main_helper_handoff_paths) >= 2
                        and _main_helper_handoff_direct_work_count >= _MAIN_HELPER_HANDOFF_OVERWORK_DIRECT_COUNT
                        and _result_index == len(results) - 1
                    ):
                        _main_helper_handoff_overwork_hint_emitted = True
                        _handoff_paths_for_hint = sorted(_main_helper_handoff_paths)
                        _append_tool_loop_dynamic_guidance(
                            msgs,
                            _main_helper_handoff_overwork_checkpoint(
                                paths=_handoff_paths_for_hint,
                                direct_count=_main_helper_handoff_direct_work_count,
                                recent_labels=_main_helper_handoff_recent_labels,
                            ),
                        )
                        debug.log(
                            "llm.tools.main_background_work_handoff_overwork_fact",
                            (
                                f"direct_count={_main_helper_handoff_direct_work_count} "
                                f"paths={_handoff_paths_for_hint[:10]} iter={it}"
                            ),
                        )

                if task_id is not None and not _helper_completed_todos_handoff_emitted:
                    _completed_todos = _completed_todo_count_from_result(tc_name, result)
                    if _completed_todos is not None:
                        _helper_completed_todos_handoff_emitted = True
                        _append_tool_loop_dynamic_guidance(
                            msgs,
                            helper_completed_todos_handoff(
                                iteration=it,
                                helper_kind=helper_kind,
                                completed=_completed_todos,
                            ),
                        )
                        debug.log(
                            "llm.tools.helper_completed_todos_handoff",
                            f"task={task_id} completed_todos={_completed_todos} iter={it}",
                        )

                if task_id is not None:
                    _office_write_key = _office_write_artifact_key(tc_name, _args, result)
                    if _office_write_key:
                        _office_write_counts[_office_write_key] = (
                            _office_write_counts.get(_office_write_key, 0) + 1
                        )
                        _office_write_count = _office_write_counts[_office_write_key]
                        if (
                            _office_write_count >= 4
                            and _office_write_key not in _office_write_hint_emitted
                        ):
                            _office_write_hint_emitted.add(_office_write_key)
                            _append_tool_loop_dynamic_guidance(
                                msgs,
                                helper_office_write_convergence_checkpoint(
                                    iteration=it,
                                    helper_kind=helper_kind,
                                    artifact=_office_write_key,
                                    write_count=_office_write_count,
                                ),
                            )
                            debug.log(
                                "llm.tools.helper_office_write_convergence",
                                f"task={task_id} artifact={_office_write_key} count={_office_write_count} iter={it}",
                            )
                    _office_read_key = _office_read_artifact_key(tc_name, _args, result)
                    if _office_read_key and _office_read_key.startswith("office:read:"):
                        _office_read_counts[_office_read_key] = (
                            _office_read_counts.get(_office_read_key, 0) + 1
                        )
                        _office_read_count = _office_read_counts[_office_read_key]
                        if (
                            _office_read_count >= 2
                            and _office_read_key not in _office_read_hint_emitted
                        ):
                            _office_read_hint_emitted.add(_office_read_key)
                            _append_tool_loop_dynamic_guidance(
                                msgs,
                                helper_office_read_convergence_checkpoint(
                                    iteration=it,
                                    helper_kind=helper_kind,
                                    artifact=_office_read_key.split(":", 2)[-1],
                                    read_count=_office_read_count,
                                ),
                            )
                            debug.log(
                                "llm.tools.helper_office_read_convergence",
                                f"task={task_id} artifact={_office_read_key} count={_office_read_count} iter={it}",
                            )

                _artifact_key = _artifact_acceptance_key(tc_name, _args, result)
                if _artifact_key:
                    _artifact_acceptance_counts[_artifact_key] = (
                        _artifact_acceptance_counts.get(_artifact_key, 0) + 1
                    )
                    _artifact_count = _artifact_acceptance_counts[_artifact_key]
                    if (
                        _artifact_count >= 3
                        and _artifact_key not in _artifact_acceptance_hint_emitted
                    ):
                        _artifact_acceptance_hint_emitted.add(_artifact_key)
                        _artifact_path = ""
                        if isinstance(_args, dict):
                            for _path_key in ("path", "file", "filename", "target", "source"):
                                _value = _args.get(_path_key)
                                if isinstance(_value, str) and _value.strip():
                                    _artifact_path = _value.strip()
                                    break
                        _append_tool_loop_dynamic_guidance(
                            msgs,
                            artifact_acceptance_convergence_hint(
                                iteration=it,
                                helper_kind=helper_kind,
                                tool_name=tc_name,
                                artifact=_artifact_path or _artifact_key,
                                count=_artifact_count,
                            ),
                        )
                        debug.log(
                            "llm.tools.artifact_acceptance_convergence",
                            f"key={_artifact_key} count={_artifact_count} iter={it}",
                        )

            # 2026-05-11 Tier 2.F: 可恢复 helper 未完成时自动作为下一轮 hint。
            # 如果 delegate 返回失败/卡住/资源缺失/产物不完整，主线程不应从失败事实直接 finalize；
            # 这里把同 task_id 续作或升级的通用恢复事实放回上下文，具体修法仍交给模型判断。
            if helper_kind is None:  # 只主线程注入
                _injected_for_tasks = locals().get("_injected_next_action_tasks", set())
                for tc_id, tc_name, result, _args in results:
                    if tc_name != "delegate":
                        continue
                    try:
                        for _gap in _delegate_gap_facts_from_result(result):
                            _gap_key = (
                                str(_gap.get("task_id") or ""),
                                str(_gap.get("gap_kind") or ""),
                                str(_gap.get("terminal_reason") or ""),
                            )
                            if not any(
                                (
                                    str(existing.get("task_id") or ""),
                                    str(existing.get("gap_kind") or ""),
                                    str(existing.get("terminal_reason") or ""),
                                ) == _gap_key
                                for existing in _delegate_gap_facts
                            ):
                                _delegate_gap_facts.append(_gap)
                                debug.log(
                                    "llm.tools.helper_gap_fact_recorded",
                                    f"task={_gap.get('task_id')} kind={_gap.get('gap_kind')}",
                                )
                    except (json.JSONDecodeError, TypeError, KeyError):
                        pass
                    if not _main_helper_completion_checkpoint_emitted:
                        _completion = _delegate_completion_checkpoint_from_result(result)
                        if _completion:
                            _main_helper_completion_checkpoint_emitted = True
                            _append_tool_loop_dynamic_guidance(
                                msgs,
                                main_helper_completion_checkpoint(
                                    iteration=it,
                                    files=_completion.get("files") or [],
                                    facts=_completion.get("facts") or [],
                                    warning_count=int(_completion.get("warning_count") or 0),
                                    contract_snapshot=_current_task_contract_snapshot(),
                                ),
                            )
                            debug.log(
                                "llm.tools.main_helper_completion_checkpoint",
                                (
                                    f"files={_completion.get('files') or []} "
                                    f"facts={_completion.get('facts') or []} "
                                    f"warnings={_completion.get('warning_count') or 0} iter={it}"
                                ),
                            )
                    try:
                        for _fact in _retryable_delegate_facts_from_result(result):
                            _tr = _fact.get("terminal_reason")
                            _tid = _fact.get("task_id", "?")
                            if _tid not in _injected_for_tasks:
                                _injected_for_tasks.add(_tid)
                                _params = _fact.get("params") or {}
                                _rationale = _fact.get("rationale", "")
                                _retryable_next_action_iter = it
                                if _tid not in _retryable_next_action_tasks:
                                    _retryable_next_action_tasks.append(_tid)
                                _retryable_next_action_facts.append(_fact)
                                _params_json = _neutral_round3_gap_text(json.dumps(_params, ensure_ascii=False))
                                _append_tool_loop_dynamic_guidance(
                                    msgs,
                                    (
                                        f"[SYSTEM_HINT/auto_retry] Processing step '{_tid}' terminated "
                                        f"with reason '{_neutral_round3_gap_text(_tr)}'.\n"
                                        f"  Recovery rationale: {_neutral_round3_gap_text(_rationale)}\n"
                                        f"  Recovery parameters: {_params_json}\n"
                                        f"  This is a factual recovery option, not a forced decision. Before finalizing, compare the "
                                        f"current task contract, verified evidence, missing outputs, and user interruption state; then "
                                        f"decide whether to resume the same task_id, split/escalate, use sufficient existing evidence, "
                                        f"or report an explicit incomplete state.\n\n"
                                        f"处理步骤未完成时只陈述恢复事实；由模型结合任务契约、证据和缺口决定续作、拆分/升级、采用已有证据或说明未完成。"
                                    ),
                                )
                                debug.log(
                                    "llm.tools.next_action_injected",
                                    f"task={_tid} reason={_tr}",
                                )
                    except (json.JSONDecodeError, TypeError, KeyError):
                        pass
                # 把 set 保留下来给下一 iter
                _injected_next_action_tasks = _injected_for_tasks

            # ── 2026-05-02 part10 (A1):每 5 轮跑一次软压缩 ──
            # 针对**特定工具**的语义级冗余折叠:
            #   - read_file 同 path 旧范围被新范围包含 → 旧的折叠
            #   - workspace.run 同 command 重复 ≥3 次 → 最早的折叠
            #   - delegate 已完成结果(helper excerpt 已抽过) → 折叠成统计摘要
            # 比 _fold_old_tool_messages 的"按年龄/大小"更细 — 只压**真冗余**的,
            # 保留所有**还有信息价值**的 tool result(模型可能需要)。
            # 失败兜底:任何异常吞掉,主流程继续。
            if task_id is None and it > 0 and it % 5 == 0:
                try:
                    _soft_compact_redundant_tool_results(msgs)
                except Exception:
                    log.exception("soft compact failed (non-fatal)")

            # ── 主线程 stuck detector (2026-05-02 加, log trace 150eb2f2 教训) ──
            # 实测主线程 lite 反复 edit_file 改 C89 兼容,完全没读 gcc 报错的
            # "use option -std=c99" hint。修了 5 次代码后才在第 6 次加编译参数。
            # 主线程之前没有 stuck detector(只 helper 有),现在补上 — 反复同一种错时
            # 注入一条 system 提示让 LLM 强制反思:
            #   1) 最近的失败 / 错误信息究竟说了什么?
            #   2) 是不是要换思路而不是继续改?
            #   3) 是不是该向用户承认无法完成?
            #
            # 这不会阻塞 — 只是给 LLM 多一个明确信号,让它从"刷工具循环"跳出。
            #
            # 2026-05-04 v19.3:helper 还在跑时跳过 stuck 检测。
            # trace 4a3c8973 主线程 35 次 stuck 全是噪声 — 主线程在等 helper
            # 产 CSV,反复 poll processes.list 被 StuckDetector 当"无进展"。
            # helpers 活跃时主线程的工具调用本质是"管理等结果",不应判 stuck。
            _skip_stuck_check = False
            try:
                from app.core.core_processes import registry as _proc_reg
                if await _proc_reg().count_active_helpers() > 0:
                    _skip_stuck_check = True
            except Exception:
                pass
            if not _skip_stuck_check:
                stuck_main.record_batch(results)
                if stuck_main.stuck:
                    # ── 2026-05-04 Bug #20 修复 ──
                    # 旧版:count==1 时每 iter 都注入反思提示(实测 31 次反复)。
                    # 旧版:stuck 持续 True 时 _prev_stuck_state 一直 True,
                    #       _stuck_trigger_count 永远卡在 1,永不升级 abort。
                    # 新版:(a) 只在上升沿注入反思 (b) 上升沿计数升级
                    #       (c) 同周期持续 >=5 iter 也强制 abort (防 stuck 一直 True 卡死)
                    is_rising_edge = not _prev_stuck_state
                    if is_rising_edge:
                        _stuck_trigger_count += 1
                        _stuck_consecutive_iters = 1
                    else:
                        _stuck_consecutive_iters += 1

                    if is_rising_edge and _stuck_trigger_count == 1:
                        _stuck_msg = repeated_failure(stuck_main.stuck_reason)
                        _append_tool_loop_dynamic_guidance(msgs, _stuck_msg)
                        debug.log(
                            "llm.tools.stuck.warned_main",
                            f"injected stuck reflection prompt at iter {it}: "
                            f"{stuck_main.stuck_reason}",
                        )
                        if upgrade_signal is not None and not upgrade_signal.get("should_upgrade"):
                            upgrade_signal["should_upgrade"] = True
                            upgrade_signal["reason"] = (
                                f"主线程 stuck detector 触发: {stuck_main.stuck_reason}"
                            )
                            upgrade_signal["evaluated_at_iter"] = it
                            upgrade_signal["force"] = True
                    elif _stuck_trigger_count >= 2 and abort_event is not None and not abort_event.is_set():
                        if (
                            helper_kind is None
                            and _retryable_next_action_iter == it
                            and _retryable_next_action_deferred < _retryable_next_action_max_defers
                        ):
                            _retryable_next_action_deferred += 1
                            _stuck_trigger_count = 1
                            _stuck_consecutive_iters = 0
                            _prev_stuck_state = False
                            _retry_tasks = ", ".join(_retryable_next_action_tasks[-4:]) or "unknown"
                            _append_tool_loop_dynamic_guidance(
                                msgs,
                                retry_before_finalize(_retry_tasks),
                            )
                            debug.log(
                                "llm.tools.stuck.retry_deferred",
                                f"defer abort at iter {it}: trigger_count={_stuck_trigger_count} "
                                f"tasks={_retry_tasks} defer={_retryable_next_action_deferred}",
                            )
                        else:
                            log.warning(
                                "main thread stuck x%d; injecting strategy recovery hint: %s",
                                _stuck_trigger_count, stuck_main.stuck_reason,
                            )
                            _append_tool_loop_dynamic_guidance(
                                msgs,
                                strategy_recovery(stuck_main.stuck_reason),
                            )
                            _stuck_trigger_count = 1
                            _stuck_consecutive_iters = 0
                            _prev_stuck_state = False
                            if upgrade_signal is not None and not upgrade_signal.get("should_upgrade"):
                                upgrade_signal["should_upgrade"] = True
                                upgrade_signal["reason"] = (
                                    f"主线程 repeated failure recovery: {stuck_main.stuck_reason}"
                                )
                                upgrade_signal["evaluated_at_iter"] = it
                                upgrade_signal["force"] = True
                            debug.log(
                                "llm.tools.stuck.strategy_recovery",
                                f"injected strategy recovery at iter {it}",
                            )
                    elif _stuck_consecutive_iters >= 5 and abort_event is not None and not abort_event.is_set():
                        if (
                            helper_kind is None
                            and _retryable_next_action_iter == it
                            and _retryable_next_action_deferred < _retryable_next_action_max_defers
                        ):
                            _retryable_next_action_deferred += 1
                            _stuck_consecutive_iters = 0
                            _prev_stuck_state = False
                            _retry_tasks = ", ".join(_retryable_next_action_tasks[-4:]) or "unknown"
                            _append_tool_loop_dynamic_guidance(
                                msgs,
                                retry_before_finalize(_retry_tasks, short=True),
                            )
                            debug.log(
                                "llm.tools.stuck.retry_deferred",
                                f"defer consecutive abort at iter {it}: "
                                f"consecutive={_stuck_consecutive_iters} tasks={_retry_tasks} "
                                f"defer={_retryable_next_action_deferred}",
                            )
                        else:
                            log.warning(
                                "main thread stuck for %d consecutive iters at trigger=%d; injecting strategy recovery hint: %s",
                                _stuck_consecutive_iters, _stuck_trigger_count, stuck_main.stuck_reason,
                            )
                            _append_tool_loop_dynamic_guidance(
                                msgs,
                                strategy_recovery(stuck_main.stuck_reason, consecutive=True),
                            )
                            _stuck_trigger_count = max(1, _stuck_trigger_count)
                            _stuck_consecutive_iters = 0
                            _prev_stuck_state = False
                            if upgrade_signal is not None and not upgrade_signal.get("should_upgrade"):
                                upgrade_signal["should_upgrade"] = True
                                upgrade_signal["reason"] = (
                                    f"主线程 consecutive failure recovery: {stuck_main.stuck_reason}"
                                )
                                upgrade_signal["evaluated_at_iter"] = it
                                upgrade_signal["force"] = True
                            debug.log(
                                "llm.tools.stuck.strategy_recovery",
                                f"injected consecutive strategy recovery at iter {it}",
                            )
                else:
                    _stuck_consecutive_iters = 0  # stuck 清零
                _prev_stuck_state = stuck_main.stuck

                # ── 2026-05-02 part13:soft hint(edit-without-verify 等)──
                # 不算 stuck,但模型在做无效循环(edit 不 compile / read 不 act)。
                # 注入一条引导性 system msg,模型自己消化决定是否调整。每种 hint
                # 只发一次,避免刷屏。
                _soft_hint = stuck_main.consume_soft_hint()
            else:
                _soft_hint = None
            if _soft_hint:
                _append_tool_loop_dynamic_guidance(msgs, _soft_hint)
                debug.log(
                    "llm.tools.soft_hint",
                    f"injected soft hint at iter {it}: {_soft_hint[:80]}",
                )

            # ── 2026-05-15 修(F841 lint 发现): _IdleDetector 早已写好但
            # 从未接入。本段把 record_iter / should_inject_warning 接入 iter 循环。
            # 设计意图(class _IdleDetector 注释):主线程如果连续 ≥3 iter 只调
            # processes.list (其他工具都不调),即"在空等"——本该 spawn 新 helper
            # 或 collect 已 done 结果或直接出 JSON 收尾,但反而一直 poll 老 helper
            # 状态。这种循环不算 stuck (工具调用成功 + 有进展), 但本质是浪费 LLM
            # 调用。注入"Idle Penalty"system 警告让模型转向。
            try:
                idle_detector.record_iter(it, results)
                _idle_warning = idle_detector.should_inject_warning(it)
                if _idle_warning:
                    _append_tool_loop_dynamic_guidance(msgs, _idle_warning)
                    debug.log(
                        "llm.tools.idle_penalty",
                        f"injected idle penalty warning at iter {it}: "
                        f"consecutive_idle={idle_detector.consecutive_idle_iters}",
                    )
            except Exception:
                # idle detector 失败不能阻塞主流程,只 log 不抛
                log.exception("idle_detector wiring failed at iter %d", it)

            # ── helper 心跳: 汇报本轮调用的工具名 (主线程能看到 helper 在干什么) ──
            # Patch 05: 主线程跳过(no helper proc_id),避免无意义 task 创建。
            try:
                from app.core.core_processes import (
                    report_helper_progress, current_helper_proc_id,
                )
                if current_helper_proc_id() is not None:
                    from app.core.bg_tasks import schedule

                    # 每个工具都推一次,registry 里 recent_tools deque 会保留最后 8 个
                    for _, _name, _result, _args in results:
                        bg_tasks.append(schedule(
                            report_helper_progress(tool_name=_name),
                            name=f"helper_progress:tool:{task_id or 'unknown'}",
                        ))
            except Exception:
                pass

        # 走到这里有两条路径:
        #   - 用户 abort:abort_event 被 set,需要交付目前为止的成果
        #   - 触达 HARD_ITER_CAP:lite 困住没收尾,需要兜底 finalize
        # 两者在 API 调用层处理一致,prompt 略有差异(语气)。
        # 历史教训(trace 966f9fac):pro 模型在 forced finalize 偶发输出
        # <｜｜DSML｜｜tool_calls> XML 而非 JSON,导致 57 轮工作全部丢失。
        # 修复:tool_choice="none" 显式禁工具 + response_format=json_object 在 API
        # 层强制(替代 prompt 里说"不要输出 XML"——那种反指令会 prime 模型想 XML)。

        # Bug 2: forced finalize 路径上也评估升级撤销(只看 successful_after_signal,
        # 因为这种情况不算 natural_stop)
        _maybe_clear_stale_upgrade(
            upgrade_signal, successful_after_signal,
            natural_stop=False,
        )

        if finalize_kind == "text_summary":
            # helper 的收尾路径——纯文本进度报告,不是 ResponsePlan JSON
            debug.log(
                "llm.tools.forced_finalize.trigger",
                f"kind=text_summary hit_cap={hit_cap} trigger={forced_finalize_trigger} "
                f"task_id={task_id}",
            )
            if hit_cap:
                finalize_reason = (
                    "The helper reached the iteration safety cap. Stop tool use and produce a text progress summary now."
                )
                debug.log("llm.tools.cap.finalize.text", "iter cap reached (helper)")
            else:
                finalize_reason = (
                    "The main process requested interruption. Stop tool use and produce a text progress summary now."
                )
                debug.log("llm.tools.aborted.text", "interrupted (helper)")
            finalize_msgs = msgs + [{
                "role": "system",
                "content": (
                    f"{finalize_reason}\n"
                    "Output plain text, not JSON. Include completed work, remaining gaps, important artifact paths, "
                    "and enough detail for a later resume=true continuation. For code-generation tasks, include the "
                    "complete relevant code block when no file path is available. Keep it concise but preserve useful evidence.\n\n"
                    "中断或触顶时输出可续作的纯文本进度总结。"
                ),
            }]
            finalize_kwargs = dict(
                model=model,
                messages=finalize_msgs,
                stream=False,
                tool_choice="none",
                extra_body=extra_body,  # 不强制 JSON 格式
            )
            finalize_kwargs = _sanitize_tool_choice_for_thinking(
                finalize_kwargs,
                provider=model_spec.provider if model_spec else None,
                label="helper text finalize",
            )
            final_content = ""
            # ── Phase 5++ 修(v2): helper abort 也提取真实分析 ──
            # v1 错误: 只列工具名 "workspace → workspace → write" — 主线程拿不到任何分析。
            # v2 正确: 提取 helper 最后一段思考 + 工具实际产出 + 文件,
            #         拼成有信息量的报告。主线程能基于这些做后续决策(resume / 替代方案)。
            if not hit_cap:
                debug.log(
                    "llm.tools.helper_finalize.abort_extract",
                    "helper abort: extracting real analysis from msgs (no LLM call)",
                )
                import re as _re

                # 1. 提取最后一段 assistant 思考(被中断前在做什么)
                last_thought = ""
                for m in reversed(msgs):
                    if m.get("role") == "assistant":
                        c = str(m.get("content") or "").strip()
                        if c and len(c) > 20:
                            last_thought = c[:600]
                            break

                # ── 2026-05-04 Bug #18 修复 ──
                # phase 1 reasoning=disabled 的 helper 几乎不输出 content,
                # last_thought 会是空,abort_extract 报告"thought=0 chars"。
                # 兜底:遍历最近 20 条 tool_call args,提取 path / command 字符串
                # 拼成"它想做什么"的证据。bwt_final 真实 trace 有 5 个 read_file
                # 工具调用 args,path 字段足以拼出"它在试图读 helper_bwt_bwt.c
                # 但路径都错了" — 比 0 字符的 thought 信息量高 100 倍。
                last_tool_attempts: list[str] = []
                if not last_thought:
                    for m in msgs[-20:]:
                        if m.get("role") != "assistant":
                            continue
                        for tc in (m.get("tool_calls") or [])[:3]:
                            fn = (tc.get("function") or {}).get("name", "?")
                            args_raw = (tc.get("function") or {}).get("arguments", "")
                            try:
                                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                            except Exception:
                                args = {}
                            # 提取最有信息量的字段:command / path / files / action
                            hint = ""
                            for k in ("command", "path", "action", "files", "task_id", "content"):
                                v = args.get(k) if isinstance(args, dict) else None
                                if v:
                                    if isinstance(v, list):
                                        v = ", ".join(str(x) for x in v[:3])
                                    hint = f"{k}={str(v)[:80]}"
                                    break
                            entry = f"{fn}({hint})" if hint else fn
                            last_tool_attempts.append(entry)
                    last_tool_attempts = last_tool_attempts[-8:]  # 最近 8 次尝试

                # 2. 提取最近工具调用名链(给主线程看进度)
                recent_tools_summary = []
                for m in msgs[-10:]:
                    if m.get("role") == "assistant" and m.get("tool_calls"):
                        for tc in (m.get("tool_calls") or [])[:2]:
                            fn = (tc.get("function") or {}).get("name", "?")
                            recent_tools_summary.append(fn)
                tools_str = " → ".join(recent_tools_summary[-6:]) if recent_tools_summary else "(无工具调用)"

                # 3. 提取产出文件
                files = []
                for m in msgs[-15:]:
                    if m.get("role") != "tool":
                        continue
                    content = str(m.get("content", ""))
                    for match in _re.finditer(r'"saved_path":\s*"([^"]+)"', content):
                        files.append(match.group(1).split("/")[-1].split("\\")[-1])
                    if "committed_files" in content or '"files"' in content or '"outputs"' in content:
                        for match in _re.finditer(
                            r'"(?:committed_files|files|outputs)":\s*\[([^\]]+)\]',
                            content,
                        ):
                            for file_match in _re.finditer(
                                r'"([^"]+\.(?:c|cpp|h|py|md|txt|json|docx|pptx|xlsx|pdf|png|jpg|svg|html|css|js|ts))"',
                                match.group(1),
                            ):
                                files.append(file_match.group(1).split("/")[-1].split("\\")[-1])
                files = list(dict.fromkeys(files))[:8]

                # 4. 组装报告 — 前面是真实分析,后面是元信息
                # ── 2026-05-04 Bug #15 修复 ──
                # 加固定 ABORT_EXTRACT 头,让下游 helper / 主线程知道这是工具层 fallback
                # 抠出来的,未经 LLM 总结。不再被当 first-class 报告盲信。
                parts = [
                    "[ABORT_EXTRACT v1] This report was assembled by the tool layer from the message stream; the helper did not complete an LLM final summary. "
                    "Generated-file mentions may be incomplete or unverified, so verify artifacts before using them.\n\n"
                    "工具层中断摘录；文件和结论需后续验证。",
                ]
                if last_thought:
                    parts.append(f"## Recent Reasoning Excerpt\n{last_thought}")
                elif last_tool_attempts:
                    # Bug #18 兜底:0 字 thought 时,用 tool_call args 拼接
                    parts.append(
                        "## Recent Tool Attempts (extracted from tool-call arguments; no LLM final summary)\n"
                        + "\n".join(f"- {t}" for t in last_tool_attempts)
                    )
                if files:
                    parts.append(f"## Mentioned Generated Files\n{', '.join(files)}")
                parts.append(
                    f"## Interruption State\n"
                    f"Recent tool sequence before interruption: {tools_str}\n"
                    f"The workspace was preserved; resume the same task with resume=true when useful.\n\n"
                    f"中断状态：工作区已保留，可同任务续作。"
                )
                final_content = "\n\n".join(parts)
                debug.log(
                    "llm.tools.final_forced.text",
                    f"helper abort - extracted: thought={len(last_thought)} chars, "
                    f"{len(files)} files, "
                    f"{len(last_tool_attempts)} tool_attempts",
                    final_content[:500],
                )
                _clear_transient_guidance_before_return("return_helper_abort_extract")
                return (final_content, msgs)

            # cap 路径(60 轮触顶)— 走完整 LLM 总结
            finalize_timeout = 60.0
            try:
                finalize_tag = _log_nonstream_prompt_shape(
                    suffix="helper_text_finalize",
                    call_messages=finalize_msgs,
                )
                final = await asyncio.wait_for(
                    _retry(
                        lambda: _cli_container[0].chat.completions.create(**finalize_kwargs),
                        label="helper text finalize",
                        provider=model_spec.provider if model_spec else None,
                    ),
                    timeout=finalize_timeout,
                )
                _record_nonstream_response_usage(final, tag=finalize_tag)
                final_content = final.choices[0].message.content or ""
            except asyncio.TimeoutError:
                log.warning(
                    "helper text finalize timed out after %ds — using fallback",
                    finalize_timeout,
                )
                final_content = (
                    "(This helper reached the iteration cap and finalization also timed out. "
                    "The workspace is preserved; the main process can resume this task with resume=true. "
                    "helper 已触达迭代上限且收尾超时；工作区保留，可同任务续作。)"
                )
            except Exception:
                log.exception("helper text finalize failed")
                final_content = "(Helper reached the cap and progress summarization failed. Workspace is preserved for resume.)"
            debug.log("llm.tools.final_forced.text", "helper finalized", final_content[:500])
            _clear_transient_guidance_before_return("return_helper_forced_finalize")
            return (final_content, msgs)

        # json_plan 路径(主流程 round2 用)
        debug.log(
            "llm.tools.forced_finalize.trigger",
            f"kind=json_plan hit_cap={hit_cap} trigger={forced_finalize_trigger} "
            f"task_id={task_id}",
        )
        if hit_cap:
            finalize_reason = (
                "The tool-call cap has been reached. Produce the final JSON object from current evidence without further tool calls. "
                "工具调用触顶；基于已有证据输出 JSON。"
            )
            debug.log("llm.tools.cap.finalize", "iter cap reached, forcing finalize")
        else:
            finalize_reason = (
                "The user interrupted the task. Produce the final JSON object from current evidence without further tool calls. "
                "Keep intent concise. 用户中断任务；基于已有证据输出 JSON。"
            )
            debug.log("llm.tools.aborted", "user interrupted, forcing finalize")

        finalize_msgs = msgs + [{
            "role": "system",
            "content": (
                f"{finalize_reason}\n"
                "- Put acquired data/files into key_points and deliverables.\n"
                "- Describe completed work honestly in intent.\n"
                "- Record unfinished work in internal_note without inventing facts.\n"
                "- Strict JSON only, no markdown or surrounding text.\n\n"
                "被中断或触顶时基于已有证据输出最终 JSON。"
            ),
        }]
        finalize_kwargs = dict(
            model=model,
            messages=finalize_msgs,
            stream=False,
            tool_choice="none",
            extra_body={**(extra_body or {}), "response_format": {"type": "json_object"}},
        )
        finalize_kwargs = _sanitize_tool_choice_for_thinking(
            finalize_kwargs,
            provider=model_spec.provider if model_spec else None,
            label="tools finalize",
        )
        final_content = ""
        # ── Phase 5++ 修(v2): abort 路径直接给 round3 真实分析内容 ──
        # v1 错误: 跳过 LLM 后塞了 "用户中断了任务" + tone="简短" 这种 status 文字,
        #         覆盖了人设、丢失了真实分析内容。
        # v2 正确做法: 从 msgs 里提取 helper/工具实际产出的**真实分析**和**已交付物**,
        #              填进 plan,但 internal_note 告诉 round3 "本次被中断,自然衔接",
        #              tone/length 留空 → round3 用人设默认风格。
        # 这样 round3 收到的是:
        #   - 真实做了什么(intent + key_points 来自最后一段 assistant 思考 + tool 实际输出)
        #   - 真实产出了什么文件(deliverables)
        #   - "被中断"的事实(internal_note,让人设自然反应,例如吐槽/接下话茬)
        # Round3 流式生成时,LLM 会自然综合这些信息 + 人设给出回复。
        if not hit_cap:
            debug.log(
                "llm.tools.finalize.abort_to_round3",
                "abort: building plan from msg history (no LLM call), letting round3 do persona summary",
            )
            import re as _re

            # 1. 提取最后一段有内容的 assistant 思考(模型在被中断前在分析什么)
            last_assistant_text = ""
            for m in reversed(msgs):
                if m.get("role") == "assistant":
                    content = str(m.get("content") or "").strip()
                    if content and len(content) > 20:  # 跳过空/极短的(可能是只有 tool_calls)
                        last_assistant_text = content[:400]
                        break

            # 2. 提取 tool 结果里的真实产出文件
            #
            # 2026-05-10 Patch 75: 过滤内部摘要/元数据文件
            # 病因(trace debug_20260510_134607,trace 6c60898a160b4f6c):abort 路径从
            # 消息历史抠 deliverables 时,把 `.helper_cpp_join_full_report.txt`
            # `.helper_csharp_join_full_report.txt` 这种 P32 加的 helper 内部摘要文件
            # 当成 deliverable 推送给用户。
            # 修法:统一过滤以 `.` 开头(隐藏元数据文件)和 `_helpers_shared/` /
            # `_shared/` 前缀(共享脚手架,不是产物)的文件名。
            def _is_internal_file(name: str) -> bool:
                """过滤 helper 内部摘要 / 元数据 / 共享脚手架,这些不是用户 deliverable。"""
                if not name:
                    return True
                base = name.replace("\\", "/").split("/")[-1].lower()
                # 隐藏元数据文件:.helper_xxx_full_report.txt / .helpers_displayed_name.json /
                # .user_fetched_files.json / .rewrite_count.json / .session_manifest.json 等
                if name.startswith("."):
                    return True
                if base.startswith("."):
                    return True
                # Tool-layer extraction is not an LLM decision, so keep it conservative
                # around helper probes and generated command scripts.
                if base.startswith("helper_"):
                    return True
                if _re.match(r"^_py_cmd_[0-9a-f]{6,}\.py$", base):
                    return True
                # 共享脚手架,不是产物
                if name.startswith("_helpers_shared/") or name.startswith("_shared/"):
                    return True
                if name.startswith("_helpers_shared\\") or name.startswith("_shared\\"):
                    return True
                return False

            # Only verified current-run files should reach Round3 as deliverables.
            # Generic fields such as saved_path/path/files inside failed helpers are
            # recovery evidence, not proof that a user-facing artifact exists.
            recent_files = _recent_verified_files_from_tools(msgs, limit=30)[:8]

            # 3. 提取最近 helper 报告(若有 delegate 调用,helper 的 report 是核心分析)
            # 2026-05-03 v18 修复(trace 85e330f3d6f04985 实测教训):
            # 旧逻辑直接提取所有 "report" 字段,但 helper interrupted 时报告内容是
            # "[本次执行被打断,工作区已保留。主进程后续可对同 task_id=X 用 resume=true 续作]"
            # 这种**给主线程看的自我提示**,**不是给用户看的成果**。
            # round3 看到 key_points 装这种文字后可能复读给用户,极差体验。
            # 修复:
            #   (a) 过滤含"被打断"/"resume=true"/"中断状态"/"被中断前"等 sentinel 的报告
            #   (b) 优先提取 commit_to_main 工具调用的真实成果(被持久化到主区的文件)
            helper_findings = []
            for m in msgs[-10:]:
                if m.get("role") != "tool":
                    continue
                content = str(m.get("content", ""))
                # delegate 结果含 "results": [{"report": "..."}].
                # Parse structurally so failed/frozen/interrupted helpers cannot
                # be reintroduced as successful findings by a loose regex.
                for item in _delegate_items_from_result(content):
                    if _delegate_item_is_incomplete(item):
                        continue
                    report = item.get("summary") or item.get("report") or ""
                    if not isinstance(report, str):
                        continue
                    finding = report.strip()[:200]
                    if len(finding) < 20:
                        continue
                    # 过滤 helper 中断自我提示(给主线程看的,不是给用户的成果)
                    sentinels = (
                        "本次执行被打断", "resume=true", "可让我从此处继续",
                        "中断状态", "被中断前", "工作区已保留",
                        "(无工具调用)", "(无产出)",
                        "[INCOMPLETE_HELPER_RESULT]",
                    )
                    if any(s in finding for s in sentinels):
                        continue
                    if finding and finding not in helper_findings:
                        helper_findings.append(finding)
            helper_findings = helper_findings[:3]  # 最多 3 条

            # 3b. 提取 commit_to_main 真实成就(主区已持久化的文件)
            # commit_to_main 工具调用成功是**最可靠**的"做了什么"信号,因为它代表
            # 文件实际进了主工作区。比 helper 自报告 hedge 程度低。
            committed_files_real = _committed_files_from_recent_tools(msgs, limit=30)[:10]

            retry_facts = []
            for m in msgs[-30:]:
                if m.get("role") != "tool":
                    continue
                retry_facts.extend(_retryable_delegate_facts_from_result(m.get("content", "")))
            seen_retry = set()
            retry_facts_unique = []
            for fact in retry_facts:
                key = (
                    fact.get("task_id"),
                    fact.get("terminal_reason"),
                    fact.get("next_action_type"),
                )
                if key in seen_retry:
                    continue
                seen_retry.add(key)
                retry_facts_unique.append(fact)
            retry_facts = retry_facts_unique[:4]

            # 4. 构造真实 intent。
            # 若 delegate 已明确返回可续作失败,这是比 assistant 中间思考更可靠的状态事实。
            # 不再把“我换个思路/我先看看”这类内部草稿当 round3 intent,
            # 否则会让最终回复像是在失败后重新假装开始。
            def _looks_like_stage_draft(text: str) -> bool:
                draft = (text or "").strip()
                if not draft:
                    return False
                lower = draft.lower()
                stage_markers = (
                    "先看一下", "先看看", "让我先", "我先", "接下来", "下一步",
                    "逐个实现", "分步推进", "开始检查", "检查现有代码",
                    "look at", "take a look", "start by", "next step",
                    "let me first", "i will", "i'll",
                )
                completion_markers = (
                    "已完成", "完成了", "验证通过", "测试通过", "已生成",
                    "implemented", "completed", "verified", "passed",
                )
                return (
                    len(draft) <= 220
                    and any(marker in lower or marker in draft for marker in stage_markers)
                    and not any(marker in lower or marker in draft for marker in completion_markers)
                )

            if retry_facts and not committed_files_real:
                tasks = ", ".join(str(f.get("task_id") or "?") for f in retry_facts)
                intent = f"本轮任务尚未完成; 处理步骤 {tasks} 已失败并给出续作/升级重试建议"
            elif (
                last_assistant_text
                and not _looks_like_unparsed_tool_markup(last_assistant_text)
                and not _looks_like_stage_draft(last_assistant_text)
            ):
                intent = last_assistant_text
            elif helper_findings:
                intent = f"已完成阶段性工作: {helper_findings[0]}"
            else:
                intent = "本轮处理未完成,尚未取得可核查结果"

            # 5. key_points 来自真实分析 + 产出列表
            # v18:优先用 commit_to_main 真实成就(最可靠),其次 helper_findings(已过滤),
            # 最后才是 recent_files(可能含未 verify 的中间文件)
            key_points = []
            if committed_files_real:
                key_points.append(
                    f"已固化到主区的成果: {', '.join(committed_files_real)}"
                )
            if helper_findings:
                key_points.extend(helper_findings)
            if retry_facts:
                for fact in retry_facts:
                    missing = _round3_gap_missing_items(fact.get("outputs_missing"))
                    missing_text = f"; 缺失产物: {', '.join(missing)}" if missing else ""
                    reason_text = (
                        f"; 原因: {_neutral_round3_gap_text(fact.get('stuck_reason'))}"
                        if fact.get("stuck_reason") else ""
                    )
                    key_points.append(
                        f"处理步骤 {fact.get('task_id')} {_neutral_round3_gap_text(fact.get('terminal_reason'))}, "
                        f"建议 {fact.get('next_action_type')} mode={fact.get('mode')}{reason_text}{missing_text}"
                    )
            if recent_files and not committed_files_real:
                # 没真实 commit 时才退而求其次提中间文件
                key_points.append(f"中间产出文件: {', '.join(recent_files)}")
            if not key_points:
                key_points.append(
                    "This turn produced no verifiable tool result. Do not state exact numbers, tables, file-content conclusions, or completion claims.\n本轮没有可核查工具结果，不能声称完成或给出具体结论。"
                )
            # 如真的什么都没,key_points 就空,round3 用 intent 即可

            final_plan = {
                "intent": intent,
                "key_points": key_points,
                # tone / length_hint 故意不填 — round3 会用人设默认值
                "deliverables": recent_files,
                # ── 2026-05-04 Bug #1 修复 ──
                # 旧版无脑写"本次任务被用户主动中断",但 abort 触发源除了用户 abort,
                # 还包括:helper stuck → 本地 abort、resume_preempt_timeout、cap、
                # tool_choice 强制等。helper 自己 stuck 也会让 plan.internal_note
                # 写"用户主动中断",误导上游(orchestrator + bot_log)给用户看到
                # "你已经停了"的语气,实际用户根本没按停 → 体验灾难。
                # 修法:abort path 的 internal_note 改成中性描述,不强言"用户主动",
                # 由 orchestrator 通过 abort_ch.gen 判定真假 abort,各自走对应路径。
                "internal_note": (
                    "This task ended early during Round2 through the forced-finalize path. Possible causes include user interruption, long background-task inactivity, or tool-call budget pressure. "
                    "Round3 should continue naturally in persona, mention the interruption or pause when relevant, and avoid pretending the task completed successfully. Keep apologies brief. "
                    "Workspace files are preserved, so the main thread can resume later with resume=true.\n"
                    "Round2 提前结束时，Round3 按人设说明暂停/中断事实，不假装完成；工作区可续作。"
                ),
            }
            final_content = json.dumps(final_plan, ensure_ascii=False)
            final_content = _merge_delegate_gap_facts_into_response_plan_json(final_content)
            debug.log(
                "llm.tools.final_forced",
                f"abort path: plan built from history "
                f"(intent={len(intent)} chars, "
                f"{len(key_points)} key_points, {len(recent_files)} files)",
                final_content[:300],
            )
            _clear_transient_guidance_before_return("return_abort_json_plan")
            return (final_content, msgs)

        # cap 路径: 走完整 finalize(60s timeout)
        finalize_timeout = 60.0
        try:
            finalize_tag = _log_nonstream_prompt_shape(
                suffix="forced_finalize",
                call_messages=finalize_msgs,
            )
            final = await asyncio.wait_for(
                _retry(
                    lambda: _cli_container[0].chat.completions.create(**finalize_kwargs),
                    label="tools finalize",
                    provider=model_spec.provider if model_spec else None,
                ),
                timeout=finalize_timeout,
            )
            _record_nonstream_response_usage(final, tag=finalize_tag)
            final_content = final.choices[0].message.content or ""
        except asyncio.TimeoutError:
            log.warning(
                "forced finalize timed out after %ds — falling back to recovery path",
                finalize_timeout,
            )
        except Exception:
            log.exception("forced finalize first attempt failed")

        # 兜底：仍非 JSON → 用 chat_json_with_upgrade 自适应重写
        # （先 lite 试，若 lite 输出仍不像 JSON 自动升 main + max thinking）
        # cap 路径才需要(abort 上面已 return)
        if not final_content.strip().startswith("{"):
            # cap 路径:工作真的跑到上限,值得花时间生成好回复
            debug.log("llm.tools.finalize.recover", "non-JSON output, recovery with upgrade")
            try:
                history = _summarize_tool_history(msgs)
                trigger_desc = "工具调用上限触达"
                recover_msgs = [
                    {"role": "system", "content": (
                        f"The workflow stopped because `{trigger_desc}` was reached. "
                        "The next user message contains completed tool calls and result summaries. "
                        "Convert that evidence into one ResponsePlan JSON object with fields such as "
                        "intent, key_points, tone, length_hint, and deliverables. Output only the JSON object, without tools or hidden reasoning.\n\n"
                        "工作流达到上限后，根据工具摘要转换为 ResponsePlan JSON。"
                    )},
                    {"role": "user", "content": history},
                ]
                # validate: 必须是 dict 且至少有 intent 字段，否则升级到 main+thinking
                def _v(raw):
                    return isinstance(raw, dict) and bool(raw.get("intent"))
                raw = await chat_json_with_upgrade(
                    recover_msgs, validate=_v, label="cap_recovery",
                )
                if raw is not None:
                    final_content = json.dumps(raw, ensure_ascii=False)
                else:
                    log.error("cap recovery: lite+main both failed; downstream will use _DEFAULT_PLAN")
            except Exception:
                log.exception("cap recovery error; downstream will use _DEFAULT_PLAN")

        final_content = _merge_delegate_gap_facts_into_response_plan_json(final_content)
        debug.log("llm.tools.final_forced", "after cap finalize", final_content)
        _clear_transient_guidance_before_return("return_cap_finalize")
        return (final_content, msgs)

    finally:
        # ── 关键修复:abort 后取消所有 background tasks ──
        # meta-judge 旁路、progress 反馈生成 都是 fire-and-forget 的 LLM 调用,
        # 如果不取消,即便用户 abort 了/对话已结束,这些 task 还会继续:
        # - 消耗 DeepSeek API 配额
        # - 调用完成后写 progress_queue(虽然没人读但占内存)
        # - 写 upgrade_signal(无害但浪费)
        # 用户报告的 bug:"用户打断后机器人输出了结果但没有停止继续思考及调用"
        # 主要就是这些 bg tasks 在 chat_with_tools_loop 返回后仍在跑。
        pending = [t for t in bg_tasks if not t.done()]
        if pending:
            debug.log(
                "llm.tools.bg_cancel",
                f"cancelling {len(pending)} background tasks "
                f"(meta-judge / progress feedback)",
            )
            for t in pending:
                t.cancel()
            # 等所有 task 真正退出,避免 "Task was destroyed but it is pending!" 警告
            try:
                await asyncio.gather(*pending, return_exceptions=True)
            except Exception:
                pass
