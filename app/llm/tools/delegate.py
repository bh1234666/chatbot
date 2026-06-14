"""

Helper LLM delegation tool.



Main model can spawn up to 8 parallel helper LLMs, each with:

- Own isolated workspace under _delegate_{task_id}/

  * 新任务(resume=False, 默认): 工作区清空 + 从主工作区复制

  * 续作(resume=True): 工作区保留,helper 看到上次的全部文件

- Limited tools (no recursive delegate)

- **完全无时间/迭代硬上限** —— helper 一直跑直到自然停止 / 协作中断 / 用户全局 abort



由主进程根据情况管理:

  - delegate.poll(proc_id) 看心跳判断 helper 状态

  - delegate.collect 主动等结果

  - delegate.kill_helper 协作中断(helper 仍出报告)

  - delegate.spawn(task_id=同, resume=true, prompt=新指示) 让 helper 沿新方向继续



═══ 两种打断源 ═══



**A. 用户全局 abort(`/v1/chat/abort` 或类似)— 直接硬杀,不出报告**

  - 触发:group_guard.abort_event.set()

  - 路径:主线程 chat_with_tools_loop 通过 racing 机制 cancel tool dispatch

          → handle_delegate 收到 CancelledError

          → except CancelledError 块(line ~1648):cancel 所有 helper_tasks → raise

          → helper task 抛 CancelledError → finally 清理 → task 进入 cancelled 状态

  - 语义:整个对话被用户取消,helper 直接抛弃,不浪费时间做 forced finalize

  - 工作区:磁盘文件保留(asyncio cancel 不影响磁盘),但通常无意义续作



**B. 主进程模型决策 kill / helper 自检 stuck — 协作中断,出报告**

  - 触发:

      1. 主进程通过 processes.kill / delegate(action="kill") 工具调用

      2. helper 内 StuckDetector 检测到反复失败,自己 set per-helper abort_event

  - 路径:per-helper abort_event.set()

          → helper 的 chat_with_tools_loop 在下次迭代顶部检测到 → break

          → forced_finalize(finalize_kind="text_summary"):

            喂 system "请用纯文本总结当前进展" → 模型再调一次 LLM(tool_choice=none)

            → 总结返回作为 helper 的最终 report

  - 语义:helper 主动汇报进度,主进程后续可决定 resume=true 续作 or 放弃

  - 工作区:永远保留



**force=true 已禁用**(2026-05-02): 主进程模型不再能通过工具调用硬杀 helper。

  原因:违反"由主进程根据情况管理"原则——硬杀就丢了进展,不如让 helper 出报告

  让模型自己决定是否 resume。**用户的全局 abort 不受此限制**(用户语义就是直接放弃)。



模型选择: medium 路径用 lite helpers, hard/veryhard 用 pro helpers

"""

from __future__ import annotations



import asyncio

import csv  # 2026-05-11 P12.F: outputs_check 用于 benchmark sanity check

import json

import logging

import os

import platform as _platform

import re

import shutil

import subprocess as _subprocess

import sys as _sys

import time

import uuid

from contextvars import ContextVar

from pathlib import Path



from app.config import settings

from app.core import debug

from app.core.locks import get_group_guard

from app.core.core_processes import (

    registry as proc_registry,

    current_owner,

    set_current_owner,

    reset_current_owner,

    current_spawn_queue,

    set_current_spawn_queue,

    reset_current_spawn_queue,

    set_current_abort_event,

    reset_current_abort_event,

    ProcessRegistry,

    # Kill gate (2026-05-05: 下沉到 processes.py,所有 kill 路径统一门禁)

    validate_kill_reason,

    KILL_REASON_SELF_CANT_DO,

    KILL_REASON_SELF_DONE,

    KILL_REASON_SIBLING_DONE,

    KILL_REASON_CONTENT_USELESS,

    KILL_REASON_API_STALL,

)

from app.llm.json_utils import TOOL_ARGS_JSON_BROKEN_HINT  # noqa: E402

from app.llm.tools.workspace import (

    clean_workspace_dir,

    copy_workspace_contents,

    enforce_workspace_capacity,

)



log = logging.getLogger(__name__)


DELEGATE_PROGRESS_SUMMARY_SYSTEM = (
    "Generate one short Chinese progress summary for each helper from heartbeat facts.\n"
    "\n"
    "## Requirements\n"
    "- Each summary should be no more than 40 Chinese characters.\n"
    "- If heartbeat is stale or tools repeat without new evidence, mark it as possibly stuck.\n"
    "- If tools vary, evidence grows, or the thought shows analysis, mark it as progressing.\n"
    "- Use user-friendly process wording and convert internal labels to outcome-level wording where possible.\n"
    "\n"
    "## Output Format\n"
    "Strict JSON, no markdown: {\"summaries\":{\"<task_id>\":\"<≤40字摘要>\"}}\n\n"
    "根据 helper 心跳事实生成一句中文进度摘要。"
)


def _delegate_progress_summary_user_payload(active_helpers: list[dict]) -> str:
    helper_facts = []
    for h in active_helpers:
        helper_facts.append({
            "elapsed_seconds": round(float(h.get("elapsed_seconds") or 0.0), 1),
            "heartbeat_status": str(h.get("heartbeat_status") or "?"),
            "iter": int(h.get("iter") or 0),
            "last_thought": str(h.get("last_thought") or "")[:160],
            "recent_tools": list(h.get("recent_tools") or [])[-6:],
            "task_id": str(h.get("task_id") or "?"),
        })
    return (
        "## Runtime Facts\n"
        + json.dumps(
            {"helpers": helper_facts},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n\n输出每个 helper 的进度摘要。"
    )





async def chat_with_tools_loop(*args, **kwargs):

    from app.llm.client import chat_with_tools_loop as _chat_with_tools_loop

    return await _chat_with_tools_loop(*args, **kwargs)





def _copy_helper_debug_artifacts_to_main(*args, **kwargs):

    return None





async def _persist_pending_result(*args, **kwargs):

    return None





# helper 类型/模式/任务ID 分类已抽离到 helper_kinds.py(2026-05-20 重构);re-export 兼容。

from app.llm.tools.helper_kinds import (  # noqa: E402,F401

    _is_legacy_paired_task_id,

    _is_legacy_paired_hard_task,

    _is_scaffold_task,

    _has_non_text_implementation_output,

    _is_code_project_companion_output,

    _has_text_report_output,

    _text_report_outputs_only,

    _auto_correct_obvious_helper_kind,

    _filter_tools_for_kind,

    _normalize_helper_kind_mode,

    get_helper_config,

    VALID_HELPER_MODES,

    VALID_HELPER_KINDS,

    MODEL_VISIBLE_HELPER_KINDS,

    HELPER_CONFIGS,

)

LEGACY_HELPER_KINDS = ("final", "coding")

LEGACY_HELPER_MODES = ("normal", "final")

















# 工作区/路径工具已抽离到 workspace_utils.py(2026-05-20 重构);re-export 兼容。

from app.llm.tools.workspace_utils import (  # noqa: E402,F401

    _dir_size,

    _workspace_has_basename,

    _list_workspace_files,

    _extract_declared_files,
    _extract_reported_output_files,
    _has_malformed_output_files_attempt,

    _is_internal_helper_artifact,

    _matches_declared_output_via_mapping,

    _list_helper_workspace_for_prompt,

    take_workspace_snapshot,

    _derive_permanent_root,

    _disk_result_for_collect,

    _has_office_document_output,
    _filter_read_helper_expected_outputs,
    _is_internal_read_evidence_output,
    _collect_read_evidence_files,
    _build_read_evidence_summary,
    _rewrite_staged_read_evidence_mentions,
    _is_source_material_reference,

    _match_path_pattern,

    _validate_shared_scaffold,

    _is_shared_support_artifact,

)





from app.llm.tools.delegate_quality import (  # noqa: E402,F401

    blocking_quality_warnings,

    _extract_expected_text_tokens_for_document,

    _normalize_doc_text_for_match,

    _document_text_for_quality_check,

    document_source_grounding_warnings,

    academic_document_warnings,

    docx_table_structure_warnings,

    text_mojibake_warnings,

    source_data_approximation_warnings,

    document_structure_quantity_warnings,

    repair_common_mojibake_text,

    _pptx_slide_texts_for_quality_check,

    _zh_num_to_int,

    _extract_expected_ppt_slide_token_groups,

)

from app.llm.tools.delegate_resources import (  # noqa: E402,F401

    _parse_main_resource_request,

    _resource_task_prompt,

)











































# ── 2026-05-02 part12 (Bug C):用户原 message 语言 ContextVar ──

# orchestrator 在进入 round2 前 set,handle_delegate / handle_spawn_helper 读取

# 后传给 _run_one_helper,helper system prompt 末尾追加语言硬约束。

# 默认 'en' = 不注入额外约束(模型默认英文友好,无害)。

_current_user_lang: ContextVar[str] = ContextVar("_current_user_lang", default="en")





def set_current_user_lang(lang: str):

    """orchestrator 进入 round2 前调用,告知 delegate 链路用户原 message 语言。"""

    return _current_user_lang.set(lang)





def reset_current_user_lang(token):

    """配对 set_current_user_lang 的清理。"""

    _current_user_lang.reset(token)





def current_user_lang() -> str:

    """读取当前用户语言('zh' / 'en' / 'mixed')。"""

    return _current_user_lang.get()





# 2026-05-10 Patch 83: 人设守卫 ContextVar(orchestrator round2 入口 set)

# 病因:trace d808cfc509654d38(嘴臭混混人设 + "帮我写快排")—— P82 v3 把人设

# 注入移到 layered prompts 之前,LLM 仍然派 helper。Prompt 引导对 helpful-倾向

# 强的模型(DeepSeek)效果有限,需要**结构化硬约束**。

#

# 方案:在 spawn 入口启动一个**独立 lite LLM 守卫**,与 helper **并行**判断

# "当前派发是否应当运行"。守卫拦截 → cancel 整棵 trace 的 helper +

# 返回 guard_blocked 和自由理由给主线程；主线程可重规划或用 dispatch_reason 说明后重派。

#

# 设计原则:

# - 与 helper 同步并行,不阻塞 spawn(用户原话:"避免不拦截时浪费时间")

# - 守卫保守:不明确拒绝 → 默认放行(避免误杀正常请求)

# - 守卫失败 → 默认放行(可用性优先)

_current_persona_excerpt: ContextVar[str] = ContextVar(

    "_current_persona_excerpt", default=""

)

_current_tts_helper_persona: ContextVar[str] = ContextVar(

    "_current_tts_helper_persona", default=""

)

_current_user_message: ContextVar[str] = ContextVar(

    "_current_user_message", default=""

)





def set_current_persona_excerpt(persona: str):

    """orchestrator round2 入口调用,告知 delegate 守卫人设执行准则。"""

    rules = ""
    try:
        from app.memory.persona_files import persona_round2_instruct_by_content
        rules = persona_round2_instruct_by_content(persona or "")
    except Exception:
        rules = ""
    if not rules:
        rules = (persona or "").strip()
    return _current_persona_excerpt.set(rules.strip()[:1800])


def set_current_tts_helper_persona(persona: str):

    """Expose full persona text only to TTS helpers that may deliver final voice."""

    return _current_tts_helper_persona.set((persona or "").strip()[:8000])





def set_current_user_message(msg: str):

    """orchestrator round2 入口调用,告知 delegate 守卫用户原 message。"""

    return _current_user_message.set((msg or "")[:600])





def reset_current_persona_excerpt(token):

    if token is not None:

        _current_persona_excerpt.reset(token)


def reset_current_tts_helper_persona(token):

    if token is not None:

        _current_tts_helper_persona.reset(token)





def reset_current_user_message(token):

    if token is not None:

        _current_user_message.reset(token)


def _current_task_anchor_for_guard(user_message: str, tasks: list[dict]) -> str:
    """Build a compact active-goal anchor for delegate quality guard calls.

    The guard may run from nested helper contexts where the original user
    message is not available through the legacy ContextVar. The thread context
    and explicit helper envelopes still carry the user-authorized task, so pass
    that as model-visible context instead of letting the guard infer a detached
    helper chain.

    为守卫补齐当前任务锚点；嵌套 helper 缺少原始用户消息时，用线程契约和 helper 目标说明用户授权范围。
    """
    parts: list[str] = []
    msg = (user_message or "").strip()
    if msg:
        parts.append("Original or current user request:\n" + msg[:1200])
    try:
        from app.core.core_processes import get_current_thread_context
        ctx = get_current_thread_context()
    except Exception:
        ctx = None
    if ctx is not None:
        ctx_msg = str(getattr(ctx, "user_message", "") or "").strip()
        if ctx_msg and ctx_msg != msg:
            parts.append("Thread user request:\n" + ctx_msg[:1200])
        intent = str(getattr(ctx, "plan_intent", "") or "").strip()
        key_points = list(getattr(ctx, "plan_key_points", None) or [])
        deliverables = list(getattr(ctx, "plan_deliverables", None) or [])
        markers = dict(getattr(ctx, "plan_markers", None) or {})
        if intent or key_points or deliverables or markers:
            lines = ["Thread task contract:"]
            if intent:
                lines.append("intent=" + intent[:500])
            if key_points:
                lines.append("key_points=" + json.dumps(key_points[:10], ensure_ascii=False)[:900])
            if deliverables:
                lines.append("deliverables=" + json.dumps(deliverables[:10], ensure_ascii=False)[:500])
            if markers:
                lines.append("markers=" + json.dumps(markers, ensure_ascii=False, sort_keys=True)[:700])
            parts.append("\n".join(lines))
    try:
        from app.core import agent_state as _agent_state
        trace_id = debug.current_trace_id() or ""
        state = _agent_state.structured_status(trace_id) if trace_id else {}
    except Exception:
        state = {}
    if isinstance(state, dict):
        evidence_lines: list[str] = []
        for ev in list(state.get("verified_evidence_recent") or [])[-12:]:
            if not isinstance(ev, dict):
                continue
            source = str(ev.get("source") or "")
            kind = str(ev.get("kind") or "")
            # 2026-06-11 Round 15: include completed-helper evidence so a
            # follow-up delegation is judged against what prior helpers already
            # produced. Without it the guard blocked a patch helper for
            # "missing browser tools" AFTER a browser helper had completed the
            # Playwright evidence (20260611_162518_p16784, 2x guard_blocked,
            # recovery 0.2).
            if source == "helper" and kind:
                summary = str(ev.get("summary") or "").strip()
                data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
                line = f"- completed_helper[{kind}]: {summary[:220]}"
                if data.get("terminal_reason"):
                    line += f" (terminal={data.get('terminal_reason')}, ok={data.get('ok')})"
                evidence_lines.append(line)
                continue
            if source != "env_read" and kind not in {"project_file_read", "exact_text_reference"}:
                continue
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            ev_path = str(data.get("path") or "").strip()
            summary = str(ev.get("summary") or "").strip()
            facts = data.get("text_facts") if isinstance(data.get("text_facts"), dict) else {}
            fact_bits = []
            for key in ("total_lines", "line_count", "start_line", "end_line", "truncated"):
                if facts.get(key) not in (None, "", [], {}):
                    fact_bits.append(f"{key}={facts.get(key)}")
            if data.get("sha256"):
                fact_bits.append(f"sha256={str(data.get('sha256'))[:12]}...")
            line = f"- {kind or source}: {ev_path or summary[:160]}"
            if fact_bits:
                line += " (" + ", ".join(fact_bits[:6]) + ")"
            evidence_lines.append(line)
        if evidence_lines:
            parts.append(
                "Recent verified main-thread and completed-helper evidence:\n"
                + "\n".join(evidence_lines)
                + "\nThese are facts already observed by the main workflow. A completed_helper line means that helper already produced its evidence; a follow-up delegation consuming that evidence does not need the same tools again. They do not by themselves prove that downstream synthesis is complete."
            )
    task_goal_lines: list[str] = []
    for t in (tasks or [])[:5]:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("task_id") or "").strip()
        kind = str(t.get("kind") or "").strip()
        prompt = str(t.get("prompt") or "").strip().replace("\r\n", "\n")
        expected = t.get("expected_outputs") or []
        line = f"- task_id={tid or '?'} kind={kind or '?'}"
        if expected:
            line += " expected_outputs=" + json.dumps(expected[:8], ensure_ascii=False)[:400]
        if prompt:
            line += "\n  prompt=" + prompt[:500]
        task_goal_lines.append(line)
    if task_goal_lines:
        parts.append("Delegated helper goals:\n" + "\n".join(task_goal_lines))
    return "\n\n".join(parts).strip()[:3000] or "(No explicit task anchor was available; judge conservatively from helper tasks and persona.)"


def _task_quality_guard_environment_helper_text() -> tuple[str, str, str]:
    """Return environment-only helper guidance for the task-quality guard."""
    try:
        from app.core.runtime_mode import is_environment_mode
        enabled = is_environment_mode()
    except Exception:
        enabled = False
    if not enabled:
        return (
            "",
            "Helper-kind scope facts may reference only these base kinds; `final` is not a helper kind.\n",
            "- Broad project architecture mapping -> project_map; selected-file summaries -> file_summary; change-risk review -> impact_review.\n",
        )
    return (
        "- inventory: environment-only first-pass project inventory, file-type coverage, README/entry/config/test discovery, exact lightweight statistics, and unread source-material notes.\n",
        "Helper-kind scope facts may reference base kinds plus environment-only inventory for first-pass project inventory; `final` is not a helper kind.\n",
        "- First-pass unfamiliar project inventory -> inventory; deeper architecture mapping -> project_map; selected-file summaries -> file_summary; change-risk review -> impact_review.\n"
        "- Framework/contract/spec/outline tasks that must write `.txt`, `.md`, or `.json` files are artifact-producing. Code is the matching kind when the contract controls runnable project files, benchmark execution, generated datasets, APIs, schemas consumed by code, or implementation interfaces. Edit is the matching kind when the contract controls a long-form document, report structure, prose section plan, literature review structure, document acceptance checklist, or final-document assembly plan.\n"
        "- Technical subject matter alone does not make a task code. Markdown algorithm analysis, theoretical comparison tables, and proposed data-structure descriptions remain edit unless they must implement code, run experiments, or generate structured benchmark data.\n",
    )


def _guard_observation_from_payload(payload: dict) -> dict:
    """Convert guard or deterministic quality results into neutral facts."""
    if not isinstance(payload, dict):
        return {"kind": "guard_observation", "needs_attention": True, "raw": str(payload)[:500]}
    fact = {
        "kind": "guard_observation",
        "needs_attention": True,
        "issue": payload.get("issue") or payload.get("error") or payload.get("error_kind") or "guard_attention",
    }
    for key in (
        "task_id",
        "task_ids",
        "reason",
        "details",
        "signals",
        "prompt_len",
        "expected_outputs",
        "expected_outputs_count",
        "kind",
        "mode",
    ):
        value = payload.get(key)
        if value not in (None, "", [], {}):
            fact[key] = value
    return fact





async def _persona_consent_guard(

    persona: str, user_message: str, tasks: list[dict]

) -> tuple[bool, str, list[dict], list[dict]]:

    """LLM quality guard for whether this exact delegation may run as-is.

    The runtime only executes should_act plus the free-form reason. Symbolic
    checks are attached as guard_observations, and old structured guard fields
    are ignored for runtime decisions.

    守卫只输出是否允许当前派发及自由理由；符号化检测只作为事实输入。
    """

    if not tasks:

        return True, "", [], []

    # Compact task summary for the guard. Keep full task-specific facts in the
    # user message so the static guard system prompt stays cache-stable.

    _task_brief_lines = []

    for t in tasks[:5]:

        _tid = t.get("task_id", "?")

        _kind = t.get("kind", "")

        _mode = t.get("mode", "easy")

        # Short head-only excerpts can hide prompt-embedded evidence, output
        # paths, or acceptance facts from the guard. Show head and tail plus
        # the total length fact so final-assembly prompts are judged on shape.
        _prompt_full = t.get("prompt") or ""
        if len(_prompt_full) > 1800:
            _prompt = (
                _prompt_full[:1000]
                + "\n...[middle omitted for guard brief]...\n"
                + _prompt_full[-800:]
            )
        else:
            _prompt = _prompt_full[:1400]

        _eo = t.get("expected_outputs") or []
        try:
            from app.llm.tools.delegate_framework import normalize_string_list
            _input_files = normalize_string_list(
                t.get("input_files")
                or t.get("source_files")
                or t.get("transferred_files")
                or t.get("files"),
                max_items=20,
                max_chars_each=180,
            )
            _acceptance_checks = normalize_string_list(
                t.get("acceptance_checks") or t.get("checks"),
                max_items=16,
                max_chars_each=220,
            )
        except Exception:
            _input_files = [
                str(x)[:180]
                for x in (t.get("input_files") or [])
                if str(x).strip()
            ][:20]
            _acceptance_checks = [
                str(x)[:220]
                for x in (t.get("acceptance_checks") or [])
                if str(x).strip()
            ][:16]
 
        _task_brief_lines.append(

            f"- task_id={_tid} kind={_kind} mode={_mode} expected_outputs={_eo}\n"
            f"  input_files: {_input_files or []}\n"
            f"  acceptance_checks: {_acceptance_checks or []}\n"

            f"  prompt total {len(_prompt_full)} chars; first {len(_prompt)} shown: {_prompt}"

        )
        _dispatch_reason = str(t.get("dispatch_reason") or "").strip()
        if _dispatch_reason:
            _task_brief_lines.append(
                f"  main dispatch reason: {_dispatch_reason[:700]}"
            )
        _observations = t.get("guard_observations") or []
        if isinstance(_observations, list) and _observations:
            _task_brief_lines.append(
                "  attention facts: "
                + json.dumps(_observations[:8], ensure_ascii=False, sort_keys=True)[:1200]
            )
 
    _task_brief = "\n".join(_task_brief_lines)
 
    if len(tasks) > 5:
 
        _task_brief += f"\n... {len(tasks) - 5} additional task(s) omitted."


    _env_helper_kind_line, _helper_kind_scope_facts, _project_kind_principle = _task_quality_guard_environment_helper_text()


    # 拿当前 trace 的 split-block 计数, 写进 prompt 让 guard LLM 知道(避免一直拦)

    _trace_id_for_guard = debug.current_trace_id() or ""

    _existing_block_counts = {

        t.get("task_id", "?"): _guard_split_block_count.get(

            (_trace_id_for_guard, t.get("task_id", "?")), 0

        )

        for t in tasks

    }

    _existing_kind_block_counts = {

        t.get("task_id", "?"): _guard_kind_block_count.get(

            (_trace_id_for_guard, t.get("task_id", "?")), 0

        )

        for t in tasks

    }



    from app.llm import aux_prompts as _aux
    _guard_runtime_facts = _aux.build_task_quality_guard_runtime_facts(
        persona=(persona or "(无人设,只判断可拆分性和类型匹配)"),
        env_helper_kind_line=_env_helper_kind_line,
        helper_kind_scope_facts=_helper_kind_scope_facts,
        project_kind_principle=_project_kind_principle,
        existing_block_counts=_existing_block_counts,
        existing_kind_block_counts=_existing_kind_block_counts,
    )
    msgs = [

        {"role": "system", "content": _aux.build_task_quality_guard_system(
            persona="",
            env_helper_kind_line="",
            helper_kind_scope_facts="",
            project_kind_principle="",
            existing_block_counts={},
            existing_kind_block_counts={},
        )},
        {"role": "user", "content": _aux.TASK_QUALITY_GUARD_USER_TEMPLATE.format(
            task_anchor=_current_task_anchor_for_guard(user_message, tasks),
            user_message=(user_message or "(none)"),
            task_brief=_task_brief,
            runtime_facts=_guard_runtime_facts,
        )},

    ]

    try:

        from app.llm.client import chat_json

        result = await chat_json(
            msgs,
            lite=True,
            reasoning="disabled",
            metrics_tag="json.task_quality_guard",
        )

        should_act = bool(result.get("should_act", True))

        reason = str(result.get("reason", ""))[:500]
        return should_act, reason, [], []
    except Exception as e:

        # 守卫失败 → 放行(保守:不耽误正常请求)

        return True, f"guard_error: {e}", [], []


def _deterministic_kind_recommendations(tasks: list[dict]) -> list[dict]:
    """Return clear artifact-to-helper mismatch facts before any helper starts.

    This is a guard fact, not an auto-correction: it is attached to
    guard_observations so the guard LLM can decide whether to allow or block.

    明确产物类型和 helper 工具族不匹配时返回事实；不静默改写任务。

    2026-06-04 P133: 限制为**物理硬约束**——只在 kind 完全无法产出预期产物时
    暴露事实（read/draw/tts 想产 docx/pptx/xlsx 等可执行/二进制；code 想产 docx
    类 Office 主体）。模糊的"prose vs code"/"framework-contract"启发已删除，
    交给 guard LLM 与主线程基于上下文判断。
    """
    recommendations: list[dict] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        tid = str(task.get("task_id") or "").strip()
        kind = str(task.get("kind") or "").strip().lower()
        prompt = str(task.get("prompt") or "")
        expected_outputs = [
            str(x).strip()
            for x in (task.get("expected_outputs") or [])
            if str(x).strip()
        ]
        if not tid:
            continue

        # Skip deprecated kinds - they should be rejected upstream, not recommended
        if kind in {"general", "final"}:
            continue

        suggested = ""
        reason = ""
        has_non_text_impl = _has_non_text_implementation_output(expected_outputs)

        if (
            kind != "tts"
            and _looks_like_user_facing_tts_synthesis_task(prompt, expected_outputs)
            and not _looks_like_project_tts_implementation_task(prompt, expected_outputs)
        ):
            suggested = "tts"
            reason = (
                "User-facing/persona voice synthesis or audio-file generation must use the "
                "system-managed TTS route. Code helpers must not install or use external TTS "
                "engines such as gTTS, edge-tts, pyttsx3, SAPI, browser speech, espeak, or "
                "similar engines to produce the requested speech/voice output. Non-speech audio "
                "such as white noise, tones, beeps, music/signal synthesis, waveform processing, "
                "or audio analysis remains code/signal work."
            )
        # Hard physical constraint: edit helper owns Office/PDF document assembly.
        elif expected_outputs and _has_office_document_output(prompt, expected_outputs) and kind != "edit":
            suggested = "edit"
            reason = "Office/PDF deliverables require document assembly tools."
        # Hard physical constraint: source code, scripts, executables, generated data
        # need code-helper file/run tools. read/edit/draw/tts/verify cannot run a build.
        elif has_non_text_impl and kind in {"edit", "verify", "draw", "tts", "read", "ocr"}:
            suggested = "code"
            reason = (
                "Source code, scripts, generated data, benchmarks, and runnable project files "
                "need code-helper tools and checks."
            )

        if suggested and suggested != kind and suggested in VALID_HELPER_KINDS:
            rec = {
                "task_id": tid,
                "current_kind": kind,
                "observed_helper_kind_name": suggested,
                "reason": reason,
            }
            recommendations.append(rec)
    return recommendations


_AUDIO_OUTPUT_RE = re.compile(
    r"(?i)(?:\b(?:speech|spoken|narration|voice\s*file|voice\s*reply|tts)\b|"
    r"voice_reply_file|voice_reply\.|final\s+voice\s+reply|"
    r"语音文件|人声|朗读|配音|语音回复|生成语音|合成语音|输出语音)"
)
_EXTERNAL_TTS_ENGINE_RE = re.compile(
    r"(?i)(?:\bgtts\b|edge[-_ ]?tts|pyttsx3|sapi\.spvoice|spvoice|"
    r"speechsynthesizer|speechsynthesis|espeak|festival|pico2wave|"
    r"system\.speech\.synthesis|windows\s+sapi|browser\s+speech)"
)
_TTS_SYNTHESIS_VERB_RE = re.compile(
    r"(?i)(?:synthesi[sz]e|generate|create|produce|save|output|export|"
    r"spoken|narration|voice|audio|tts|朗读|配音|语音|音频|合成|生成|输出)"
)
_PROJECT_TTS_IMPLEMENTATION_RE = re.compile(
    r"(?i)(?:implement|debug|fix|repair|refactor|test|unit\s*test|pytest|"
    r"module|source|project|library|wrapper|bridge|api|pipeline|handler|"
    r"实现|调试|修复|重构|测试|源码|模块|项目|接口|封装|管线)"
)


def _looks_like_user_facing_tts_synthesis_task(prompt: str, expected_outputs: list[str]) -> bool:
    """Return true when the helper envelope asks to produce user-facing speech."""
    joined_outputs = " ".join(str(x or "") for x in expected_outputs)
    text = f"{prompt or ''}\n{joined_outputs}"
    if not text.strip():
        return False
    if _AUDIO_OUTPUT_RE.search(text):
        return bool(_EXTERNAL_TTS_ENGINE_RE.search(text) or _TTS_SYNTHESIS_VERB_RE.search(text))
    return False


def _looks_like_project_tts_implementation_task(prompt: str, expected_outputs: list[str]) -> bool:
    """Exempt project-code work about TTS systems from audio-output routing facts."""
    joined_outputs = " ".join(str(x or "") for x in expected_outputs)
    text = f"{prompt or ''}\n{joined_outputs}"
    low_outputs = joined_outputs.lower()
    if any(str(x or "").lower().endswith((".wav", ".mp3", ".m4a", ".ogg", ".flac")) for x in expected_outputs):
        return False
    if not _PROJECT_TTS_IMPLEMENTATION_RE.search(text):
        return False
    project_path_hint = bool(re.search(r"(?i)(?:^|[\\/])(?:app|src|tests?|lib|core)[\\/]|\.py\b|\.ts\b|\.js\b", low_outputs + "\n" + text))
    return project_path_hint or bool(re.search(r"(?i)(tts\s+(?:module|bridge|handler|pipeline|tool|api)|(?:module|bridge|handler|pipeline|tool|api)\s+tts)", text))


def _read_helper_project_visible_output_facts(
    *,
    task_id: str,
    prompt: str,
    expected_before: list[str],
    expected_after: list[str],
) -> list[dict]:
    """Return neutral guard facts for read helpers with final-artifact outputs.

    Read helpers can write internal evidence, but not project-visible or
    user-facing deliverables. The sanitizer still canonicalizes internal
    evidence names for compatibility; this function only exposes the mismatch
    before the filtered outputs disappear from the guard's view.

    read helper 只能写内部证据；项目可见或用户交付物输出在过滤前转成守卫事实。
    """
    kept_keys = {
        str(x or "").replace("\\", "/").strip().strip("`\"'").lstrip("./").lower().removeprefix("_env/").rsplit("/", 1)[-1]
        for x in (expected_after or [])
        if str(x or "").strip()
    }
    facts: list[dict] = []
    conflicts: list[str] = []
    for raw in expected_before or []:
        norm = str(raw or "").replace("\\", "/").strip().strip("`\"'").lstrip("./")
        if not norm:
            continue
        key = norm.lower().removeprefix("_env/").rsplit("/", 1)[-1]
        if key in kept_keys:
            continue
        low = norm.lower()
        is_project_visible = low == "_env" or low.startswith("_env/")
        is_user_facing = _looks_like_user_facing_text_artifact_output(norm)
        if is_project_visible and is_user_facing:
            conflicts.append(norm)
        elif is_user_facing and not low.endswith(".txt"):
            conflicts.append(norm)
    if conflicts:
        facts.append({
            "kind": "guard_observation",
            "issue": "read_helper_project_visible_output_conflict",
            "needs_attention": True,
            "task_id": task_id,
            "current_kind": "read",
            "expected_outputs": conflicts[:12],
            "details": (
                "A read helper can only write internal evidence files. These declared outputs look "
                "project-visible or user-facing and were not kept as internal read evidence. The "
                "guard should decide whether this exact delegation may run as read-first, or whether "
                "another helper or main-process project apply step should own the final artifact after "
                "evidence is collected."
            ),
        })
    return facts


def _looks_like_user_facing_text_artifact_output(path: str) -> bool:
    """Return true for text artifacts that are deliverables, not read evidence."""
    norm = str(path or "").replace("\\", "/").strip().strip("`\"'").lstrip("./")
    if not norm:
        return False
    low = norm.lower()
    if not low.endswith((".md", ".markdown", ".rst", ".txt", ".json", ".yaml", ".yml", ".csv")):
        return False
    if _is_internal_read_evidence_output(norm):
        return False
    base = low.rsplit("/", 1)[-1]
    deliverable_markers = (
        "analysis", "report", "paper", "chapter", "section", "outline",
        "summary", "comparison", "design", "contract", "framework",
        "论文", "报告", "分析", "章节", "设计", "比较", "对比", "框架", "契约",
    )
    return (
        low.startswith("_helpers_shared/")
        or low.startswith("_env/")
        or any(marker in low for marker in deliverable_markers)
        or base in {"readme.md", "report.md", "paper.md", "summary.md"}
    )


def _looks_like_prose_artifact_task(prompt: str, expected_outputs: list[str]) -> bool:
    """Detect prose/document artifact production without runnable-code ownership."""
    if not any(_looks_like_user_facing_text_artifact_output(x) for x in expected_outputs):
        return False
    prompt_text = str(prompt or "")
    text = (prompt_text + " " + " ".join(str(x) for x in expected_outputs)).lower()
    prose_markers = (
        "paper", "report", "chapter", "section", "markdown", "analysis",
        "comparison table", "theoretical", "literature", "prose", "document",
        "论文", "报告", "章节", "文档", "分析", "比较", "对比", "理论", "撰写", "成文",
    )
    if not any(marker in text or marker in prompt_text for marker in prose_markers):
        return False
    code_markers = (
        "implement source", "implement code", "compile", "run benchmark",
        "benchmark script", "benchmark data", "run test", "pytest", "makefile",
        "source code", "write script", "generate csv", "data computation",
        "实现源码", "实现代码", "编译", "运行测试", "运行基准", "基准脚本",
        "源码", "编写脚本", "生成 csv", "计算数据",
    )
    if any(marker in text or marker in prompt_text for marker in code_markers):
        return False
    return True


def _looks_like_code_owned_framework_contract_task(prompt: str, expected_outputs: list[str]) -> bool:
    """Detect framework contracts that should be owned by code helpers."""
    if not _looks_like_operational_framework_contract_task(prompt, expected_outputs):
        return False
    prompt_text = str(prompt or "")
    text = (prompt_text + " " + " ".join(str(x) for x in expected_outputs)).lower()
    prose_markers = (
        "paper", "report", "article", "chapter", "literature", "document acceptance",
        "final-document", "final document", "word validation", "docx paper",
        "论文", "报告", "文章", "章节", "正文", "文档验收", "最终文档",
    )
    code_markers = (
        "runnable project", "project scaffold", "source", "implementation interface",
        "api", "generated dataset", "benchmark execution", "benchmark script",
        "benchmark harness", "compile", "test command", "build",
        "可运行项目", "项目脚手架", "源码", "实现接口", "生成数据集",
        "执行基准", "基准脚本", "基准框架", "编译", "构建",
    )
    if any(marker in text or marker in prompt_text for marker in code_markers):
        return True
    if any(marker in text or marker in prompt_text for marker in prose_markers):
        return False
    return False


def _looks_like_operational_framework_contract_task(prompt: str, expected_outputs: list[str]) -> bool:
    """Detect shared contracts that define downstream helper/tool workflow."""
    prompt_text = str(prompt or "")
    text = (prompt_text + " " + " ".join(str(x) for x in expected_outputs)).lower()
    if not any(marker in text for marker in ("framework", "contract", "spec", "schema", "outline")) and not any(
        marker in prompt_text for marker in ("框架", "契约", "规范", "大纲", "命名", "验收")
    ):
        return False
    operational_markers = (
        "helper", "downstream", "fan-out", "fanout", "workflow", "acceptance",
        "validation", "naming", "file naming", "merge order", "ownership",
        "benchmark design", "benchmark protocol", "shared contract",
        "_helpers_shared", "expected_outputs",
        "后续", "分片", "验收", "命名", "文件命名", "合并顺序", "职责边界",
        "基准测试设计", "统一接口", "统一框架", "共享框架",
    )
    if not any(marker in text or marker in prompt_text for marker in operational_markers):
        return False
    return any(
        str(path or "").replace("\\", "/").strip().lower().lstrip("./").startswith("_helpers_shared/")
        for path in expected_outputs
    )


_SOURCE_READING_EXT_RE = re.compile(
    r"(?i)(?:^|[\s`'\"([{,;:，。；：、])"
    r"([^\s`'\"<>(){}\[\],;:，。；：、]+?\."
    r"(?:docx?|pdf|pptx?|xlsx?|xls|png|jpe?g|webp|bmp|gif|txt|md))"
)


def _looks_like_source_material_reading(prompt: str) -> bool:
    """Detect tasks whose real work is reading user/source materials.

    Script/library wording can appear in these prompts, but it is only a means
    of extraction. The helper plan should still start with read helpers.

    判断任务本质是否为读取/抽取材料；脚本措辞只是手段。
    """
    p = prompt or ""
    lower = p.lower()
    read_signals = (
        "read", "extract", "transcribe", "ocr", "source material", "source-material",
        "document content", "office", "pdf", "docx", "screenshot", "scan",
        "读取", "提取", "抽取", "识别", "转写", "材料", "正文", "报告内容",
    )
    source_signals = (
        ".docx", ".doc", ".pdf", ".pptx", ".ppt", ".xlsx", ".xls",
        ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", "office",
        "source material", "source materials", "materials", "all files",
        "directory", "current directory", "图片", "截图", "扫描", "文档", "报告",
        "材料", "所有文件", "全部文件", "当前目录",
    )
    return any(s in lower or s in p for s in read_signals) and any(
        s in lower or s in p for s in source_signals
    )


def _source_material_ref_count(prompt: str) -> int:
    """Approximate how many concrete source files or natural groups are present."""
    p = prompt or ""
    refs = {
        m.group(1).replace("\\", "/").strip()
        for m in _SOURCE_READING_EXT_RE.finditer(p)
        if m.group(1).strip()
    }
    group_markers = re.findall(
        (
            r"(?im)^\s*(?:\d+[.)、]|[-*])\s+"
            r"(?:"
            r"(?:batch|folder|group|目录)\s*[0-9一二三四五六七八九十百千万]*\b"
            r"|第?\s*[0-9一二三四五六七八九十百千万]+\s*组"
            r"|[^:\n]{1,80}(?:组|folder|batch|group|目录)"
            r")\s*[:：]?"
        ),
        p,
    )
    explicit_count = 0
    for m in re.finditer(r"(?i)(?:共|total|all)\s*(\d+)\s*(?:份|个|files?|documents?|docs?|材料|报告)", p):
        try:
            explicit_count = max(explicit_count, int(m.group(1)))
        except ValueError:
            pass
    return max(len(refs), len(group_markers), explicit_count)


def _source_material_count_hint(task: dict) -> int:
    try:
        value = int(task.get("_source_count_hint") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _prompt_has_verified_or_embedded_evidence_for_final_assembly(prompt: str) -> bool:
    """Detect prompts that already carry evidence for final artifact assembly.

    This only suppresses source-read split facts for the guard. It does not
    validate evidence quality and does not allow or block a helper.

    已带证据的最终组装提示不应再被当作原始材料读取分片事实。
    """
    text = str(prompt or "")
    if not text:
        return False
    lower = text.lower()
    evidence_markers = (
        "verified evidence",
        "already verified",
        "verified facts",
        "classification evidence",
        "evidence below",
        "use these facts",
        "confirmed facts",
        "helper evidence",
        "already read",
        "already extracted",
        "source materials already read",
        "source files already read",
        "已验证",
        "验证事实",
        "证据如下",
        "使用这些事实",
        "确认事实",
        "已读取",
        "已读完",
        "已提取",
    )
    output_markers = (
        "output file",
        "output files",
        "files to create",
        "file 1:",
        "write all",
        "create exactly",
        "produce exactly",
        "write the final",
        "create final",
        "final report",
        "draft",
        "markdown",
        "产出文件",
        "创建文件",
        "最终报告",
        "草稿",
    )
    return any(marker in lower or marker in text for marker in evidence_markers) and any(
        marker in lower or marker in text for marker in output_markers
    )


def _task_has_concrete_source_material_inputs(task: dict) -> bool:
    """Return true only when a task has real source material to read.

    Framework, outline, and paper-contract tasks may mention many sections,
    algorithms, or future files. That alone should not trigger read-helper
    fan-out; read fan-out is for existing source files, source ranges, material
    batches, or an explicit directory/material inventory request.

    read 分片只面向真实材料输入；框架/大纲/论文契约不因章节多而触发拆分。
    """
    prompt = str(task.get("prompt") or "")
    lower = prompt.lower()
    list_fields = ("input_files", "source_files", "transferred_files", "files")
    for field in list_fields:
        value = task.get(field)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (list, tuple, set)) and any(str(x).strip() for x in value):
            return True
    if _source_material_count_hint(task) >= 1:
        return True
    if _source_material_ref_count(prompt) >= 1:
        return True
    explicit_material_scope = (
        "current directory", "all files", "source materials", "source material",
        "material batch", "file batch", "office files", "pdf files", "image files",
        "read all", "extract from", "transcribe",
        "当前目录", "所有文件", "全部文件", "材料", "文档", "提取", "读取",
    )
    return any(marker in lower or marker in prompt for marker in explicit_material_scope)


def _split_recommendation_is_source_read(rec: dict) -> bool:
    split_names = rec.get("observed_split_boundary_names") or rec.get("split_into") or []
    text = (
        str(rec.get("reason") or "") + " " + " ".join(str(x) for x in split_names)
    ).lower()
    return any(marker in text for marker in ("source material", "source-material", "read_sources", "read helper", "read helpers"))


def _prompt_has_broad_or_heavy_source_scope(prompt: str) -> bool:
    text = str(prompt or "")
    lower = text.lower()
    broad_scope = (
        "current directory", "all files", "read all", "extract from", "transcribe",
        "source materials", "material batch", "file batch", "office files",
        "pdf files", "image files", "全部文件", "所有文件", "当前目录",
        "整批", "材料批", "文档批", "提取", "转写",
    )
    if any(marker in lower or marker in text for marker in broad_scope):
        return True
    return bool(re.search(r"(?i)\.(?:docx?|pdf|pptx?|xlsx?|xls|png|jpe?g|webp|bmp|gif)\b", text))


def _should_soften_source_read_split_for_single_text_output(task: dict, rec: dict) -> bool:
    """Return true when a source-read split should be advice, not a hard block.

    Hard split-blocks are useful for broad raw-material extraction. They are
    counterproductive when the main process already shaped a single prose/text
    deliverable from a small fact set: blocking the helper forces another round
    of main-process authoring or repeated delegate attempts. Keep the guard
    factual by letting the helper run in these small synthesis cases.

    少量事实合成单个文本产物时，拆分建议不应硬拦；大批原始材料读取仍由上游规则拦截。
    """
    if not isinstance(task, dict) or not _split_recommendation_is_source_read(rec):
        return False
    expected = [
        str(x or "").replace("\\", "/").strip().lower()
        for x in (task.get("expected_outputs") or [])
        if str(x or "").strip()
    ]
    if len(expected) != 1:
        return False
    text_exts = (".md", ".markdown", ".txt", ".rst", ".html", ".htm", ".json", ".yaml", ".yml")
    if not expected[0].endswith(text_exts):
        return False
    explicit_input_count = 0
    for field in ("input_files", "source_files", "transferred_files", "files"):
        value = task.get(field)
        if isinstance(value, str):
            items = [
                item.strip()
                for item in re.split(r"[\n,;]+", value)
                if item.strip()
            ]
            explicit_input_count += len(items) if items else 1 if value.strip() else 0
        elif isinstance(value, (list, tuple, set)):
            explicit_input_count += sum(1 for x in value if str(x or "").strip())
    prompt = str(task.get("prompt") or "")
    source_count = max(
        explicit_input_count,
        _source_material_ref_count(prompt),
        _source_material_count_hint(task),
    )
    if source_count >= 6:
        return source_count <= 8 and not _prompt_has_broad_or_heavy_source_scope(prompt)
    return True


_COMPACT_TEXT_MATERIAL_EXTS = (
    ".txt", ".md", ".markdown", ".rst", ".json", ".yaml", ".yml",
    ".csv", ".tsv", ".xml", ".html", ".htm", ".ini", ".toml",
)
_HEAVY_SOURCE_MATERIAL_EXTS = (
    ".doc", ".docx", ".pdf", ".ppt", ".pptx", ".xls", ".xlsx",
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".zip",
    ".tar", ".gz", ".7z", ".rar", ".mp3", ".wav", ".mp4", ".mov",
)
_FINAL_TEXT_ARTIFACT_EXTS = (
    ".txt", ".md", ".markdown", ".rst", ".json", ".yaml", ".yml",
    ".csv", ".html", ".htm",
)


def _normalise_delegate_path(value: object) -> str:
    return str(value or "").replace("\\", "/").strip().strip("`\"'").lstrip("./")


def _split_delegate_list_field(value) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, str):
        raw_items = re.split(r"[\n,;]+", value)
    elif isinstance(value, dict):
        raw_items = [str(k) for k in value.keys()]
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        norm = _normalise_delegate_path(item)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def _delegate_path_suffix(path: str) -> str:
    name = _normalise_delegate_path(path).lower()
    if "." not in name.rsplit("/", 1)[-1]:
        return ""
    return "." + name.rsplit(".", 1)[-1]


def _looks_like_compact_text_input(path: str) -> bool:
    suffix = _delegate_path_suffix(path)
    if not suffix:
        return False
    return suffix in _COMPACT_TEXT_MATERIAL_EXTS


def _looks_like_heavy_or_visual_input(path: str) -> bool:
    suffix = _delegate_path_suffix(path)
    return bool(suffix and suffix in _HEAVY_SOURCE_MATERIAL_EXTS)


def _looks_like_final_text_artifact(path: str) -> bool:
    suffix = _delegate_path_suffix(path)
    return bool(suffix and suffix in _FINAL_TEXT_ARTIFACT_EXTS)


def _task_explicit_input_files(task: dict) -> list[str]:
    if not isinstance(task, dict):
        return []
    for field in ("input_files", "source_files", "transferred_files", "files"):
        values = _split_delegate_list_field(task.get(field))
        if values:
            return values
    return []


def _task_expected_outputs(task: dict) -> list[str]:
    if not isinstance(task, dict):
        return []
    return _split_delegate_list_field(task.get("expected_outputs"))


def _looks_like_compact_text_material_bundle_task(task: dict) -> bool:
    """Detect bounded text-material synthesis tasks for guard attention only.

    This does not allow or block anything. It exposes a task-shape fact to the
    LLM guard when the main process appears to split a small text bundle into
    read slices instead of giving one owner helper the bounded inputs.

    紧凑文本材料包只作为守卫事实，不由程序决定是否拆分。
    """
    if not isinstance(task, dict):
        return False
    inputs = _task_explicit_input_files(task)
    if not (2 <= len(inputs) <= 20):
        return False
    if any(_looks_like_heavy_or_visual_input(path) for path in inputs):
        return False
    compact_count = sum(1 for path in inputs if _looks_like_compact_text_input(path))
    if compact_count < max(2, len(inputs) - 2):
        return False
    outputs = _task_expected_outputs(task)
    if outputs and not all(_looks_like_final_text_artifact(path) for path in outputs):
        return False
    prompt = str(task.get("prompt") or "")
    if _prompt_has_broad_or_heavy_source_scope(prompt):
        return False
    return True


def _deterministic_compact_text_bundle_split_observations(tasks: list[dict]) -> list[dict]:
    """Return neutral facts for compact text bundles split into read slices.

    A compact group of ordinary text/config/data files often converges faster
    when one owner helper reads the bounded inputs and writes the final artifact
    set. Read-slice fan-out remains useful for broad, heavy, visual, Office,
    uncertain, or reusable-evidence work; this function only reports the batch
    shape to the guard.

    小型文本材料包拆成 read 批次时仅提示守卫注意，由守卫判断是否放行。
    """
    if not tasks:
        return []
    read_bundle_tasks: list[dict] = []
    final_owner_present = False
    all_inputs: list[str] = []
    all_outputs: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        kind = str(task.get("kind") or "").strip().lower()
        inputs = _task_explicit_input_files(task)
        outputs = _task_expected_outputs(task)
        if kind in {"code", "edit"} and _looks_like_compact_text_material_bundle_task(task):
            final_owner_present = True
        if kind == "read" and inputs:
            pseudo = dict(task)
            pseudo["expected_outputs"] = outputs or ["read_evidence.txt"]
            if _looks_like_compact_text_material_bundle_task(pseudo):
                read_bundle_tasks.append(task)
                all_inputs.extend(inputs)
                all_outputs.extend(outputs)
    if len(read_bundle_tasks) < 2 or final_owner_present:
        return []
    unique_inputs = []
    seen_inputs: set[str] = set()
    for path in all_inputs:
        norm = _normalise_delegate_path(path)
        if norm and norm not in seen_inputs:
            seen_inputs.add(norm)
            unique_inputs.append(norm)
    if not (2 <= len(unique_inputs) <= 24):
        return []
    if any(_looks_like_heavy_or_visual_input(path) for path in unique_inputs):
        return []
    compact_count = sum(1 for path in unique_inputs if _looks_like_compact_text_input(path))
    if compact_count < max(2, len(unique_inputs) - 2):
        return []
    task_ids = [str(task.get("task_id") or "").strip() for task in read_bundle_tasks if str(task.get("task_id") or "").strip()]
    return [{
        "task_id": task_ids[0] if task_ids else "",
        "issue": "compact_text_material_bundle_split",
        "observed_split_boundary_names": task_ids[:12],
        "expected_outputs": all_outputs[:20],
        "details": (
            f"The current helper batch splits {len(unique_inputs)} bounded text-like input files across "
            f"{len(read_bundle_tasks)} read helpers, and no code/edit owner helper for the final artifact "
            "set is present in this batch. This is a task-shape fact for guard judgment: one owner helper "
            "can often read compact text inputs and produce cohesive text artifacts, while split read "
            "helpers remain appropriate for broad, large, visual, Office/OCR, uncertain, or reusable "
            "evidence extraction."
        ),
    }]


def _deterministic_compact_text_owner_observations(tasks: list[dict]) -> list[dict]:
    """Return neutral facts when one helper owns a compact text artifact set.

    The guard receives this as positive task-shape context. Runtime still uses
    only the guard LLM verdict.

    给守卫提供小型文本 owner 形状事实；不由程序判定放行。
    """
    observations: list[dict] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        tid = str(task.get("task_id") or "").strip()
        kind = str(task.get("kind") or "").strip().lower()
        if not tid or kind not in {"read", "edit", "code"}:
            continue
        inputs = _task_explicit_input_files(task)
        outputs = _task_expected_outputs(task)
        prompt = str(task.get("prompt") or "")
        compact_inputs = bool(inputs) and _looks_like_compact_text_material_bundle_task(task)
        embedded_final_assembly = (
            kind in {"edit", "code"}
            and _prompt_has_verified_or_embedded_evidence_for_final_assembly(prompt)
        )
        if not compact_inputs and not embedded_final_assembly:
            continue
        observations.append({
            "task_id": tid,
            "issue": "compact_text_owner_shape",
            "current_kind": kind,
            "expected_outputs": outputs[:20],
            "input_file_count": len(inputs),
            "details": (
                "This helper appears shaped as one owner for a compact text-material or embedded-evidence "
                "artifact set. This is a task-shape fact for guard judgment: one owner helper may read "
                "bounded text inputs or use verified embedded evidence to produce cohesive final text "
                "artifacts when fresh raw-source extraction is not required."
            ),
        })
    return observations[:8]


def _delegate_output_key(value: object) -> str:
    text = _normalise_delegate_path(value).lower()
    if text.startswith("_env/"):
        text = text[5:]
    if text.startswith("_helpers_shared/"):
        text = text[len("_helpers_shared/"):]
    return text


def _deterministic_same_batch_output_overlap_observations(tasks: list[dict]) -> list[dict]:
    """Return facts when helper tasks in one batch own overlapping outputs."""
    owners: dict[str, list[str]] = {}
    raw_by_key: dict[str, list[str]] = {}
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        tid = str(task.get("task_id") or "").strip()
        if not tid:
            continue
        for output in _task_expected_outputs(task):
            key = _delegate_output_key(output)
            if not key:
                continue
            owners.setdefault(key, []).append(tid)
            raw_by_key.setdefault(key, []).append(output)
    overlaps = {
        key: tids
        for key, tids in owners.items()
        if len(set(tids)) >= 2
    }
    if not overlaps:
        return []
    output_names = sorted(overlaps)[:12]
    first_tid = sorted(set(tid for tids in overlaps.values() for tid in tids))[0]
    return [{
        "task_id": first_tid,
        "issue": "same_batch_expected_output_overlap",
        "expected_outputs": output_names,
        "details": (
            "Multiple helpers in the current delegation declare overlapping expected_outputs. "
            "This may be an intentional hard/easy backup race, a replacement attempt, or duplicate "
            "same-goal work. The guard should decide from dispatch_reason, mode, and task boundary "
            "whether the exact batch should run as-is."
        ),
        "signals": [
            {"output": key, "task_ids": sorted(set(tids))[:8], "raw_paths": raw_by_key.get(key, [])[:8]}
            for key, tids in list(overlaps.items())[:8]
        ],
    }]


def _deterministic_recent_output_overlap_observations(
    tasks: list[dict],
    *,
    recent_records: list[dict] | None = None,
) -> list[dict]:
    """Return facts when recent helper records overlap current expected outputs."""
    if not tasks or not recent_records:
        return []
    observations: list[dict] = []
    recent_by_output: dict[str, list[dict]] = {}
    for record in recent_records or []:
        if not isinstance(record, dict):
            continue
        task_id = str(record.get("task_id") or "").strip()
        delivered = _split_delegate_list_field(record.get("delivered_files") or record.get("expected_outputs"))
        for path in delivered:
            key = _delegate_output_key(path)
            if not key:
                continue
            recent_by_output.setdefault(key, []).append({
                "task_id": task_id,
                "ok": record.get("ok"),
                "outputs_complete": record.get("outputs_complete"),
                "terminal_reason": record.get("terminal_reason"),
                "path": path,
            })
    if not recent_by_output:
        return []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if bool(task.get("resume")):
            continue
        tid = str(task.get("task_id") or "").strip()
        outputs = _task_expected_outputs(task)
        if not tid or not outputs:
            continue
        overlaps: list[dict] = []
        for output in outputs:
            key = _delegate_output_key(output)
            for record in recent_by_output.get(key, []):
                if record.get("task_id") == tid:
                    continue
                overlaps.append({"output": key, "current_path": output, **record})
        if overlaps:
            observations.append({
                "task_id": tid,
                "issue": "recent_helper_expected_output_overlap",
                "expected_outputs": sorted({item["output"] for item in overlaps})[:12],
                "details": (
                    "Recent helper ledger records include delivered or declared outputs overlapping this "
                    "new helper's expected_outputs. This is a fact for guard judgment: the current task may "
                    "be a retry, replacement, resume-like continuation, backup race, or duplicate same-goal work."
                ),
                "signals": overlaps[:12],
            })
    return observations


def _deterministic_ready_artifact_overlap_observations(
    tasks: list[dict],
    *,
    ready_artifacts: list[dict] | None = None,
) -> list[dict]:
    """Return facts when ready main-thread artifacts overlap helper outputs."""
    if not tasks or not ready_artifacts:
        return []
    ready_by_output: dict[str, list[dict]] = {}
    for artifact in ready_artifacts or []:
        if not isinstance(artifact, dict):
            continue
        path = str(artifact.get("path") or "").strip()
        key = _delegate_output_key(path)
        if not key:
            continue
        ready_by_output.setdefault(key, []).append({
            "path": path,
            "created_by": artifact.get("created_by"),
            "verified_by": artifact.get("verified_by"),
            "type": artifact.get("type"),
            "status": artifact.get("status"),
        })
    if not ready_by_output:
        return []
    observations: list[dict] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if bool(task.get("resume")):
            continue
        tid = str(task.get("task_id") or "").strip()
        outputs = _task_expected_outputs(task)
        if not tid or not outputs:
            continue
        overlaps: list[dict] = []
        for output in outputs:
            key = _delegate_output_key(output)
            for artifact in ready_by_output.get(key, []):
                overlaps.append({"output": key, "current_path": output, **artifact})
        if overlaps:
            observations.append({
                "task_id": tid,
                "issue": "ready_artifact_expected_output_overlap",
                "expected_outputs": sorted({item["output"] for item in overlaps})[:12],
                "details": (
                    "Agent state already records ready artifacts overlapping this new helper's "
                    "expected_outputs. This is a fact for guard judgment: the helper may be useful "
                    "for revision or verification, or it may duplicate an already completed artifact."
                ),
                "signals": overlaps[:12],
            })
    return observations


# 2026-06-04 P131: dispatch-time guard for read helpers receiving helper-produced inputs.
# Filename patterns that mark a path as helper-produced artifact (not user source material).
import re as _re_p131
_HELPER_PRODUCED_NAME_PATTERNS = [
    _re_p131.compile(r"(?:^|/)framework_contract", _re_p131.IGNORECASE),
    _re_p131.compile(r"_analysis\.(md|markdown|txt)$", _re_p131.IGNORECASE),
    _re_p131.compile(r"_evidence\.(txt|md)$", _re_p131.IGNORECASE),
    _re_p131.compile(r"_long_report\.(md|markdown|txt)$", _re_p131.IGNORECASE),
    _re_p131.compile(r"_inventory\.(md|markdown|txt)$", _re_p131.IGNORECASE),
    _re_p131.compile(r"_summary\.(md|markdown|txt)$", _re_p131.IGNORECASE),
    _re_p131.compile(r"_outline\.(md|markdown|txt)$", _re_p131.IGNORECASE),
]
# Exceptions: filenames matching the patterns above but that are actually system-staged
# project orientation files for read helpers (resource manifests, project trees, etc.).
# These are written by the workspace-stager, not by a sibling helper, and read helpers
# are explicitly told to consult them.
_HELPER_PRODUCED_BASENAME_EXCEPTIONS = {
    "project_inventory.md",
    "project_inventory.txt",
    ".resource_manifest.json",
    "resource_manifest.md",
}


def _deterministic_source_read_split_recommendations(tasks: list[dict]) -> list[dict]:
    """Recommend read-helper fan-out for broad source-material extraction.

    This is a guard signal rather than task rewriting. It prevents one code or
    hard helper from serially reading many raw documents before the main thread
    has parallel evidence files to coordinate.

    大批材料读取先并行 read 分片；后续 code/edit 消费证据。

    2026-06-04 P132: when the task explicitly lists `input_files`, the main
    process has already enumerated its inputs and decided how to handle them;
    we do not split-recommend in that case. The P131 read-helper-targeting-
    helper-produced guard plus the prompt guidance is enough to prevent the
    inverted pattern (read-helper-on-sibling-helper-output). This keeps the
    deterministic guard focused on the original failure mode (a code helper
    asked to extract from a directory full of raw documents) while letting
    LLM + dispatch guards handle the rest.
    """
    recommendations: list[dict] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        tid = str(task.get("task_id") or "").strip()
        kind = str(task.get("kind") or "").strip().lower()
        if not tid or kind not in {"code", "edit"}:
            continue
        # P132 exemption: explicit input_files means the main process already
        # enumerated inputs. Trust LLM + P131 guard for the routing decision.
        explicit_inputs = task.get("input_files")
        if isinstance(explicit_inputs, (list, tuple, set)) and any(
            str(x or "").strip() for x in explicit_inputs
        ):
            continue
        prompt = str(task.get("prompt") or "")
        if _prompt_has_verified_or_embedded_evidence_for_final_assembly(prompt):
            continue
        if not _task_has_concrete_source_material_inputs(task):
            continue
        if not _looks_like_source_material_reading(prompt):
            continue
        source_count = max(_source_material_ref_count(prompt), _source_material_count_hint(task))
        if source_count < 6:
            continue
        batch_count = min(8, max(2, (source_count + 5) // 6))
        recommendations.append({
            "task_id": tid,
            "observed_split_boundary_names": [f"read_sources_batch_{i}" for i in range(1, batch_count + 1)],
            "reason": (
                f"Detected {source_count} source material items/groups in this code/edit helper request. "
                "Candidate source-reading boundaries are available as read_sources_batch_*; downstream code/edit "
                "synthesis can consume compact evidence if the guard and main thread choose that workflow."
            ),
        })
    return recommendations





# 2026-05-12 P19: split-block 计数(死循环防御 — 双维度)

# 单 task_id 计数:同一 task_id 被拦 ≥ 2 次后必放行

# 全 trace 计数:整个 trace 累计 split-block ≥ 4 次后, 后续全放行

#               (防止主线程换 task_id 名字无限绕过)

_guard_split_block_count: dict[tuple[str, str], int] = {}

_guard_split_block_trace_total: dict[str, int] = {}

_GUARD_SPLIT_TRACE_HARD_CAP = 4  # 全 trace 上限



# 2026-05-12 P21: kind-block 计数(同样的死循环防御)

_guard_kind_block_count: dict[tuple[str, str], int] = {}

_guard_kind_block_trace_total: dict[str, int] = {}

_GUARD_KIND_TRACE_HARD_CAP = 4



# 2026-05-21: framework-block 计数(同类对比缺统一框架的死循环防御)

# 主线程被要求先建框架后,重派时守卫应看到框架已存在而放行;但万一守卫误判,

# trace 级 cap 兜底:同 trace block ≥2 次后必放行,避免卡死。

_guard_framework_block_trace_total: dict[str, int] = {}

_GUARD_FRAMEWORK_TRACE_HARD_CAP = 2


def _framework_block_counter_key(trace_id: str, task_ids: list[str] | None = None) -> str:
    """Scope framework-block loop prevention to one comparable helper batch."""
    base = str(trace_id or "unknown").strip() or "unknown"
    tids = sorted(str(t or "").strip() for t in (task_ids or []) if str(t or "").strip())
    if not tids:
        return base
    return base + "::" + ",".join(tids[:20])





_MAX_DELEGATE_TASKS_PER_CALL = settings.max_delegate_tasks_per_call

_MAX_HELPERS_PER_AGENT = settings.max_helpers_per_agent

_MAX_HELPERS = settings.max_helpers_concurrent

_MAX_AUTO_RESOURCE_RECOVERY_ROUNDS = 1

# helper 递归深度限制:

#   主线程 (depth=0) 可以 delegate 创建多个 helper(depth=1)

#   helper(depth=1) 可以 spawn_helper 创建兄弟 helper(depth=2),但深度=2 的 helper

#   不能再 spawn(防止指数爆炸)。

# 这个限制 + per-agent/server 活跃数,共同保证资源不失控。

# 2026-05-23: 单次 delegate 16、单 agent 32、服务端 64 分开控制。

_MAX_HELPER_DEPTH = settings.max_helper_depth



# ─── L1-1 (2026-05-09): 异步 spawn 的 result 缓存 ──────────────

# helper 在后台完成后,把 result dict 暂存这里,等主线程 collect/wait_any 来取。

# 拿走后从 cache 移除(避免内存累积)。

# 同 task_id 多次完成(比如同 task_id 多次 spawn_async)以最新结果覆盖。

#

# Key: (trace_id, task_id) — 防跨 trace 污染(多用户场景)

# Value: {"result": dict, "completed_at": float, "helper_task": asyncio.Task}

from app.llm.tools.delegate_state import (  # noqa: E402,F401

    _pending_results,

    _pending_results_lock,

    _completion_events,

    _PENDING_RESULT_TTL,

    _LEDGER_PER_TRACE_LIMIT,

    _completion_ledger,

    _completion_ledger_lock,

    _add_to_completion_ledger,

    _get_completion_ledger,

    _record_task_contracts,

    _get_task_contract,

    _count_task_id_attempts,

    _store_pending_result,

    _consume_pending_result,

    _ledger_result_for_collect,

    _recover_completed_result_for_collect,

    _peek_pending_result,

    _ensure_completion_event,

)



_bench_dims_registry: dict[str, dict[str, dict]] = {}  # trace_id -> task_id -> {"dims": set, "prefix": str, "ext": str, "ts": float}













































# 工作区复制超大警告阈值——大于这个值的 fork 会在 debug.log 报警(不是阻断,

# 让 LLM 知道这次 spawn 会比较慢且占空间)。Windows 上用 robocopy /MT 多线程,

# 100MB 一般 1-2s 完成,300MB 5-10s,>500MB 拒绝(显然不是设计意图)。

_FORK_WORKSPACE_WARN_BYTES = 100 * 1024 * 1024    # 100 MB → 警告

_FORK_WORKSPACE_REJECT_BYTES = 500 * 1024 * 1024  # 500 MB → 拒绝

# 2026-05-02 重构: helper 完全无时间/迭代硬墙

#   旧设计经历过 max_iter=7 → max_iter=None+timeout=900s → timeout=300s →

#   timeout=1800s 几次松绑,但 asyncio.wait_for 强制 cancel 始终是"硬杀",违反

#   "由主进程根据情况管理,不应硬杀"原则。**最终方案**:撤掉所有时间硬墙,helper:

#     - helper 自然停手 → 出报告退出

#     - helper 自检 stuck(StuckDetector)→ 主动 abort_event.set() → forced finalize 出报告退出

#     - 主进程 processes.kill / delegate.kill_helper(协作 only)→ 同上

#     - LLM 死循环兜底(client.py HARD_ITER_CAP=9999)→ 实际触达概率为 0

#   保留 _HELPER_LONG_RUN_OBSERVE 仅用于"helper 跑了很久"的观测埋点(日志告警),不强制中断。

_HELPER_LONG_RUN_OBSERVE = settings.helper_long_run_observe_sec   # 默认 30 分钟,仅用于 log.warning 提醒主进程关注,不会强制中断 helper



# 2026-05-09 Patch 33: helper 跑超过此阈值,observer 自动 set local_abort

# 让它走 forced finalize 出部分报告。

# 病因(trace 779bbcf0):helper repair_woat 一个就跑了 38 分钟 199 iter,

# 反复测试 woat.c 段错误(Missing: 10000/10000)却没意识到实现根本错,主线程

# 也没强制收尾 → 整个 trace 58 分钟用户体感完全卡死。

# 阈值设 2700s(45 min):比 LONG_RUN_OBSERVE 多 15 min 缓冲(留给 helper 自然完成);

# 触发后:set local_abort → helper next iter 顶部检测到 → forced finalize → 主线程拿到部分报告。

# 这是"硬护栏",不是"积极调度":只在异常长时(典型 helper 应该 < 10 min)兜底。

_HELPER_HARD_KILL_THRESHOLD = settings.helper_hard_kill_sec  # 默认 45 分钟,observer 强制 abort,helper 走 forced finalize



# 2026-05-10 Patch 55:废除 P45-P53 的 verify 自动派机制

#

# 用户精准批评(让我反思整个设计):

#   "程序需要验证是正常的,只是不同任务需要验证的强度不同,或许可以强制要求主进程

#    委派验证任务而非自动委派。长度限制不合理,简单任务也可能很长(纯字符输出),

#    复杂任务也可能很短(复杂算法),交由 codehelper(作为编写者本身应该能更准确得

#    预估)或者主进程决定复杂度。"

#

# 旧设计(P45 自动派 + P53 长度阈值)的根本问题:

#   - 关键词命中误判("正确性"在简单 task 也常见)

#   - 长度阈值粗糙(简单任务可能长 / 复杂任务可能短)

#   - 系统替主进程做决策违反"主进程保留决策权"原则

#

# 新设计:

#   - 完全去掉自动派 verify(_should_auto_verify / _auto_spawn_verify 都删)

#   - **主进程显式委派**:看任务复杂度自己决定 spawn verify helper

#   - codehelper 在自己的报告里**建议**是否需要 verify(作为编写者最清楚)

#   - 主进程综合 codehelper 建议 + 自己判断,显式 spawn 或不 spawn verify

#

# 保留:P47/P48 的 verify_verdict / repair_recommendation 解析

#   主进程显式派 verify 时,这套字段仍然有用



# task_id 用作 helper 工作区目录名 `_delegate_{user_tag}_{task_id}`，需做 sanitize：

# - 防 path traversal（'../escape' 会跳出主工作区）

# - 兼容 Windows / POSIX 文件名规则（去空格、去保留字符）

#

# 2026-05-01 改造: helper 工作区目录名加 user 前缀,避免 per-user 并行下

# 同名 task_id 撞目录(用户A 的 _delegate_avl_tree/ 和 用户B 的 _delegate_avl_tree/

# 会互相覆盖)。同 user 续作仍能命中(user 前缀+task_id 一致,resume=True 能找到原目录)。

import hashlib as _hashlib
import re as _re

_TASK_ID_RE = _re.compile(r"[^A-Za-z0-9_\-]")





# 2026-05-12 P17: 从 prompt 自动推断 expected_outputs

# 病因(实测 23:46 trace): 主线程 23 个 helper 全无 expected_outputs →

# Tier 1.C 验收链路完全失效(P11.L5/P12/P14.G/P14.K 全部依赖此字段)。

# 修法: 扫 prompt 提取"产出动词 + 文件名" 模式, 作为系统默认值。



# helper 文本工具(语言提示/验证摘录/相似度/预期产出推断)已抽离到 helper_text.py

# (2026-05-20 重构);re-export 兼容,调用点零改动。

from app.llm.tools.helper_text import (  # noqa: E402,F401

    _PRODUCT_FILE_EXTS,

    _PRODUCE_VERBS_CN,

    _PRODUCE_VERBS_EN,

    _TEMPLATE_NAMES,

    _helper_lang_hint,

    _extract_verify_fail_excerpt,

    _prompt_similarity,

    _infer_expected_outputs_from_prompt,

)









def _sanitize_task_id(raw: str, fallback_idx: int) -> str:

    """把 LLM 给的 task_id 转成安全的目录名片段。"""

    raw_text = str(raw or "").strip()
    s = _TASK_ID_RE.sub("_", raw_text)

    s = s.strip("_")[:32]  # 长度兜底

    if s:
        if raw_text and s != raw_text and len(s) < 8:
            digest = _hashlib.sha1(raw_text.encode("utf-8", errors="ignore")).hexdigest()[:8]
            return f"{s}_{digest}"[:32]
        return s
    if raw_text:
        digest = _hashlib.sha1(raw_text.encode("utf-8", errors="ignore")).hexdigest()[:10]
        return f"task{fallback_idx}_{digest}"[:32]
    return f"task{fallback_idx}"





def _user_workspace_tag(user_id: str) -> str:

    """把 user_id 缩成短目录名片段,用于 helper 工作区前缀。



    纯数字平台 ID 通常较短,直接取最后 10 位即可,完整保留也无副作用。

    其他形式的 user_id 走 sanitize+截断兜底。

    """

    s = _TASK_ID_RE.sub("_", (user_id or "anon").strip())

    s = s.strip("_")[:12]

    return s or "anon"









def _post_clean_helper_artifacts(ws_dir: str) -> int:

    """B8 修复辅助:robocopy 完成后扫一遍目录,删除多层 task_id 前缀的污染文件。



    robocopy 不能根据文件名规则过滤,所以 fork 完后用本函数补刀。

    只删非源码,且必须命中 _looks_like_helper_artifact 启发式。

    返回删除的文件数。

    """

    if not ws_dir or not os.path.isdir(ws_dir):

        return 0

    from app.llm.tools.workspace import _looks_like_helper_artifact

    n = 0

    SOURCE_EXTS = {

        ".py", ".c", ".cpp", ".cc", ".h", ".hpp", ".js", ".ts",

        ".sh", ".bat", ".cmd", ".ps1", ".md", ".txt", ".json", ".yaml", ".yml",

    }

    try:

        for name in os.listdir(ws_dir):

            full = os.path.join(ws_dir, name)

            if not os.path.isfile(full):

                continue

            ext = os.path.splitext(name)[1].lower()

            if ext in SOURCE_EXTS:

                continue

            if _looks_like_helper_artifact(name):

                try:

                    os.remove(full)

                    n += 1

                except OSError:

                    pass

    except OSError:

        pass

    if n > 0:

        debug.log(

            "delegate.fork.post_clean",

            f"removed {n} multi-prefix helper artifacts from helper workspace",

        )

    return n





def _clean_main_workspace_before_spawn(ws_dir: str) -> int:

    """2026-05-05: spawn 前清理主工作区的旧会话残留文件。



    旧会话产生的文件(历史 helper artifact、编译中间产物、非当前任务的文档)

    累积在主区间，每次 fork 都带进新 helper 工作区，污染上下文。

    此函数在 batch spawn 前扫一遍主区，删掉无害的残留。



    安全设计：

    - 只删根目录文件，不动 _shared/ 和 _helpers_shared/

    - 只删明确模式匹配的残留(多前缀 artifact + 编译产物)

    - 2026-05-08: 使用 _session_manifest.json 区分新旧——只删 session 前就存在的文件，

      当前会话新产生的文件(含 helper_* 前缀产物)绝不删除

    - 2026-05-12 P23: 修复 manifest 路径 bug —

      旧版读 ws_dir/.temp/_session_manifest.json (多一层 .temp/),

      但 _write_session_manifest 写的是 temp_ws/_session_manifest.json,

      而 ws_dir 本身就是 temp_ws (.temp/ 目录). 路径不一致导致 manifest 永远读不到,

      _files_before 永远空, 所有 helper 产物被当 stale 删除(累计 46+ 个文件)。

      修: 直接读 ws_dir/_session_manifest.json.

    Returns: 删除的文件数。

    """

    if not ws_dir or not os.path.isdir(ws_dir):

        return 0

    from app.llm.tools.workspace import _looks_like_helper_artifact



    # ── 2026-05-08: 加载 manifest，区分当前会话产物 vs 旧残留 ──

    # 2026-05-12 P23: 修复路径 — ws_dir 本身就是 temp_ws (.temp/), 不需要再加 .temp/

    _files_before: set[str] = set()

    _manifest_path = os.path.join(ws_dir, "_session_manifest.json")

    try:

        if os.path.isfile(_manifest_path):

            import json as _json

            _manifest = _json.loads(open(_manifest_path, "r", encoding="utf-8").read())

            _files_before = set(_manifest.get("files_before") or [])

    except (OSError, ValueError, KeyError):

        pass



    n = 0

    # 确认安全的残留扩展名(非源码、非任务关键数据)

    STALE_EXTS = {".o", ".obj", ".exe", ".pyc", ".pyo"}



    # 2026-05-12 P23: 加载当前会话已 spawn 的 helper task_id 作为 allowed_prefixes

    # 病因: _looks_like_helper_artifact 接受 allowed_prefixes 参数 (P57 修过),

    # 但本函数旧版没传 → 当前会话产物(如 chart_group1_chart1.png)被误判为 artifact 删掉。

    # 实测教训: 09:14 trace 累计删了 46 个文件,manifest 同时为空(已被 P23 第一处修复)。

    _allowed_prefixes: set[str] = set()

    try:

        from app.llm.tools.workspace import load_displayed_name_remap

        _remap = load_displayed_name_remap(ws_dir)

        # remap 的 key 是 task_id, 加进 allowed_prefixes

        _allowed_prefixes = set(_remap.keys()) if _remap else set()

    except Exception:

        pass



    try:

        for name in os.listdir(ws_dir):

            full = os.path.join(ws_dir, name)

            # 只处理根目录文件,不动子目录

            if not os.path.isfile(full):

                continue

            # ── 2026-05-08: 当前会话新产生的文件绝不删除 ──

            if _files_before and name not in _files_before:

                continue

            ext = os.path.splitext(name)[1].lower()

            remove = False

            if ext in STALE_EXTS:

                remove = True

            elif _looks_like_helper_artifact(name, allowed_prefixes=_allowed_prefixes):

                remove = True

            if remove:

                try:

                    os.remove(full)

                    n += 1

                except OSError:

                    pass

    except OSError:

        pass

    if n > 0:

        debug.log(

            "delegate.main_ws_clean",

            f"removed {n} stale files from main workspace before spawn "

            f"(manifest had {len(_files_before)} pre-existing files, "

            f"allowed_prefixes={len(_allowed_prefixes)})",

        )

    return n













async def _fast_copy_workspace(src: str, dst: str) -> int:

    """高效复制工作区。Windows 上优先用 robocopy(多线程),失败 fallback 到 shutil。



    Returns: 复制的文件数(估计值)



    设计:

      - Windows: robocopy /MT:8 多线程,跳过 _delegate_* 子目录(避免 fork 嵌套复制),

                 跳过 . 开头的隐藏文件(.helper_summary.txt 等内部状态)

                 用 /MAX:1073741824 (1GB) 仅挡极端大文件

      - 其他:  shutil.copytree 单线程,带 _should_copy_for_fork 过滤(同样 1GB)



    设计哲学(Phase 5++ v3): 工作区爆炸根因是清理失败,不是文件大小。

    真正修复是在 handle_delegate 立即清理 helper 工作区。

    这里 1GB cap 仅挡真异常大文件(单文件 >1GB 几乎肯定是 bug)。



    robocopy exit code 0-7 都是成功(8+ 才是真错),独立设计,不能直接当 returncode 用。

    """

    from app.llm.tools.workspace import (

        _FORK_COPY_HARD_LIMIT, _should_copy_for_fork, _should_copy_to_helper,

    )

    os.makedirs(dst, exist_ok=True)



    if _sys.platform == "win32":

        cmd = [

            "robocopy", src, dst,

            "/MT:8", "/E", "/R:1", "/W:1",

            "/MAX:" + str(_FORK_COPY_HARD_LIMIT),  # 1GB 单文件上限(只挡极端 outlier)

            "/NJH", "/NJS", "/NDL", "/NFL", "/NP",

            "/XD", "_delegate_*",

            "/XF", ".*",

        ]

        try:

            proc = await asyncio.create_subprocess_exec(

                *cmd,

                stdout=asyncio.subprocess.PIPE,

                stderr=asyncio.subprocess.PIPE,

            )

            try:

                _stdout, _stderr = await asyncio.wait_for(

                    proc.communicate(), timeout=120.0

                )

            except asyncio.TimeoutError:

                proc.kill()

                await proc.wait()

                raise RuntimeError("robocopy timed out after 120s")

            if proc.returncode is not None and proc.returncode < 8:

                # B8 修复:robocopy 不识别 helper artifact 启发式,事后清理一遍。

                # 把目标目录里看起来像多层 task_id 前缀的非源码文件删掉。

                _post_clean_helper_artifacts(dst)

                count = sum(len(files) for _, _, files in os.walk(dst))

                return count

            else:

                log.warning(

                    "robocopy returned %d (>=8 means failure), falling back to shutil",

                    proc.returncode,

                )

        except (FileNotFoundError, RuntimeError) as e:

            log.warning("robocopy unavailable (%s), falling back to shutil", e)



    # Fallback: shutil.copytree (single-threaded, 慢但可靠)

    # ignore 函数同时排除 _delegate_*/. 隐藏文件 + helper artifact + >1GB 文件

    def _ignore(src_path, names):

        skip = []

        for n in names:

            if n.startswith("_delegate_") or n.startswith("."):

                skip.append(n)

                continue

            full = os.path.join(src_path, n)

            if os.path.isdir(full) and n == "_env":
                continue

            if os.path.isfile(full):

                try:

                    if not _should_copy_to_helper(full, os.path.getsize(full)):

                        skip.append(n)

                except OSError:

                    pass  # 失败时保守复制

        return skip



    try:

        shutil.copytree(src, dst, dirs_exist_ok=True, ignore=_ignore)

    except Exception as e:

        log.exception("shutil fallback copy also failed: %s", e)

        raise



    count = sum(len(files) for _, _, files in os.walk(dst))

    return count





# L6-2 (2026-05-09): Resume incremental sync helpers











def _resume_incremental_sync(

    helper_workspace: str, main_workspace: str,

    *, skip_patterns: list[str] | None = None,

) -> dict:

    """resume 时只同步 main → helper 的增量改动。



    Returns stats dict with: copied, skipped_unchanged, helper_kept.

    """

    stats = {"copied": 0, "skipped_unchanged": 0, "helper_kept": 0}

    skip = set(skip_patterns or [])



    # 1. 扫主区,对每个文件比 helper 沙箱内的 (mtime, size)

    for main_rel_path in _list_workspace_files(main_workspace):

        if any(_match_path_pattern(main_rel_path, p) for p in skip):

            continue



        main_full = os.path.join(main_workspace, main_rel_path)

        helper_full = os.path.join(helper_workspace, main_rel_path)



        try:

            main_st = os.stat(main_full)

        except OSError:

            continue



        if os.path.exists(helper_full):

            try:

                helper_st = os.stat(helper_full)

            except OSError:

                helper_st = None



            if helper_st and (

                helper_st.st_size == main_st.st_size

                and abs(helper_st.st_mtime - main_st.st_mtime) < 1.0

            ):

                stats["skipped_unchanged"] += 1

                continue  # 完全一样,跳过



        # 主区改过 / helper 沙箱缺该文件 → 复制

        os.makedirs(os.path.dirname(helper_full) or helper_workspace, exist_ok=True)

        try:

            shutil.copy2(main_full, helper_full)

            stats["copied"] += 1

        except OSError:

            pass



    # 2. helper 沙箱内独有(主区没有)的文件保留(可能是 helper 自己产物)

    for helper_rel_path in _list_workspace_files(helper_workspace):

        if "_helpers_shared" in helper_rel_path:

            continue

        if any(_match_path_pattern(helper_rel_path, p) for p in skip):

            continue

        if not os.path.exists(os.path.join(main_workspace, helper_rel_path)):

            stats["helper_kept"] += 1



    return stats





# ContextVar：由 orchestrator _round2 设置，控制 helper 是否使用 lite 模型

# medium 路径设为 True（lite helpers），hard/veryhard 设为 False（pro helpers）

_helper_lite_var: ContextVar[bool] = ContextVar("delegate_helper_lite", default=False)





# ── Helper tools: all Round2 tools except delegate/spawn (no recursive LLM spawn) ──

# helper 工作区是 _setup_helper_workspace 复制出的隔离副本,改动不影响主工作区。

# 所以 4 个局部读写工具(read/edit/insert/search)对 helper 全开——

# helper 常需要"试验某个补丁后测试",edit_file 是核心需求。

from app.llm.tools.registry import (

    PYTHON_TOOL_SCHEMA, WORKSPACE_TOOL_SCHEMA,

    BASH_TOOL_SCHEMA,                               # 2026-05-02 part21

    COMMIT_TO_MAIN_SCHEMA,                          # 2026-05-03 Bug E

    RECALL_THREAD_SCHEMA,                           # 2026-05-03 防上下文淹没

    PROGRESS_NOTE_SCHEMA,                           # 2026-05-03 helper 心跳

    REQUEST_RESOURCE_SCHEMA,                         # helper 缺资源时统一冻结入口

    TODO_WRITE_SCHEMA, TODO_READ_SCHEMA,            # 2026-05-02 part21

    INSPECT_FILE_SCHEMA, READ_FILE_SCHEMA, EDIT_FILE_SCHEMA,

    MULTI_EDIT_SCHEMA,                              # 2026-05-02 part22 微调

    INSERT_IN_FILE_SCHEMA, SEARCH_IN_FILE_SCHEMA,

    CODE_INDEX_SCHEMA, READ_FUNCTION_SCHEMA,        # part14/16

    SEARCH_ACROSS_FILES_SCHEMA,                     # part16

    EXPAND_WARM_SCHEMA, EXPAND_COLD_SCHEMA, EXPAND_KB_SCHEMA,

    SEARCH_FILES_SCHEMA, MARK_AVOID_SCHEMA, FETCH_GROUP_FILE_SCHEMA,

    FETCH_TO_TEMP_SCHEMA,                            # v2: 三层隔离文件访问

    OFFICE_TOOL_SCHEMA,

    OCR_TOOL_SCHEMA, TTS_TOOL_SCHEMA,

    ASK_USER_QUESTION_SCHEMA,                        # 2026-05-04 Claude Code 移植

)

from app.llm.tools.tool_processes import PROCESSES_TOOL_SCHEMA



# Forward declarations — schemas defined later in this file.  They are kept for

# compatibility, but are not exposed to helpers; the main process owns all helper

# spawning/resume decisions.

SPAWN_HELPER_TOOL_SCHEMA: dict   # 在文件下方定义

WAIT_HELPER_TOOL_SCHEMA: dict    # 在文件下方定义



_HELPER_TOOLS = [

    PYTHON_TOOL_SCHEMA,

    WORKSPACE_TOOL_SCHEMA,

    BASH_TOOL_SCHEMA,            # 2026-05-02 part21:Claude Code 风格 shell

    # ── v2 架构:helper 不能 commit_to_main,只能产出到 temp,由主线程合并 ──

    FETCH_TO_TEMP_SCHEMA,        # v2:从永久区/历史快照按需复制文件到 temp

    RECALL_THREAD_SCHEMA,        # 2026-05-03:回顾原始任务+todos(防淹没)

    PROGRESS_NOTE_SCHEMA,        # 2026-05-03:写心跳给主线程看

    REQUEST_RESOURCE_SCHEMA,      # 缺主线程资源时冻结并请求资源 helper

    TODO_WRITE_SCHEMA,           # 2026-05-02 part21:任务规划外化

    TODO_READ_SCHEMA,            # 2026-05-02 part21

    INSPECT_FILE_SCHEMA,

    READ_FILE_SCHEMA,

    EDIT_FILE_SCHEMA,

    MULTI_EDIT_SCHEMA,           # 2026-05-02 part22 微调:之前漏了,helper 也该有

    INSERT_IN_FILE_SCHEMA,

    SEARCH_IN_FILE_SCHEMA,

    CODE_INDEX_SCHEMA,           # 2026-05-02 part14

    READ_FUNCTION_SCHEMA,        # 2026-05-02 part16

    SEARCH_ACROSS_FILES_SCHEMA,  # 2026-05-02 part16

    EXPAND_WARM_SCHEMA,

    EXPAND_COLD_SCHEMA,

    EXPAND_KB_SCHEMA,

    SEARCH_FILES_SCHEMA,

    MARK_AVOID_SCHEMA,

    FETCH_GROUP_FILE_SCHEMA,

    OCR_TOOL_SCHEMA,

    TTS_TOOL_SCHEMA,

    OFFICE_TOOL_SCHEMA,

    PROCESSES_TOOL_SCHEMA,           # NEW: 让 helper 能 list/kill 自己的进程

    ASK_USER_QUESTION_SCHEMA,        # 2026-05-04 Claude Code 移植:向主线程提问

    # 2026-05-11 P1.1: Skills 系统轻量版 — 详细指引按需加载,瘦身 system prompt

    {

        "type": "function",

        "function": {

            "name": "read_skill",

            "description": (

                "Load a detailed helper skill into the current helper context. Skills are prewritten best-practice notes for "
                "common helper situations such as missing files, compile diagnostics, workspace protocol, shared files, and "
                "cross-helper handoff. The system prompt stays compact; call read_skill only when deeper guidance would change "
                "the next action. Available skill names are listed in the system prompt or by list_skills.\n"

                "\n"

                "Typical use: read `find-missing-file` after file-not-found evidence, `compile-errors` after compiler/linker "
                "diagnostics, `workspace-deep-dive` for `_helpers_shared/` collaboration, or `shared-files-warning` for uploaded "
                "or shared files. When the task path is already clear, proceed with tools and keep the skill for later evidence-driven "
                "uncertainty.\n"

                "\n"

                "Returns the skill markdown. Unknown skill names return an error plus the available list.\n"

                "按需加载 helper 技术指引；只有缺文件、编译诊断、共享工作区或上传文件等需要更深规则时再调用。"

            ),

            "parameters": {

                "type": "object",

                "properties": {

                    "name": {

                        "type": "string",

                        "description": "Exact skill name, such as `find-missing-file` or `compile-errors`.\n精确 skill 名称。",

                    },

                },

                "required": ["name"],

            },

        },

    },

]









# 2026-05-03 重写(Bug 1 修):原版无条件告诉 helper "Windows 上 bash 工具有

# git-bash/MSYS2 环境,unix 命令可用",但实际工具入口只走 cmd /c → 模型按

# prompt 写 unix 命令必失败 → stuck detector 杀(实测 trace 7e6629f228c84e78

# sbt helper 32s 就被杀)。修后:启动时检测 git-bash → 有就如实告知 unix

# 可用;Windows + 无 git-bash 时如实告知只有 cmd 可用。helper 见到的 prompt

# 与工具实际行为一致,不再被假承诺误导。

# 2026-05-05: 硬件信息检测,解决 helper 在论文中编造 CPU/RAM 的 bug。

# 用 PowerShell (Windows) 或 /proc/cpuinfo+free (Linux) 获取真实配置。

# 检测一次后缓存,失败时降级为 platform 模块的最小信息。





# 平台/硬件提示 + bash 示例块已抽离到 helper_prompts.py(2026-05-20 重构);re-export 兼容。

from app.llm.tools.helper_prompts import (  # noqa: E402,F401

    _get_hardware_info,

    _build_platform_hint,

    _build_bash_examples_block,

    _PLATFORM_HINT,

    _ASAN_HINT,

)





from app.llm.tools.skills import (  # noqa: E402,F401
    BUNDLED_SKILLS,
    get_skill,
    list_skills,
    _SKILL_DESCRIPTIONS,
    _build_skills_listing,
)

from app.llm.tools.delegate_stuck import (  # noqa: E402,F401
    StuckDetector,
    _estimate_msgs_tokens,
    _stall_threshold_for,
)


# Helper model-visible prompts are centralized in helper_prompt_catalog.py.
from app.llm.tools.helper_prompt_catalog import (  # noqa: E402,F401
    _BASH_EXAMPLES_BLOCK,
    _HELPER_CONSISTENCY_CONTRACT,
    _helper_tool_availability_note,
    _SHARED_WORKSPACE_CORE,
    _SHARED_WORKSPACE,
    _SHARED_TECH,
    _SHARED_TOOL_SELECTION,
    _SHARED_DEBUG,
    _SHARED_TIMEOUT,
    _SHARED_C_BUGS,
    _SHARED_INTERFACE_CONSISTENCY,
    _SHARED_COMPILE_ERRS,
    _SHARED_HONESTY,
    _SHARED_REPORT_CODE,
    _select_helper_system,
    _HARD_MODE_SUFFIX,
    _HELPER_RESUME_HINT,
    _HELPER_SYSTEM_CODE,
    _HELPER_SYSTEM_EDIT,
    _PROJECT_ANALYSIS_BASE,
    _HELPER_SYSTEM_PROJECT_MAP,
    _HELPER_SYSTEM_FILE_SUMMARY,
    _HELPER_SYSTEM_IMPACT_REVIEW,
    _HELPER_SYSTEM_INVENTORY,
    _HELPER_SYSTEM_VERIFY,
    _HELPER_SYSTEM_DRAW,
    _HELPER_SYSTEM_TTS,
    _HELPER_SYSTEM_READ,
    _HELPER_SYSTEM_OCR,
    _HELPER_SYSTEM,
)


async def _run_managed_helper_once(

    *,

    task_id: str,

    prompt: str,

    main_workspace: str,

    helper_workspace: str,

    archive_id: str,

    group_id: str,

    user_id: str,

    resume: bool,

    kind: str,

    mode: str,

    user_lang: str,

    helper_think: bool,

    input_files: list[str] | None,

    expected_outputs: list[str] | None,

    acceptance_checks: list[str] | None = None,

    batch_sibling_outputs: set[str] | None,

    owner: str,

    description: str,

) -> tuple[dict, asyncio.Task]:

    """Run one helper through the normal registry path and return its result."""

    per_helper_abort = asyncio.Event()

    register_done = asyncio.Event()

    task = asyncio.create_task(_run_one_helper(

        task_id=task_id,

        prompt=prompt,

        main_workspace=main_workspace,

        helper_workspace=helper_workspace,

        archive_id=archive_id,

        group_id=group_id,

        user_id=user_id,

        resume=resume,

        local_abort=per_helper_abort,

        wait_for_register=register_done,

        user_lang=user_lang,

        kind=kind,

        mode=mode,

        helper_think=helper_think,

        input_files=input_files or [],

        expected_outputs=expected_outputs or [],

        acceptance_checks=acceptance_checks or [],

        batch_sibling_outputs=batch_sibling_outputs,

    ))

    await _register_helper_with_autoclean(

        owner=owner,

        task=task,

        helper_task_id=task_id,

        helper_workspace=helper_workspace,

        abort_event=per_helper_abort,

        description=description,

        helper_kind=kind,

        archive_id=archive_id,

        group_id=group_id,

        user_id=user_id,

    )

    register_done.set()

    try:

        result = await task

    except BaseException as e:

        result = {

            "task_id": task_id,

            "ok": False,

            "report": f"helper crashed: {type(e).__name__}: {e}",

            "terminal_reason": "crashed",

            "crash_type": type(e).__name__,

        }

    return result, task





async def _auto_recover_resource_required(

    *,

    results: list[dict],

    cleaned: list[dict],

    main_workspace: str,

    archive_id: str,

    group_id: str,

    user_id: str,

    main_owner: str,

    user_lang_now: str,

    helper_think: bool,

    helper_task_ids: dict[asyncio.Task, str],

    trace_id: str,

    max_rounds: int = _MAX_AUTO_RESOURCE_RECOVERY_ROUNDS,

) -> tuple[list[dict], list[dict]]:

    """Return resource-required results unchanged.



    Resource helpers must be spawned/activated by the main process, not by

    delegate internals.  `request_resource` only freezes the blocked helper and

    reports a structured request plus wake conditions to Round2/main.

    """

    return list(results), []





from app.llm.tools.delegate_runner import _run_one_helper  # noqa: E402,F401







from app.llm.tools.delegate_copyback import (  # noqa: E402,F401

    _SOURCE_EXTENSIONS,

    _RESULT_COPY_BACK_MAX_SIZE,

    _RESULT_COPY_BACK_MAX_FILES,

    _copy_results_to_main,

)







async def _register_helper_with_autoclean(

    *, owner: str, task: asyncio.Task, helper_task_id: str,

    helper_workspace: str, abort_event: asyncio.Event,

    description: str, parent_helper_task_id: str | None = None,

    helper_kind: str = "", archive_id: str = "", group_id: str = "", user_id: str = "",

) -> str:

    """注册 helper 到 ProcessRegistry,并安装 done_callback 在 task 完成时

    自动注销 — 防泄漏。



    所有 helper(初始 delegate / fork_from / spawn_helper)都应走这个 wrapper,

    保证 LLM 看到的活跃 helper 数与真实运行情况一致(老的 dead helper 不残留)。

    """

    proc_id = await proc_registry().register_helper(

        owner=owner,

        task=task,

        helper_task_id=helper_task_id,

        helper_workspace=helper_workspace,

        abort_event=abort_event,

        description=description,

        parent_helper_task_id=parent_helper_task_id,

        helper_kind=helper_kind,

        archive_id=archive_id,

        group_id=group_id,

        user_id=user_id,

    )

    try:

        from app.core.environment_events import publish_workflow_event



        publish_workflow_event({

            "kind": "helper_start",
            "status": "running",
            "trace_id": debug.current_trace_id() or "",

            "proc_id": proc_id,

            "task_id": helper_task_id,

            "helper_kind": helper_kind,
            "title": "helper started",
            "description": description[:600],

            "archive_id": archive_id,

            "group_id": group_id,

            "user_id": user_id,

        })

    except Exception:

        pass



    def _on_done(t: asyncio.Task):

        # task 完成回调:同步触发的,这里用 create_task 调异步 unregister。

        # 即使 task 被 cancel/exception,这个 callback 都会触发(asyncio 保证)。

        try:

            from app.core.bg_tasks import schedule



            async def _finalize_helper_lifecycle():

                h = await proc_registry().unregister(proc_id)

                try:

                    from app.core.environment_events import publish_workflow_event



                    publish_workflow_event({

                        "kind": "helper_registry_done",
                        "status": "exited",
                        "trace_id": debug.current_trace_id() or "",

                        "proc_id": proc_id,

                        "task_id": helper_task_id,

                        "helper_kind": helper_kind or (getattr(h, "helper_kind", "") if h else ""),
                        "title": "helper process exited",
                        "description": description[:600],
                        "truth_scope": (
                            "This event only means the helper process left the active registry. "
                            "Use the delegate result for success, blocker, resource request, or failure state."
                        ),

                        "archive_id": archive_id,

                        "group_id": group_id,

                        "user_id": user_id,

                    })

                except Exception:

                    pass

                try:

                    if helper_workspace and os.path.isdir(helper_workspace):

                        enforce_workspace_capacity(

                            helper_workspace,

                            label=f"helper_done:{helper_task_id}",

                        )

                except Exception:

                    log.exception("helper %s workspace capacity cleanup failed", helper_task_id)



            schedule(_finalize_helper_lifecycle(),

                     name=f"proc_unreg.{proc_id}")

        except RuntimeError:

            # event loop 已关闭,无所谓 — 进程都要退了

            pass



    task.add_done_callback(_on_done)

    return proc_id





# ─── Lite 进度摘要 ─────────────────────────────────────────

async def _generate_progress_summaries(

    active_helpers: list[dict],

    *,

    timeout_sec: float = 8.0,

) -> dict[str, str]:

    """对一批 active helper 运行一次 lite 模型生成中文进度摘要。



    2026-05-02 part8 改:从原来的"每 15s 一次的后台 loop"改成"主模型按需触发"。

    主模型在 processes(action="list", with_summary=true) 时调用这个函数,等同步 LLM

    调用结束(超时上限 timeout_sec)再返回结果挂在 list 响应里。



    设计取舍:

      - 旧版后台 loop:7 helper 跑 5min = 20 次 lite 调用 = ~20s 算力,且大部分主模型

        从未读到这个字段(主模型决策只看 iter/recent_tools/last_thought 已经够)

      - 新版按需:主模型自己决定何时要看摘要(比如怀疑 helper 卡住时),触发一次同步

        lite 调用,8s timeout 兜底。零浪费、阻塞短(8s 是 LLM 的 hard cap,实测 1-2s 返回)

      - 模型不主动调时永远不跑 = 算力 0



    Args:

        active_helpers: 来自 processes.list 的 helper 字典列表(已过滤掉 no_heartbeat_yet)

        timeout_sec: LLM 调用硬上限,超时返回空 dict(不让主模型等 30s)



    Returns:

        {task_id: summary_text}  失败/超时返回 {}

    """

    from app.llm.model_pool import resolve_task

    from app.llm.client import _client_for_spec, _retry, _log_prompt_cache_shape, _record_response_usage

    from app.core import debug as _dbg

    if not active_helpers:

        return {}



    messages = [
        {"role": "system", "content": DELEGATE_PROGRESS_SUMMARY_SYSTEM},
        {"role": "user", "content": _delegate_progress_summary_user_payload(active_helpers)},
    ]



    _ps_spec = resolve_task("progress_message")

    _ps_cli = _client_for_spec(_ps_spec)
    _log_prompt_cache_shape(
        label="delegate.progress_summary",
        model=_ps_spec.model,
        messages=messages,
    )

    try:

        resp = await asyncio.wait_for(

            _retry(

                lambda: _ps_cli.chat.completions.create(

                    model=_ps_spec.model,

                    messages=messages,

                    stream=False,

                    max_tokens=300,

                    extra_body={"thinking": {"type": "disabled"}},

                    response_format={"type": "json_object"},

                ),

                label="delegate.progress_summary",

                provider=_ps_spec.provider,

            ),

            timeout=timeout_sec,

        )

        _record_response_usage(resp, model=_ps_spec.model, tag="delegate.progress_summary")

        content = resp.choices[0].message.content or ""

    except asyncio.TimeoutError:

        _dbg.warn(f"progress_summary on-demand: LLM timed out (>{timeout_sec}s)")

        return {}

    except Exception as e:

        _dbg.warn(f"progress_summary on-demand: LLM call failed: {type(e).__name__}: {e}")

        return {}



    try:

        data = json.loads(content)

        summaries = data.get("summaries") or {}

        if not isinstance(summaries, dict):

            return {}

    except (json.JSONDecodeError, AttributeError):

        return {}



    # 只保留字符串值

    result: dict[str, str] = {}

    for tid in task_ids:

        s = summaries.get(tid)

        if isinstance(s, str) and s.strip():

            result[tid] = s.strip()[:120]

    return result





# ── 兼容性保留 ──

# 老接口 _progress_summarizer_loop 已弃用(2026-05-02 part8)。

# 旧调用者(handle_delegate 那条 asyncio.create_task)已改成不调用。

# 留个 shim 防止外部如果还有引用导致 ImportError;调用直接返回不做事。

async def _progress_summarizer_loop(owner: str, interval: float = 15.0):

    """**已弃用**(2026-05-02 part8)。改用 _generate_progress_summaries 按需触发。



    立即返回 — 不再做后台轮询,避免每 15s 一次的浪费 LLM 调用。

    旧调用方应改为:模型显式 processes(action="list", with_summary=true)。

    """

    return





# ═══════════════════════════════════════════════════════════════

# L1-1 (2026-05-09): Async delegate action handlers

# ═══════════════════════════════════════════════════════════════





# 2026-05-10 Patch 61 v2: wait_stub 反模式检测(spawn 和 spawn_async 共用)

#

# 病因(trace f973df3770544567):主线程派 helper {"prompt": "等待之前的 gen_charts

# 和 woat_impl 完成。不做任何事,直接输出 done。", "task_id": "wait_stub", ...}。

# 浪费 fork + LLM API + ProcessRegistry 槽位。

# 主线程应该用 delegate(action='wait_any') 或 delegate(action='collect') 等已派的 helper。

#

# 检测启发式:

#   - NOOP 关键词("什么都不做")单独命中即拦截 — 这本身就是 stub 信号

#   - 或 WAIT + DONE 同时命中(明确"等待+输出 done"模式)



_WAIT_STUB_KEYWORDS_WAIT = (

    "等待", "等其他", "等之前", "等所有", "等全部", "等 helper",

    "等同伴", "等同步", "wait for", "wait until",

)

_WAIT_STUB_KEYWORDS_NOOP = (

    "不做任何", "什么都不做", "什么也不做", "什么也别做", "不做任何事",

    "do nothing", "no-op", "noop",

)

_WAIT_STUB_KEYWORDS_DONE = (

    "直接输出 done", "直接 done", "直接返回 done",

    "output done", "return done", "just done",

)



_WAIT_STUB_HINT = (

    "**Wait action guidance**: you appear to want an existing helper to finish. Use wait or collect instead of spawning a sleep/wait placeholder helper.\n"

    "Use one of these actions instead:\n"

    "  - **Block until at least one helper finishes**: `delegate(action='wait_any', "

    "task_ids=['<existing task_id>', ...], wait_window_sec=N)`\n"

    "  - **Collect completed results without blocking**: `delegate(action='collect', "

    "task_ids=['<existing task_id>', ...])`\n"

    "Spawn helpers only for new work. A no-op helper wastes process and model capacity.\n"
    "等待已有 helper 时使用 wait_any 或 collect，不要新派空任务。"

)





def _check_wait_stub_anti_pattern(tasks: list) -> str | None:

    """检测 tasks 中是否有 wait_stub 反模式。命中返回 hint JSON,否则 None。"""

    if not isinstance(tasks, list):

        return None

    for _t in tasks:

        if not isinstance(_t, dict):

            continue

        _p_text = (_t.get("prompt") or "").lower()

        if not _p_text:

            continue

        _has_wait = any(k in _p_text for k in _WAIT_STUB_KEYWORDS_WAIT)

        _has_noop = any(k in _p_text for k in _WAIT_STUB_KEYWORDS_NOOP)

        _has_done = any(k in _p_text for k in _WAIT_STUB_KEYWORDS_DONE)

        # NOOP 单独命中即拦截,或 WAIT + DONE 同时命中

        if _has_noop or (_has_wait and _has_done):

            debug.log(

                "delegate.spawn.wait_stub_blocked",

                f"P61: 拦截 wait_stub 反模式 task_id={_t.get('task_id')!r} "

                f"signals=wait:{_has_wait}/noop:{_has_noop}/done:{_has_done}",

            )

            return json.dumps({

                "ok": False,

                "error": "wait_stub anti-pattern detected",

                "hint": _WAIT_STUB_HINT,

                "blocked_task_id": _t.get("task_id"),

            }, ensure_ascii=False)

    return None





def _detect_missing_unified_framework(cleaned: list[dict]) -> dict | None:

    """确定性检测:多个同类算法/实验做横向对比但无统一框架 task。



    不依赖守卫 LLM 判定,也不依赖提示词是否被正确部署 —— 纯代码规则,作为框架先行的兜底。

    病因(实测 trace c6e42ed6 / 32f96bf6 重做):用户因数据造假要求重做,主线程仍派

    6 算法各 1 helper 各自跑 benchmark,无 infra helper 建统一测量框架 → 数据口径不一/失真。



    触发(全部满足,保守低误伤):

      - ≥3 个 code helper 是横向比较 peer:各自 expected_outputs 含 results_*.csv,
        或 task_id/prompt 显示为不同算法/策略且用户目标是 compare/benchmark;

      - 没有任何 task 是 infra/framework 角色(task_id 或 prompt 提示统一框架/共享 .h/harness);

      - 都不是 resume(重做而非续作);

      - 各 task 没有引用共享框架文件(.h / bench_common / 统一 schema)。

    返回 warning dict(供主线程参考)或 None。不 kill,只警告 —— 硬拦截误伤风险高,留给守卫。

    """

    try:

        # 收集横向比较 peer code helper(排除 hard 竞速副本)

        import re as _re

        _peer_producers = []

        _has_infra = False

        _refs_shared = False

        for c in cleaned:

            _tid = str(c.get("task_id", "")).strip()

            _kind = str(c.get("kind", "")).strip().lower()

            _mode = str(c.get("mode", "")).strip().lower()

            _prompt = str(c.get("prompt", "") or "")
            _framework = str(c.get("framework", "") or "")

            _outs = c.get("expected_outputs") or []

            # hard 竞速副本不计(同任务副本,非独立对比单元)

            if _mode == "hard" and _tid.endswith("_hard"):

                continue

            if c.get("resume"):

                return None  # 有 resume = 续作,不是从零重做对比, 不触发

            # infra/framework 角色识别

            _tid_l = _tid.lower()

            if any(k in _tid_l for k in ("infra", "framework", "common", "harness", "bench_common", "scaffold", "spec")):

                _has_infra = True

            _framework_text = f"{_prompt}\n{_framework}"
            if _framework.strip():
                _refs_shared = True
            if any(k in _framework_text for k in ("bench_common", "统一框架", "统一测量", "共享框架", "shared framework", "统一 CSV", "统一计时")):

                # 引用了共享框架 → 视为已有框架

                _refs_shared = True

            _role_text = f"{_tid_l} {_prompt.lower()}"

            if any(k in _role_text for k in (
                "test", "verify", "review", "report", "doc", "chart", "plot", "draw", "visual",
                "测试", "验证", "审查", "报告", "文档", "图表", "绘图",
            )):
                continue

            # 横向比较 peer:产 results_*.csv,或实现/实验一种可比较对象

            _produces_result_csv = any(

                isinstance(o, str) and _re.match(r"results_[a-z_0-9]+\.csv$", o.strip().lower())

                for o in _outs

            )
            _produces_code = any(
                isinstance(o, str) and str(o).strip().lower().endswith((
                    ".c", ".cc", ".cpp", ".h", ".hpp", ".py", ".rs", ".go", ".java", ".ts", ".js"
                ))
                for o in _outs
            )
            _comparison_signal = any(k in _role_text for k in (
                "benchmark", "bench", "performance", "compare", "comparison", "vs ",
                "基准", "性能", "对比", "比较",
            ))

            if _kind in ("code", "coding", "") and (_produces_result_csv or (_produces_code and _comparison_signal)):

                _peer_producers.append(_tid)

        if _has_infra or _refs_shared:

            return None  # 已有框架/引用框架 → 放行

        if len(_peer_producers) >= 3:

            return {

                "issue": "missing_unified_benchmark_framework",

                "task_ids": _peer_producers[:20],

                "details": (

                    f"{len(_peer_producers)} 个算法/实验 helper 作为同类 peer 做横向对比,"

                    f"但没有统一测量框架 task(infra/共享 bench_common.h)。各自发明 benchmark 会导致"

                    f"计时精度/内存口径/CSV 格式不一致 → 对比数据失真(如把对手低效实现说成自己快上千倍)。"

                    f"可恢复的框架事实:需要一个统一计时器、统一内存口径、统一 CSV schema,并让各 peer "

                    f"引用同一框架后再产生可比较结果。"

                ),

            }

    except Exception:

        pass

    return None



def _is_true_horizontal_framework_block(cleaned: list[dict], task_ids: list[str] | None = None) -> bool:
    """Return True only for genuine peer implementations/experiments needing one benchmark framework.

    LLM guard output is intentionally advisory; this deterministic check prevents a mixed pipeline
    such as implementation + tests + benchmark + report from being killed as a false comparison.
    """
    try:
        import re as _re

        selected = {str(x).strip() for x in (task_ids or []) if str(x).strip()}
        tasks = [c for c in cleaned if not selected or str(c.get("task_id", "")).strip() in selected]
        if not tasks:
            return False

        if any(c.get("resume") for c in tasks):
            return False

        has_infra = False
        peer_candidates: list[str] = []
        for c in tasks:
            tid = str(c.get("task_id", "")).strip()
            tid_l = tid.lower()
            kind = str(c.get("kind", "")).strip().lower()
            mode = str(c.get("mode", "")).strip().lower()
            prompt = str(c.get("prompt", "") or "")
            framework = str(c.get("framework", "") or "")
            prompt_l = prompt.lower()
            outs = c.get("expected_outputs") or []

            if mode == "hard" and tid_l.endswith("_hard"):
                continue
            if any(k in tid_l for k in ("infra", "framework", "common", "harness", "bench_common", "scaffold", "spec")):
                has_infra = True
            framework_text = f"{prompt}\n{framework}"
            if framework.strip():
                has_infra = True
            if any(k in framework_text for k in ("bench_common", "统一框架", "统一测量", "共享框架", "shared framework", "统一 CSV", "统一计时")):
                has_infra = True

            role_text = f"{tid_l} {prompt_l}"
            if any(k in role_text for k in (
                "test", "verify", "review", "report", "doc", "chart", "plot", "draw", "visual",
                "测试", "验证", "审查", "报告", "文档", "图表", "绘图",
            )):
                continue

            produces_result_csv = any(
                isinstance(o, str) and _re.match(r"results_[a-z_0-9]+\.csv$", o.strip().lower())
                for o in outs
            )
            produces_code = any(
                isinstance(o, str) and o.strip().lower().endswith((
                    ".c", ".cc", ".cpp", ".h", ".hpp", ".py", ".rs", ".go", ".java", ".ts", ".js"
                ))
                for o in outs
            )
            comparison_signal = any(k in role_text for k in (
                "benchmark", "bench", "performance", "compare", "comparison", "vs ",
                "基准", "性能", "对比", "比较",
            ))

            if kind in ("code", "coding", "") and (produces_result_csv or (produces_code and comparison_signal)):
                peer_candidates.append(tid)

        if has_infra:
            return False
        return len(peer_candidates) >= 3
    except Exception:
        return False


def _has_embedded_peer_framework_contract(cleaned: list[dict], task_ids: list[str] | None = None) -> bool:
    """Return True when peer tasks already carry a usable shared framework contract."""
    try:
        def _framework_to_text(raw: object) -> str:
            try:
                from app.llm.tools.delegate_framework import normalize_framework_contract
                normalized = normalize_framework_contract(raw, max_chars=4000)
            except Exception:
                normalized = ""
            if isinstance(raw, (dict, list, tuple)):
                try:
                    serialized = json.dumps(raw, ensure_ascii=False, sort_keys=True)
                except Exception:
                    serialized = str(raw or "")
                text = f"{normalized}\n{serialized}".strip()
            else:
                text = normalized or str(raw or "")
            return text.strip()

        def _framework_signal_groups(text: str) -> int:
            lower = text.lower()
            groups = 0
            if any(marker in lower for marker in (
                "goal", "purpose", "role", "from_file", "任务目标", "目标", "用途", "角色", "来源",
            )):
                groups += 1
            if any(marker in lower for marker in (
                "interface", "schema", "criteria", "template", "required_subsections",
                "subsections", "outline", "evidence_map", "benchmark", "comparison",
                "维度", "标准", "比较", "基准", "模板", "大纲", "证据", "伪代码", "复杂度",
            )):
                groups += 1
            if any(marker in lower for marker in (
                "output", "expected", "expected_outputs", "file", "artifact",
                "输出", "文件", "产物", "保存",
            )):
                groups += 1
            if any(marker in lower for marker in (
                "acceptance", "acceptance_checks", "validation", "check", "verify",
                "验收", "验证", "检查", "可合并",
            )):
                groups += 1
            if any(marker in lower for marker in (
                "contract", "framework", "spec", "契约", "框架", "规格",
            )):
                groups += 1
            return groups

        def _dict_has_structured_contract(raw: object) -> bool:
            if not isinstance(raw, dict):
                return False
            keys = {str(k).lower() for k in raw.keys()}
            has_source_or_role = bool(keys & {"goal", "purpose", "role", "from_file", "contract", "framework"})
            has_structure = bool(keys & {"template", "schema", "outline", "interfaces", "required_subsections", "evidence_map"})
            has_output = bool(keys & {"output", "expected_outputs", "outputs"})
            has_acceptance = bool(keys & {"acceptance", "acceptance_checks", "validation", "checks"})
            return (has_structure and has_output) or (has_source_or_role and has_output and has_acceptance)

        selected = {str(x).strip() for x in (task_ids or []) if str(x).strip()}
        tasks = [c for c in cleaned if not selected or str(c.get("task_id", "")).strip() in selected]
        if len(tasks) < 2:
            return False
        peer_tasks = list(tasks)

        # Generic contract path: a real `framework` field is stronger evidence
        # than prompt wording. It applies to code, read, edit, draw, and verify
        # fan-out as long as every selected peer receives a concrete contract.
        framework_texts: list[str] = []
        for c in peer_tasks:
            if bool(c.get("resume")):
                return True
            raw_framework = c.get("framework")
            framework = _framework_to_text(raw_framework)
            framework = framework.strip()
            if len(framework) < 60:
                framework_texts = []
                break
            if isinstance(raw_framework, dict) and not _dict_has_structured_contract(raw_framework):
                framework_texts = []
                break
            framework_texts.append(framework.lower())
        if len(framework_texts) == len(peer_tasks):
            common_text = "\n".join(framework_texts)
            signal_groups = _framework_signal_groups(common_text)
            if signal_groups >= 2:
                return True

        code_peer_tasks = [
            c for c in peer_tasks
            if str(c.get("kind", "")).strip().lower() in {"code", "coding", ""}
        ]
        if len(code_peer_tasks) < 2:
            return False
        matched = 0
        for c in code_peer_tasks:
            prompt = str(c.get("prompt", "") or "")
            framework = str(c.get("framework", "") or "")
            text = f"{prompt}\n{framework}".lower()
            expected = " ".join(str(x).lower() for x in (c.get("expected_outputs") or []))
            has_self_contained = any(
                marker in prompt
                for marker in ("自包含", "内嵌", "不依赖外部文件")
            ) or any(
                marker in text
                for marker in ("self-contained", "embedded benchmark", "no external dependency")
            )
            has_shared_protocol = (
                (
                    "csv" in text
                    and (
                        "algorithm,operation,data_size,distribution,rep,time_ns,memory_bytes" in text
                        or all(k in text for k in ("data_size", "distribution", "time_ns"))
                    )
                )
                or ("统一" in prompt and ("csv" in text or "基准" in prompt or "benchmark" in text))
                or ("same benchmark" in text or "same output" in text)
            )
            has_outputs = ".csv" in expected or "benchmark_" in expected or "results_" in expected
            if has_self_contained and has_shared_protocol and has_outputs:
                matched += 1
        return matched == len(code_peer_tasks)
    except Exception:
        return False





def _detect_duplicate_chart_tasks(cleaned: list[dict]) -> dict | None:

    """检测图表任务重复派发:一个 helper 画全部 N 张图 + 多个 helper 各画一张。"""

    try:

        import re as _re

        _png_re = _re.compile(r"chart_[a-z_0-9]*\.png$", _re.I)

        _multi_chart_helpers = []

        _single_chart_helpers = []

        for c in cleaned:

            _tid = str(c.get("task_id", "")).strip()

            _mode = str(c.get("mode", "")).strip().lower()

            if _mode == "hard" and _tid.endswith("_hard"):

                continue

            _outs = [

                o for o in (c.get("expected_outputs") or [])

                if isinstance(o, str) and _png_re.search(o.strip().lower())

            ]

            if len(_outs) >= 3:

                _multi_chart_helpers.append(_tid)

            elif len(_outs) == 1:

                _single_chart_helpers.append(_tid)

        if _multi_chart_helpers and len(_single_chart_helpers) >= 2:

            return {

                "issue": "duplicate_chart_tasks",

                "multi_chart_task": _multi_chart_helpers[:5],

                "single_chart_tasks": _single_chart_helpers[:10],

                "details": (

                    f"图表任务重复派发:helper {_multi_chart_helpers} 声称一次画多张图,"

                    f"同时另有 {len(_single_chart_helpers)} 个 helper 各画一张"

                    f"({_single_chart_helpers[:6]})。两套并存会产生两套文件命名"

                    f"(画全部的常用无前缀名 chart_X.png,各画一张的落盘带 helper 前缀 "

                    f"draw_X_chart_X.png)→ 交付清单易混乱、漏列、误报'图没生成'。"

                    f"请择一:要么 1 个 helper 画全部,要么每图 1 个 helper,不要两套并存。"

                ),

            }

    except Exception:

        pass

    return None





def _detect_timing_precision_loss(csv_abs_path: str) -> list[dict]:

    """检测计时精度不足:某操作大量 time 值为 0 或被量化到同一极小值。"""

    warnings: list[dict] = []

    try:

        import csv as _csv_m

        from collections import defaultdict as _dd

        with open(csv_abs_path, "r", encoding="utf-8-sig", errors="replace") as f:

            rows = list(_csv_m.DictReader(f))

        if len(rows) < 4:

            return warnings

        cols = {c.lower().strip(): c for c in rows[0].keys() if c}

        time_col = next((cols[k] for k in ("time_us", "time_ms", "time", "ms", "us", "latency_ms") if k in cols), None)

        op_col = next((cols[k] for k in ("operation", "op") if k in cols), None)

        if not (time_col and op_col):

            return warnings

        by_op = _dd(list)

        for r in rows:

            try:

                tv = float(r.get(time_col, -1))

                op = (r.get(op_col, "") or "").strip()

                if op and tv >= 0:

                    by_op[op].append(tv)

            except (ValueError, TypeError):

                continue

        for op, vals in by_op.items():

            if len(vals) < 4:

                continue

            _zero_or_tiny = sum(1 for v in vals if v <= 0.001)

            if _zero_or_tiny / len(vals) > 0.4:

                warnings.append({

                    "issue": "timing_precision_loss",

                    "operation": op,

                    "zero_ratio": round(_zero_or_tiny / len(vals), 2),

                    "details": (

                        f"操作 '{op}' 有 {_zero_or_tiny}/{len(vals)} 个耗时记录 ≈0(精度不足)。"

                        f"该操作太快,当前计时单位测不出 → 画对数图时贴底/不可读,对比失真。"

                        f"请用更高精度计时器(纳秒 clock_gettime)或对快操作增大测量批量"

                        f"(循环 K 次取均值),让小耗时也有有效数字。"

                    ),

                })

    except (OSError, ValueError, ImportError):

        pass

    return warnings





def _detect_benchmark_csv_schema_issues(csv_abs_path: str) -> list[dict]:

    """交付前校验 benchmark CSV 是否符合统一 schema(列数一致 + 有算法名列)。"""

    warnings: list[dict] = []

    try:

        import csv as _csv_m

        from collections import Counter as _Counter

        with open(csv_abs_path, "r", encoding="utf-8-sig", errors="replace") as f:

            rows = list(_csv_m.reader(f))

        rows = [r for r in rows if any(c.strip() for c in r)]

        if len(rows) < 3:

            return warnings

        _col_counts = _Counter(len(r) for r in rows[1:])

        if not _col_counts:

            return warnings

        _mode_cols, _ = _col_counts.most_common(1)[0]

        _bad_rows = [i + 2 for i, r in enumerate(rows[1:]) if len(r) != _mode_cols]

        if _bad_rows and len(_bad_rows) < len(rows):

            warnings.append({

                "issue": "benchmark_csv_column_mismatch",

                "expected_columns": _mode_cols,

                "bad_row_count": len(_bad_rows),

                "sample_rows": _bad_rows[:5],

                "details": (

                    f"CSV 各行列数不一致:多数行 {_mode_cols} 列,但有 {len(_bad_rows)} 行列数不同"

                    f"(行号样本 {_bad_rows[:5]})。常见于某规模(如大 N)绕过统一框架手写输出,"

                    f"导致缺算法名列或列错位 → 画图时该批数据被静默丢弃。"

                    f"请确认所有规模都用统一框架的 CSV 输出函数,列数/列序一致。"

                ),

            })

        _header = [c.strip().lower() for c in rows[0]]

        if _header and _header[0] in ("algorithm", "algo", "name"):

            _numeric_first = sum(

                1 for r in rows[1:]

                if r and r[0].strip().replace(".", "", 1).isdigit()

            )

            if _numeric_first:

                warnings.append({

                    "issue": "benchmark_csv_missing_algorithm_name",

                    "bad_row_count": _numeric_first,

                    "details": (

                        f"表头首列为算法名,但有 {_numeric_first} 行首列是数字(算法名缺失/列错位)。"

                        f"这些行画图时按算法分组会被丢弃。请确认大规模数据行也写入了算法名列。"

                    ),

                })

    except (OSError, ValueError, ImportError):

        pass

    return warnings





def _detect_memory_non_monotonic(csv_abs_path: str) -> list[dict]:

    """检测内存随 N 非单调(内存测量误差信号)。"""

    warnings: list[dict] = []

    try:

        import csv as _csv_m

        from collections import defaultdict as _dd

        with open(csv_abs_path, "r", encoding="utf-8-sig", errors="replace") as f:

            rows = list(_csv_m.DictReader(f))

        if len(rows) < 4:

            return warnings

        cols = {c.lower().strip(): c for c in rows[0].keys() if c}

        n_col = next((cols[k] for k in ("n", "size", "scale") if k in cols), None)

        mem_col = next((cols[k] for k in ("memory_kb", "memory", "mem_kb", "mem") if k in cols), None)

        algo_col = next((cols[k] for k in ("algorithm", "algo", "name") if k in cols), None)

        op_col = next((cols[k] for k in ("operation", "op") if k in cols), None)

        if not (n_col and mem_col):

            return warnings

        series = _dd(dict)

        for r in rows:

            try:

                n = float(r.get(n_col, 0) or 0)

                m = float(r.get(mem_col, 0) or 0)

                a = (r.get(algo_col, "") or "").strip() if algo_col else ""

                o = (r.get(op_col, "") or "").strip() if op_col else ""

                if n > 0 and m > 0:

                    series[(a, o)][n] = m

            except (ValueError, TypeError):

                continue

        for (a, o), nm in series.items():

            xs = sorted(nm)

            if len(xs) < 3:

                continue

            for i in range(1, len(xs)):

                if nm[xs[i]] < nm[xs[i - 1]] * 0.8:

                    warnings.append({

                        "issue": "benchmark_memory_non_monotonic",

                        "algorithm": a or "?",

                        "operation": o or "?",

                        "details": (

                            f"内存随数据量非单调:{a or '算法'} 在 N {xs[i-1]:.0f}→{xs[i]:.0f} 时"

                            f"内存 {nm[xs[i-1]]:.0f}→{nm[xs[i]]:.0f}KB 反而下降 >20%。"

                            f"内存不应随数据量减少 → memory_usage() 可能漏算节点或测错时机,请核对。"

                        ),

                    })

                    break

    except (OSError, ValueError, ImportError):

        pass

    return warnings







    """检测单个 benchmark CSV 里某操作随 N 的增长是否异常(疑似实现低效/bug)。



    病因(实测 trace c6e42ed6 论文"1100 倍"假象): rbtree range_query 随 N 平方增长

    (N×10 → 时间×149), 是把范围查询写成 O(n^2) 的实现 bug; 论文却拿它当"HAT 快 1100 倍"

    的对比基准 → 夸张失真。单文件即可查: 取某 (operation,distribution) 下 time vs N 的标度,

    若 time 增长指数显著 > 1.3(明显超线性, 接近平方), 报警 helper "此操作实现可能低效"。



    返回 quality_warning dict 列表(可能空)。纯 stdlib, 不抛异常。

    """

    warnings: list[dict] = []

    try:

        import csv as _csv_m

        import math as _math

        with open(csv_abs_path, "r", encoding="utf-8-sig", errors="replace") as f:

            rows = list(_csv_m.DictReader(f))

        if len(rows) < 6:

            return warnings

        cols = {c.lower().strip(): c for c in rows[0].keys() if c}

        n_col = next((cols[k] for k in ("n", "size", "scale") if k in cols), None)

        op_col = next((cols[k] for k in ("operation", "op") if k in cols), None)

        time_col = next((cols[k] for k in ("time_ms", "time", "ms", "latency_ms") if k in cols), None)

        dist_col = next((cols[k] for k in ("distribution", "dist") if k in cols), None)

        if not (n_col and op_col and time_col):

            return warnings

        # 按 (operation, distribution) 分组收集 (N, time)

        from collections import defaultdict as _dd

        series = _dd(list)

        for r in rows:

            try:

                n = float(r.get(n_col, 0) or 0)

                t = float(r.get(time_col, 0) or 0)

                op = (r.get(op_col, "") or "").strip()

                dist = (r.get(dist_col, "") or "").strip() if dist_col else ""

                if n > 0 and t > 0 and op:

                    series[(op, dist)].append((n, t))

            except (ValueError, TypeError):

                continue

        for (op, dist), pts in series.items():

            # 需要至少 3 个不同规模, 且最大最小 N 跨度足够(≥10x)才能估标度

            uniq_n = sorted({n for n, _ in pts})

            if len(uniq_n) < 3 or uniq_n[-1] / uniq_n[0] < 10:

                continue

            # 每个 N 取中位 time

            by_n = _dd(list)

            for n, t in pts:

                by_n[n].append(t)

            xs = sorted(by_n)

            x0, x1 = xs[0], xs[-1]

            t0 = sorted(by_n[x0])[len(by_n[x0]) // 2]

            t1 = sorted(by_n[x1])[len(by_n[x1]) // 2]

            if t0 <= 0 or t1 <= 0:

                continue

            # 标度指数 p: time ∝ N^p → p = log(t1/t0)/log(x1/x0)

            p = _math.log(t1 / t0) / _math.log(x1 / x0)

            # 线性/对数线性 p≈1; 平方 p≈2。range/search 类应 ~1, p>1.6 明显异常。

            if p > 1.6:

                warnings.append({

                    "issue": "benchmark_complexity_anomaly",

                    "operation": op,

                    "scaling_exponent": round(p, 2),

                    "details": (

                        f"操作 '{op}'({dist or 'all'}) 耗时随 N 的标度指数≈{p:.2f}"

                        f"(N {x0:.0f}→{x1:.0f} 时 {t0:.3f}→{t1:.3f}ms),明显超线性、接近平方,"

                        f"疑似实现低效(如范围查询对每个元素从根重查)。"

                        f"这会让对比基准失真——拿它当对手会得出虚高的加速比。"

                        f"请核对该操作实现复杂度,确认是算法本身还是实现 bug。"

                    ),

                })

    except (OSError, ValueError, ImportError, ZeroDivisionError):

        pass

    return warnings





def _detect_broad_code_task_warning(cleaned: list[dict]) -> str | None:
    """Deprecated compatibility shim for older callers.

    Broad-task signals are now passed as neutral guard observations through
    `_detect_broad_code_task_warning_v2`. This helper must not return a
    deterministic planning decision.
    """
    return None

def _framework_split_exemption(
    task: dict,
    *,
    total_task_count: int,
    outputs: list[str],
    prompt_l: str,
    enum_signals: list[str],
    comparison_pipeline: bool,
) -> str | None:
    """Single entry for "is this framework-style task exempt from the broad-code
    split warning". Returns the exemption reason or None.

    2026-06-11 Round 17 (#3): merges three historically separate predicates
    (compact contract / scoped fanout / bounded scaffold) that accumulated as
    patches. Shared preconditions are checked once; variant-specific shape
    checks follow. Behavior is the union of the former three.

    framework 拆分豁免单入口；返回豁免原因枚举(compact_contract/scoped_fanout/
    bounded_scaffold)或 None。
    """
    # Variant 1: compact framework/spec contract — delegated to the dedicated
    # module check; has its own preconditions and does not require framework=.
    if _is_single_compact_framework_contract_task_for_guard(task):
        return "compact_contract"

    # Shared preconditions for the fanout/scaffold variants.
    if not str(task.get("framework") or "").strip():
        return None
    if comparison_pipeline or enum_signals:
        return None
    if not outputs or len(outputs) > 8:
        return None
    if not (task.get("acceptance_checks") or []):
        return None
    normalized = [output.replace("\\", "/").lstrip("./").strip() for output in outputs]
    if not all(path == "_env" or path.startswith("_env/") for path in normalized):
        return None

    task_id_l = str(task.get("task_id") or "").lower()

    # Variant 2: bounded component inside an already split fanout batch.
    if total_task_count >= 2 and len(prompt_l) <= 2400:
        if not any(
            marker in prompt_l
            for marker in (
                "benchmark all", "final paper", "generate final docx",
                "assemble final report", "implement all algorithms", "compare all algorithms",
            )
        ):
            component_groups = (
                ("core", "_env/core/", "_env/run.py", "_env/requirements.txt"),
                ("ui", "_env/ui/"),
                ("native", "_env/native/"),
                ("test", "_env/tests/", "_env/fixtures/"),
                ("fixture", "_env/tests/", "_env/fixtures/"),
                ("doc", "_env/docs/", "_env/readme.md"),
                ("script", "_env/scripts/"),
            )
            for group in component_groups:
                name, *prefixes = group
                if name not in task_id_l:
                    continue
                if all(any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in prefixes) for path in normalized):
                    return "scoped_fanout"
            first_dirs = {
                parts[1]
                for path in normalized
                if (parts := path.split("/")) and len(parts) >= 3 and parts[0] == "_env"
            }
            if len(first_dirs) == 1:
                return "scoped_fanout"
            if len(normalized) <= 5 and any(k in task_id_l for k in ("readme", "docs", "script", "fixture")):
                return "scoped_fanout"

    # Variant 3: one compact technical scaffold defining shared interfaces.
    role_text = f"{task_id_l} {prompt_l}"
    if any(marker in role_text for marker in ("framework", "scaffold", "infra", "contract", "harness", "spec")):
        if not any(marker in prompt_l for marker in ("implement all algorithms", "benchmark all", "final paper", "generate final docx")):
            return "bounded_scaffold"

    return None


def _is_single_compact_framework_contract_task_for_guard(task: dict) -> bool:
    """Return True for a compact framework/spec helper that should not be split."""
    try:
        from app.llm.tools.delegate_framework import is_compact_framework_contract_task
        return is_compact_framework_contract_task(task)
    except Exception:
        return False


def _detect_broad_code_task_warning_v2(cleaned: list[dict]) -> list[dict] | None:

    """Return neutral attention facts for broad ordinary code tasks.



    Environment project tasks are allowed to be a little larger only for a

    bounded vertical slice. `_env/` ownership paths are isolation boundaries,

    not a reason to let one helper absorb weakly coupled project work.

    """

    from app.core.runtime_mode import is_environment_mode



    warnings: list[dict] = []
    total_task_count = len(cleaned)

    for task in cleaned:

        kind = str(task.get("kind") or "").lower()

        if kind not in ("code", "coding") or task.get("resume"):

            continue



        prompt = str(task.get("prompt") or "")

        expected_outputs = task.get("expected_outputs") or []

        outputs = [

            str(output).replace("\\", "/").lstrip("./").strip()

            for output in expected_outputs

            if str(output).strip()

        ]

        prompt_l = prompt.lower()
        # Round 17 (#3): unified framework-exemption check, first variant
        # (compact contract) short-circuits the whole warning like before.
        if _framework_split_exemption(
            task,
            total_task_count=total_task_count,
            outputs=outputs,
            prompt_l=prompt_l,
            enum_signals=[],
            comparison_pipeline=False,
        ) == "compact_contract":
            continue



        greenfield_vertical_slice = False
        if is_environment_mode():
            prompt_l_for_slice = prompt.lower()
            outputs_l = [o.lower() for o in outputs]
            has_project_creation_goal = any(
                marker in prompt_l_for_slice
                for marker in (
                    "empty directory",
                    "from scratch",
                    "greenfield",
                    "new project",
                    "create a project",
                    "build a project",
                    "空目录",
                    "从零",
                    "新项目",
                )
            )
            has_vertical_slice_outputs = (
                any(o.startswith(("src/", "_env/src/")) or "/src/" in o for o in outputs_l)
                and any(o.startswith(("tests/", "_env/tests/")) or "/tests/" in o for o in outputs_l)
                and (
                    any(o.startswith(("scripts/", "_env/scripts/")) or "check_project" in o for o in outputs_l)
                    or any(o.endswith(("/readme.md", "readme.md")) or "/docs/" in o for o in outputs_l)
                )
                and any(o.endswith(".md") or o.endswith(".txt") for o in outputs_l)
            )
            has_validation_goal = any(
                marker in prompt_l_for_slice
                for marker in (
                    "self-check",
                    "smoke",
                    "pytest",
                    "compile",
                    "verify",
                    "validation",
                    "restart behavior",
                    "game loop",
                    "自检",
                    "验证",
                )
            )
            greenfield_vertical_slice = (
                ("first slice" in prompt_l_for_slice or "vertical slice" in prompt_l_for_slice or "首个切片" in prompt_l_for_slice)
                and has_vertical_slice_outputs
                and has_validation_goal
                and len(outputs) <= 10
                and not any(
                    marker in prompt_l_for_slice
                    for marker in (
                        "complete project", "full project", "whole project", "all files",
                        "build the complete", "create all", "every file",
                        "完整项目", "整个项目", "全部文件", "所有文件",
                    )
                )
            )
            if greenfield_vertical_slice:
                top_dirs = {
                    parts[1]
                    for output in outputs_l
                    if output.startswith("_env/") and (parts := output.split("/")) and len(parts) >= 3
                }
                if len(top_dirs) > 3:
                    greenfield_vertical_slice = False
            env_narrow_project_slice = (
                bool(outputs)
                and len(outputs) <= 3
                and all(path == "_env" or path.startswith("_env/") for path in outputs)
                and not any(
                    marker in prompt_l_for_slice
                    for marker in (
                        "多种", "若干", "全部", "所有", "整套", "整个", "论文", "报告",
                        "compare", "comparison", "benchmark", "paper", "report",
                    )
                )
            )
            if env_narrow_project_slice:
                continue

        if greenfield_vertical_slice:

            continue



        enum_signals: list[str] = []

        for match in re.finditer(

            r"(\d+)\s*(?:types?|kinds?|variants?|modules?|files?|algorithms?)\s+([\w-]+)",

            prompt,

            re.I,

        ):

            try:

                n = int(match.group(1))

            except ValueError:

                continue

            if 3 <= n <= 50:

                enum_signals.append(f"{n} {match.group(2)[:16]}")



        for match in re.finditer(r"(\d+)\s*[x*]\s*(\d+)(?:\s*[x*]\s*(\d+))?", prompt, re.I):

            try:

                a = int(match.group(1))

                b = int(match.group(2))

                c = int(match.group(3)) if match.group(3) else 1

            except (TypeError, ValueError):

                continue

            product = a * b * c

            if 50 <= product <= 100000:

                enum_signals.append(f"{a}x{b}" + (f"x{c}" if c > 1 else "") + f"={product}")



        heading_items = [

            h.strip().strip("*").strip()

            for h in re.findall(r"^\s*#{2,6}\s+(.{1,80})$", prompt, re.MULTILINE)

            if h.strip()

        ]

        meaningful_headings = [

            h

            for h in heading_items

            if not any(

                skip in h.lower()

                for skip in (

                    "constraint",

                    "constraints",

                    "requirement",

                    "requirements",

                    "note",

                    "notes",

                    "delivery",

                    "output",

                    "outputs",

                )

            )

        ]

        numbered_items = re.findall(r"^\s*\d+[.)]\s+\S{2,40}", prompt, re.MULTILINE)

        if len(meaningful_headings) >= 3:

            enum_signals.append(f"{len(meaningful_headings)} headings")

        if len(numbered_items) >= 6:

            enum_signals.append(f"{len(numbered_items)} numbered items")

        peer_terms = re.findall(
            r"\b(?:bubble|quick|merge|heap|radix|insertion|selection|shell|tim|counting|bucket)\s*sort\b"
            r"|\b(?:quicksort|mergesort|heapsort|bubblesort|radixsort)\b"
            r"|\b(?:red[- ]?black|rb)\s*tree\b|\bskip\s*list\b|\bb[+-]?\s*tree\b|\bbtree\b|\bbptree\b"
            r"|\blsm(?:[- ]?tree)?\b|\bfractal\s*tree\b|\bavl\s*tree\b|\bhash\s*index\b"
            r"|冒泡排序|快速排序|归并排序|堆排序|插入排序|选择排序|希尔排序|基数排序|计数排序|桶排序"
            r"|红黑树|跳表|B树|B 树|B\+树|B\+ 树|LSM树|分形树|AVL树|哈希索引",
            prompt_l,
            re.I,
        )
        unique_peer_terms = {term.lower() for term in peer_terms}
        chinese_multi_algorithm_pipeline = bool(
            re.search(
                r"(?:分析|比较|实现|研究|撰写|形成).{0,40}(?:四种|五种|多种|若干|几个).{0,30}(?:算法|数据结构|索引|结构)",
                prompt,
            )
            or (
                len(unique_peer_terms) >= 3
                and any(k in prompt for k in ("论文", "报告", "比较", "性能", "基准", "研究", "实现"))
            )
        )
        comparison_pipeline = (
            len(unique_peer_terms) >= 3
            and any(k in prompt_l for k in ("benchmark", "bench", "performance", "compare", "comparison", "性能", "对比", "比较", "基准"))
            and any(k in prompt_l for k in ("report", "docx", "markdown", "paper", "论文", "报告", "长报告", "文档"))
        )
        if comparison_pipeline or chinese_multi_algorithm_pipeline:
            enum_signals.append(f"{len(unique_peer_terms)} comparable algorithms + benchmark/report")

        distribution_terms = [
            k for k in ("original", "random", "shuffled", "sorted", "ascending", "descending",
                        "原始", "随机", "打乱", "升序", "降序", "有序", "逆序")
            if k in prompt_l
        ]
        if len(distribution_terms) >= 3 and any(k in prompt_l for k in ("benchmark", "bench", "性能", "基准")):
            enum_signals.append(f"{len(distribution_terms)} benchmark distributions")



        _framework_exemption = _framework_split_exemption(
            task,
            total_task_count=total_task_count,
            outputs=outputs,
            prompt_l=prompt_l,
            enum_signals=enum_signals,
            comparison_pipeline=comparison_pipeline,
        )
        scoped_framework_fanout = _framework_exemption == "scoped_fanout"
        bounded_framework_scaffold = _framework_exemption == "bounded_scaffold"

        many_expected = (
            len(expected_outputs) >= 5
            and not scoped_framework_fanout
            and not bounded_framework_scaffold
        )

        long_with_enum = len(prompt) >= 2000 and bool(enum_signals)

        if len(enum_signals) >= 2 or long_with_enum or many_expected or comparison_pipeline:

            warnings.append(

                {

                    "task_id": task.get("task_id"),

                    "issue": "broad_code_task_before_spawn",

                    "severity": "high",

                    "signals": sorted(set(enum_signals))[:8],

                    "prompt_len": len(prompt),

                    "expected_outputs": len(expected_outputs),

                }

            )



    if not warnings:
        return None
    return [_guard_observation_from_payload(w) for w in warnings]


def _expand_environment_expected_outputs(prompt: str, expected_outputs: list[str]) -> list[str]:
    """Prefer full staged `_env/...` ownership paths when the prompt names them.

    This keeps helper write ownership aligned with environment project staging:
    if a task output is `models.py` and the task prompt names
    `_env/src/pkg/models.py`, the helper should own the staged project path.
    """
    if not prompt or not expected_outputs:
        return expected_outputs
    try:
        from app.core.runtime_mode import is_environment_mode
        if not is_environment_mode():
            return expected_outputs
    except Exception:
        return expected_outputs

    prompt_norm = prompt.replace("\\", "/")
    env_paths = [
        "_env/" + m.group(1).strip("`'\".,;:()[]{}")
        for m in re.finditer(
            r"_env/([^`'\"<>|\r\n]+?\.[A-Za-z0-9]{1,12})(?=\s|$)",
            prompt_norm,
        )
    ]
    env_paths.extend(
        m.group(0).replace("\\", "/").strip("`'\".,;:()[]{}")
        for m in re.finditer(r"_env/[^\s`'\"<>|]+", prompt_norm)
    )
    if not env_paths:
        return expected_outputs

    by_base: dict[str, list[str]] = {}
    for path in env_paths:
        base = path.rsplit("/", 1)[-1].lower()
        if "." not in base:
            continue
        by_base.setdefault(base, []).append(path)

    expanded: list[str] = []
    seen: set[str] = set()
    for raw in expected_outputs:
        out = str(raw or "").replace("\\", "/").strip().lstrip("./")
        if not out:
            continue
        replacements = by_base.get(out.rsplit("/", 1)[-1].lower(), []) if not out.startswith("_env/") else []
        candidates = replacements or [out]
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                expanded.append(candidate)
    return expanded[:20]


def _derive_environment_write_scopes(expected_outputs: list[str]) -> list[str]:
    """Derive component write scopes from concrete staged project outputs."""
    cleaned = [
        str(path or "").replace("\\", "/").lstrip("./").strip().rstrip("/")
        for path in expected_outputs or []
        if str(path or "").strip()
    ]
    env_paths = [path for path in cleaned if path == "_env" or path.startswith("_env/")]
    if not env_paths:
        return []
    first_dirs: set[str] = set()
    for path in env_paths:
        parts = path.split("/")
        if len(parts) >= 3:
            first_dirs.add("/".join(parts[:3]))
    if len(first_dirs) == 1:
        return sorted(first_dirs)
    grouped_prefixes = [
        ("_env/core/", "_env/core"),
        ("_env/tests/", "_env/tests"),
        ("_env/fixtures/", "_env/fixtures"),
        ("_env/ui/", "_env/ui"),
        ("_env/native/", "_env/native"),
        ("_env/scripts/", "_env/scripts"),
        ("_env/docs/", "_env/docs"),
    ]
    scopes: set[str] = set()
    for path in env_paths:
        parts = path.split("/")
        if len(parts) >= 4 and parts[0] == "_env" and parts[1] == "src":
            scopes.add("/".join(parts[:3]))
        elif path.startswith("_env/src/"):
            scopes.add("_env/src")
    for prefix, scope in grouped_prefixes:
        if any(path.startswith(prefix) for path in env_paths):
            scopes.add(scope)
    scopes.update(path for path in env_paths if path.count("/") == 1 and path != "_env")
    return sorted(scopes)


_SOURCE_EDIT_OUTPUT_SUFFIXES = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".c", ".cc", ".cpp",
    ".h", ".hpp", ".cs", ".php", ".rb", ".swift", ".kt", ".scala",
)


def _augment_code_repair_expected_outputs(
    *,
    kind: str,
    prompt: str,
    input_files: list[str],
    expected_outputs: list[str],
) -> list[str]:
    """Add missing staged source outputs for explicit code repair ownership."""
    if kind not in {"code", "coding"}:
        return expected_outputs
    prompt_text = str(prompt or "")
    if not prompt_text or not input_files:
        return expected_outputs
    prompt_l = prompt_text.lower()
    if not re.search(r"\b(fix|repair|patch|modify|edit|update|implement|debug)\b|修复|修改|实现|调试", prompt_l):
        return expected_outputs
    existing_keys = {
        str(path).replace("\\", "/").lstrip("./").removeprefix("_env/").lower()
        for path in expected_outputs or []
    }

    def _input_path_is_removed_or_renamed(norm_path: str, base_name: str) -> bool:
        """Return true when the prompt describes the input path as removed/renamed.

        Input-files are often read-side context. For code repair we normally add
        edited source inputs to expected_outputs so helper copyback keeps the
        modified project slices. Do not do that for a source path that the task
        explicitly renames or deletes; the replacement target should be the
        declared output instead.

        输入文件通常是读取上下文；显式重命名/删除的旧路径不应被自动变成输出契约。
        """
        path = re.escape(norm_path.lower())
        base = re.escape(base_name.lower())
        rename_words = r"(?:rename|move|replace\s+file|renamed|moved|重命名|改名|移动|迁移)"
        delete_words = r"(?:delete|remove|drop|deprecate|deleted|removed|删除|移除|废弃)"
        arrow = r"(?:->|→|=>|to\b|as\b|为|成|到)"
        file_target = r"[`\"']?[\w./\\-]+\.[a-z0-9]{1,12}[`\"']?"
        return bool(
            re.search(rf"{rename_words}[^\n.;。；]{{0,180}}(?:{path}|{base})[^\n.;。；]{{0,80}}{arrow}[^\n.;。；]{{0,40}}{file_target}", prompt_l)
            or re.search(rf"(?:{path}|{base})[^\n.;。；]{{0,80}}{arrow}[^\n.;。；]{{0,40}}{file_target}", prompt_l)
            and re.search(rf"{rename_words}[^\n.;。；]{{0,220}}(?:{path}|{base})", prompt_l)
            or re.search(rf"{delete_words}[^\n.;。；]{{0,180}}(?:{path}|{base})", prompt_l)
            or re.search(rf"(?:{path}|{base})[^\n.;。；]{{0,180}}{delete_words}", prompt_l)
        )

    edit_words = r"(?:fix|repair|patch|modify|edit|update|implement|debug|change|修复|修改|实现|调试|更新|变更)"

    def _target_pattern(norm_path: str, base_name: str) -> str:
        path_l = norm_path.lower()
        base_l2 = base_name.lower()
        path_pat = rf"(?<![\w/.-]){re.escape(path_l)}(?![\w/.-])"
        base_pat = rf"(?<![\w/.-]){re.escape(base_l2)}(?![\w/.-])"
        return rf"(?:{path_pat}|{base_pat})"

    def _path_windows(norm_path: str, base_name: str) -> list[str]:
        pattern = re.compile(_target_pattern(norm_path, base_name))
        windows: list[str] = []
        for match in pattern.finditer(prompt_l):
            start = max(0, match.start() - 220)
            end = min(len(prompt_l), match.end() + 220)
            windows.append(prompt_l[start:end])
        return windows

    def _input_path_is_context_only(norm_path: str, base_name: str, windows: list[str]) -> bool:
        target = _target_pattern(norm_path, base_name)
        same_clause = r"[^\n.;。；]"
        context_patterns = (
            rf"\b(?:read|inspect|review|reference|refer\s+to|use|using)\b{same_clause}{{0,100}}{target}",
            rf"{target}{same_clause}{{0,100}}\b(?:as\s+context|for\s+context|context\s+only|read[-\s]?only|reference\s+only)\b",
            rf"(?:不要|不应|不可|别){same_clause}{{0,100}}(?:修改|编辑|改动){same_clause}{{0,100}}{target}",
            rf"{target}{same_clause}{{0,100}}(?:不要|不应|不可|别){same_clause}{{0,100}}(?:修改|编辑|改动)",
        )
        return any(
            re.search(pattern, window, re.IGNORECASE)
            for window in windows
            for pattern in context_patterns
        )

    def _input_path_has_edit_signal(norm_path: str, base_name: str, windows: list[str]) -> bool:
        target = _target_pattern(norm_path, base_name)
        return any(
            re.search(rf"{edit_words}[\s\S]{{0,220}}{target}", window, re.IGNORECASE)
            or re.search(rf"{target}[\s\S]{{0,220}}{edit_words}", window, re.IGNORECASE)
            for window in windows
        )

    augmented = list(expected_outputs or [])
    for raw in input_files:
        norm = str(raw or "").replace("\\", "/").strip().strip("`\"'").lstrip("./")
        if not norm:
            continue
        base = norm.rsplit("/", 1)[-1]
        base_l = base.lower()
        if "/test" in f"/{norm.lower()}" or base_l.startswith("test_"):
            continue
        if not base_l.endswith(_SOURCE_EDIT_OUTPUT_SUFFIXES):
            continue
        if base_l not in prompt_l:
            continue
        if _input_path_is_removed_or_renamed(norm, base):
            continue
        windows = _path_windows(norm, base)
        if _input_path_is_context_only(norm, base, windows):
            continue
        if not _input_path_has_edit_signal(norm, base, windows):
            continue
        staged = norm if norm.startswith("_env/") else f"_env/{norm}"
        staged_key = staged.removeprefix("_env/").lower()
        if norm.lower() in existing_keys or staged_key in existing_keys:
            continue
        augmented.append(staged)
        existing_keys.add(staged_key)
    return augmented[:20]


def _strip_redundant_input_file_source_blocks(prompt: str, input_files: list[str]) -> tuple[str, list[dict]]:
    """Omit source-code blocks that duplicate named input files in helper prompts.

    The helper can read `input_files` from its staged workspace. This keeps the
    helper contract compact without changing paths, goals, or acceptance checks.
    """
    text = str(prompt or "")
    if not text or not input_files or "```" not in text:
        return text, []
    markers: set[str] = set()
    for raw in input_files:
        norm = str(raw or "").replace("\\", "/").strip().strip("`\"'").lstrip("./")
        if not norm:
            continue
        markers.add(norm.lower())
        if norm.startswith("_env/"):
            markers.add(norm[len("_env/"):].lower())
        base = os.path.basename(norm)
        if base and "." in base:
            markers.add(base.lower())
    if not markers:
        return text, []

    omitted: list[dict] = []

    def _replace(match: re.Match) -> str:
        block = match.group(0)
        body = match.group("body") or ""
        if len(body.strip()) < 80:
            return block
        start, end = match.span()
        window = (text[max(0, start - 360):start] + text[end:min(len(text), end + 120)]).lower()
        matched = sorted(marker for marker in markers if marker and marker in window)
        if not matched:
            return block
        path_fact = matched[0]
        omitted.append({
            "path_marker": path_fact,
            "chars": len(body),
            "lines": body.count("\n") + (1 if body else 0),
        })
        return (
            "```text\n"
            f"[source body omitted; read the current file from input_files for path marker: {path_fact}]\n"
            "```"
        )

    compacted = re.sub(
        r"```(?P<lang>[A-Za-z0-9_+.-]*)\r?\n(?P<body>.*?)\r?\n```",
        _replace,
        text,
        flags=re.DOTALL,
    )
    return compacted, omitted


_HELPER_COMMAND_RECIPE_RE = re.compile(
    r"(?im)^\s*(?:steps?|commands?|workflow|procedure|approach)\s*:|"
    r"`\s*(?:python3|python|py|node|npm|pnpm|yarn|pytest|bash|sh|cmd|powershell)\b|"
    r"\b(?:run|execute|use)\s+`?(?:python3|python|py|node|npm|pnpm|yarn|pytest|bash|sh|cmd|powershell)\b"
)

_PYTHON3_ASSERTION_RE = re.compile(
    r"(?im)^\s*(?:use\s+)?`?python3`?\s+command[^\n]*(?:shim|maps?|mapped|alias|launcher|cmd)[^\n]*(?:\n|$)"
)

_EXHAUSTIVE_SCOPE_EXPANSION_RE = re.compile(
    r"(?is)\b(?:dump\s+all|full\s+data\s+dump|report\s+everything|complete\s+evidence\s+report|"
    r"list\s+all\s+tables|all\s+row\s+counts|all\s+rows|全量|全部数据|完整证据|报告所有)\b"
)

_QUOTED_USER_SCOPE_RE = re.compile(
    r"(?is)(?:the\s+user\s+(?:now\s+)?says|user\s+said|current\s+user|用户(?:现在)?(?:说|要求))\s*[:：]"
)

_STRUCTURED_SOURCE_INTERPRETATION_RE = re.compile(
    # Require digit-bearing structured signals. The old bare word
    # list (cost|count|hours?|...) fired on ordinary prose such as "respond
    # within the hour" and prepended a read-the-raw-fields directive to edit
    # helpers whose prompts only narrate text facts, pushing them into full
    # input re-reads.
    r"(?is)"
    r"(?:\$\s*\d|\d[\d,.]*\s*(?:nights?|days?|hours?|minutes?|seats?|users?|晚|天|小时|分钟|人)\b|"
    r"(?:\b(?:cost|price|budget|amount|fee|total|subtotal|unit|quantity|count|duration)\b|"
    r"费用|价格|预算|金额|合计|小计|单位|数量|时长)[^\n]{0,16}\d)"
)


def _mark_unverified_helper_source_interpretations(
    prompt: str,
    *,
    input_files: list[str],
) -> tuple[str, list[dict]]:
    """Mark main-thread interpretations of structured source fields as evidence to verify.

    Helper prompts often contain compact main-thread summaries plus concrete
    `input_files`. If the summary includes numeric fields, source labels, or
    unit/duration language, the helper should treat that summary as routing
    context and verify the raw source fields before computing final claims.
    """
    text = str(prompt or "")
    if not text or not input_files:
        return text, []
    if "## Source Field Provenance" in text:
        return text, []
    if not _STRUCTURED_SOURCE_INTERPRETATION_RE.search(text):
        return text, []
    block = (
        "## Source Field Provenance\n"
        "The task below may summarize structured source fields from the main process. Treat those summaries as routing context until the staged input files confirm them. "
        "For costs, prices, counts, quantities, durations, units, booleans, labels, or risk flags, read the raw field names/values/notes from input_files and compute or state ambiguity from that evidence. "
        "A total, package, included, safe, friendly, or fully satisfied interpretation needs explicit source wording or verification evidence.\n\n"
        "结构化源字段以 input_files 原文为准；金额、数量、时长、单位、布尔和风险含义需基于原字段计算或说明歧义。\n\n"
    )
    return block + text, [{
        "kind": "source_field_provenance_added",
        "input_files": len(input_files or []),
    }]


def _mark_unverified_helper_command_recipes(
    prompt: str,
    *,
    kind: str,
    input_files: list[str],
    acceptance_checks: list[str],
) -> tuple[str, list[dict]]:
    """Mark main-generated command recipes as non-authoritative helper evidence.

    The main process often delegates from compact path facts and a guessed plan.
    Concrete shell snippets in that plan can be useful, but they are not runtime
    evidence until the helper verifies them in its own staged workspace.
    """
    text = str(prompt or "")
    if kind not in {"code", "coding"} or not text or not input_files:
        return text, []
    if "## Command Recipe Provenance" in text:
        return text, []
    if not _HELPER_COMMAND_RECIPE_RE.search(text):
        return text, []

    observations: list[dict] = []
    rewritten = text
    if _PYTHON3_ASSERTION_RE.search(rewritten):
        rewritten, n = _PYTHON3_ASSERTION_RE.subn(
            "Launcher fact: the main-thread prompt mentioned a `python3`/shim launcher assumption. "
            "Treat it as unverified until a command result in this helper workspace proves it; use a proven launcher for local checks.\n",
            rewritten,
            count=4,
        )
        observations.append({"kind": "python3_launcher_assertion_softened", "count": n})

    block = (
        "## Command Recipe Provenance\n"
        "The task below may include concrete shell snippets, numbered steps, or launcher names generated by the main process from partial facts. "
        "Treat those snippets as non-authoritative examples unless they are explicit user text, explicit acceptance checks, or freshly verified in this helper workspace. "
        "Preserve the goal, input_files, expected_outputs, and acceptance_checks; choose commands from current platform evidence and stop repeating a command once its failure facts explain the blocker.\n\n"
        "命令片段属于主进程参考计划；除用户/验收项明确要求或本 helper 已验证外，不把具体 launcher/步骤当作事实。\n\n"
    )
    observations.append({
        "kind": "command_recipe_provenance_added",
        "acceptance_checks": len(acceptance_checks or []),
    })
    return block + rewritten, observations


def _mark_unverified_helper_scope_expansion(
    prompt: str,
    *,
    kind: str,
    input_files: list[str],
    expected_outputs: list[str],
    acceptance_checks: list[str],
) -> tuple[str, list[dict]]:
    """Mark exhaustive main-thread expansions as scope provenance facts."""
    text = str(prompt or "")
    if kind not in {"code", "coding"} or not text or not input_files:
        return text, []
    if "## Scope Provenance" in text:
        return text, []
    if not _EXHAUSTIVE_SCOPE_EXPANSION_RE.search(text):
        return text, []
    if not (_QUOTED_USER_SCOPE_RE.search(text) or acceptance_checks or expected_outputs):
        return text, []

    block = (
        "## Scope Provenance\n"
        "The task below may contain an exhaustive audit or reporting expansion written by the main process after quoting or summarizing the user request. "
        "Treat quoted user text, explicit expected_outputs, and acceptance_checks as scope facts. Treat broad phrases such as full dump, report everything, or complete evidence report as coverage requirements: preserve the relevant facts, but use the smallest sufficient structured probe/report and stop when the requested checks are covered. "
        "Do not reread generated facts files or repeat scans solely to restate evidence already captured; inspect a named missing detail only if one remains.\n\n"
        "范围来源事实：主进程可能把用户请求扩展成全量审计计划；保留验收覆盖，但用足够小的结构化证据完成，不为复述已覆盖事实反复读取。\n\n"
    )
    return block + text, [{
        "kind": "exhaustive_scope_provenance_added",
        "expected_outputs": len(expected_outputs or []),
        "acceptance_checks": len(acceptance_checks or []),
    }]


# 2026-06-04 P131: dispatch-time guard for read helpers — patterns defined earlier
# (see _deterministic_source_read_split_recommendations exemption).


def _detect_helper_produced_inputs(prompt: str, input_files: list, expected_outputs: list) -> list[str]:
    """Return basenames of inputs that look like helper-produced artifacts.

    Examines explicit input_files plus path-like tokens in the prompt body. Excludes
    expected_outputs (the helper is supposed to produce those).
    """
    def _path_keys(value: object) -> set[str]:
        text = str(value or "").replace("\\", "/").strip().strip("`\"'").lstrip("./").lower()
        if not text:
            return set()
        keys = {text}
        if text.startswith("_env/"):
            keys.add(text[5:])
        basename = text.rsplit("/", 1)[-1]
        if basename:
            keys.add(basename)
        return keys

    expected_set: set[str] = set()
    for expected in expected_outputs or []:
        expected_set.update(_path_keys(expected))
    candidates: list[str] = []
    for f in input_files or []:
        s = str(f or "").strip()
        if s and not (_path_keys(s) & expected_set):
            candidates.append(s)
    # extract path-like tokens from prompt
    for m in _re_p131.finditer(r"[A-Za-z0-9_\-./]+\.(?:md|markdown|txt|csv)", str(prompt or "")):
        tok = m.group(0)
        if not (_path_keys(tok) & expected_set) and tok not in candidates:
            candidates.append(tok)
    matched: list[str] = []
    for c in candidates:
        # Skip system-staged orientation files (project_inventory.md, resource_manifest, ...)
        basename = c.replace("\\", "/").rsplit("/", 1)[-1]
        if basename.lower() in _HELPER_PRODUCED_BASENAME_EXCEPTIONS:
            continue
        for pat in _HELPER_PRODUCED_NAME_PATTERNS:
            if pat.search(c):
                matched.append(c)
                break
    # de-dup preserve order
    seen: set[str] = set()
    out: list[str] = []
    for m in matched:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


_EXPLICIT_READ_ONLY_ANALYSIS_RE = re.compile(
    r"(read[-\s]?only\s+analysis|analysis\s+only|do\s+not\s+modify\s+files|"
    r"no\s+file\s+modifications|no\s+code\s+changes|只读分析|仅分析|"
    r"不要(?:自行)?修改(?:任何)?文件|不要(?:自行)?改(?:任何)?文件|"
    r"不要(?:自行)?改(?=$|[\s，。,.；;])|不要(?:自行)?修改(?=$|[\s，。,.；;])|"
    r"不改代码|不要改代码)",
    re.IGNORECASE,
)


def _read_only_analysis_output_conflict(prompt: str, expected_outputs: list[str]) -> bool:
    """Return true when a helper task asks for files while declaring no file changes."""
    if not expected_outputs:
        return False
    return bool(_EXPLICIT_READ_ONLY_ANALYSIS_RE.search(str(prompt or "")))


def _read_only_analysis_output_fact(
    *,
    task_id: str,
    kind: str,
    prompt: str,
    expected_outputs: list[str],
) -> dict:
    """Return a neutral guard fact for read-only wording plus declared outputs.

    Some tasks genuinely need internal evidence files even when the user asked
    for no user-facing file changes. The dispatcher should expose the conflict
    to the guard instead of deciding the task boundary by regex.

    只读措辞和声明产物冲突时只提供事实；是否拦截交给守卫。
    """
    return {
        "kind": "guard_observation",
        "issue": "read_only_analysis_output_conflict",
        "needs_attention": True,
        "task_id": task_id,
        "current_kind": kind,
        "expected_outputs": expected_outputs[:20],
        "prompt_excerpt": str(prompt or "")[:500],
        "details": (
            "The helper prompt contains read-only / analysis-only / no-modification wording while also declaring "
            "output files. This may be valid for internal evidence only when the current task needs it, but it may "
            "also violate the user's no-file-change boundary. The guard should decide whether this exact delegation "
            "may run as-is."
        ),
    }


async def _sanitize_and_validate_tasks(

    args: dict,

    *,

    main_workspace: str,

    archive_id: str,

    group_id: str,

    user_id: str,

) -> list[dict] | str:

    """Validate + clean legacy pairing + blanket resume check for delegate tasks.



    Returns list[dict] of cleaned tasks, or str (JSON error to return to caller).

    Extracted from handle_delegate so spawn_async can reuse the same logic.

    """

    tasks = args.get("tasks") or []

    if not isinstance(tasks, list) or not tasks:

        # 2026-05-12 P39: 检测 args={} (JSON parse 失败) 的特殊场景

        # 病因(实测 18:14 trace): LLM 输出 args 时未 escape prompt 内引号,

        # json.loads 失败 → args={} → 旧版报"0 tasks" 误导主线程, 死循环重试同样错的 JSON。

        if not args or args == {}:

            return json.dumps({

                "ok": False,

                "error": "tool_call_args_json_broken",

                "hint": TOOL_ARGS_JSON_BROKEN_HINT,

                "delegate_error": (
                    "tool call args were an empty dict {}; the JSON arguments were probably malformed.\n"
                    "工具参数为空对象，通常表示 JSON 参数生成失败。"
                ),

                "delegate_hint": (
                    "Regenerate the delegate tool call with valid JSON arguments. Escape quotes inside "
                    "string fields, keep task prompts concise, and omit the delegate call entirely when "
                    "there is no helper work to dispatch.\n"
                    "重新生成合法 JSON 参数；没有可派发任务时不要调用 delegate。"

                ),

                "raw_args_excerpt": "{}",

            }, ensure_ascii=False)

        return json.dumps({

            "ok": False,

            "error": "tasks must be a non-empty array",

            "hint": (
                "delegate(action='spawn') requires tasks=[{task_id, prompt, ...}, ...]. "
                "An empty task list does not resume existing helpers. To continue a helper, use the "
                "same task_id with resume=true and a new focused prompt. To inspect helper state, use "
                "status/list tools. If there is no helper work left, continue the main workflow without "
                "calling delegate again.\n"
                "delegate spawn 需要非空 tasks；续作使用同 task_id + resume=true，没任务时不要空调。"

            ),

        }, ensure_ascii=False)

    # 2026-05-10 Patch 61 v2: 同时覆盖 spawn 和 spawn_async

    _stub_block = _check_wait_stub_anti_pattern(tasks)

    if _stub_block:

        return _stub_block

    if len(tasks) > _MAX_DELEGATE_TASKS_PER_CALL:

        return json.dumps({

            "ok": False,

            "error": (
                f"At most {_MAX_DELEGATE_TASKS_PER_CALL} helper tasks can be spawned in one delegate call.\n"
                f"单次 delegate 最多 {_MAX_DELEGATE_TASKS_PER_CALL} 个并行任务。"
            ),

        }, ensure_ascii=False)

    if not main_workspace:

        return json.dumps({
            "ok": False,
            "error": "The main workspace is unavailable.\n主工作区不可用。",
        }, ensure_ascii=False)



    # Clean tasks

    cleaned: list[dict] = []

    for idx, t in enumerate(tasks):

        raw_tid = str(t.get("task_id", "")).strip()

        prompt = str(t.get("prompt", "")).strip()
        if not prompt:
            # Recover a common partial-field spelling seen in streamed tool calls
            # ("prom" instead of "prompt"). This preserves the model's intended
            # worker request while still exposing ordinary malformed JSON as an error.
            prompt_alias = str(t.get("prom", "")).strip()
            if prompt_alias:
                prompt = prompt_alias
                debug.log(
                    "delegate.task.prompt_alias_recovered",
                    f"task '{raw_tid or f'task{idx}'}' used `prom`; recovered it as `prompt`",
                )
        try:
            from app.llm.tools.delegate_framework import normalize_framework_contract
            framework = normalize_framework_contract(t.get("framework"))
        except Exception:
            framework = str(t.get("framework") or "").strip()[:1800]
        dispatch_reason = str(
            t.get("dispatch_reason")
            or t.get("routing_reason")
            or t.get("delegation_reason")
            or ""
        ).strip()[:1200]

        resume = bool(t.get("resume", False))

        fork_from = str(t.get("fork_from", "")).strip()

        raw_kind_value = str(t.get("kind") or "").strip().lower()
        if raw_kind_value == "general" or (not raw_kind_value and not resume):
            _retry_expected_outputs = [
                str(x).strip()
                for x in (t.get("expected_outputs") or [])
                if str(x).strip()
            ][:20]
            _retry_input_files = [
                str(x).strip()
                for x in (
                    t.get("input_files")
                    or t.get("source_files")
                    or t.get("transferred_files")
                    or t.get("files")
                    or []
                )
                if str(x).strip()
            ][:60]
            _retry_acceptance_checks = [
                str(x).strip()
                for x in (t.get("acceptance_checks") or t.get("checks") or [])
                if str(x).strip()
            ][:20]
            return json.dumps({
                "ok": False,
                "error": "unsupported_helper_kind",
                "error_kind": "unsupported_helper_kind",
                "allowed_kinds": list(MODEL_VISIBLE_HELPER_KINDS),
                "hint": (
                    "The general helper kind has been removed. Re-issue the same helper request with a concrete "
                    "kind chosen from code/read/edit/verify/draw/tts/project_map/file_summary/impact_review/inventory. "
                    "Use code for implementation, scripts, benchmarks, statistics, or technical framework files; "
                    "read for source-material extraction; edit for final documents; project-analysis kinds for "
                    "read-only project understanding. If this is a true continuation, pass resume=true and the "
                    "original task_id so the system can inherit the previous kind.\n"
                    "general helper 已移除；请用明确 kind 重新派发。"
                ),
                "task_id": raw_tid or f"task{idx}",
                "original_prompt": prompt[:1200],
                "expected_outputs": _retry_expected_outputs,
                "next_delegate_shape": {
                    "action": "spawn",
                    "tasks": [{
                        "task_id": raw_tid or f"task{idx}",
                        "kind": "<choose code/read/edit/verify/draw/tts/project_map/file_summary/impact_review/inventory>",
                        "mode": str(t.get("mode") or "easy").strip().lower() or "easy",
                        "resume": False,
                        "framework": str(t.get("framework") or "").strip()[:1800],
                        "input_files": _retry_input_files,
                        "prompt": prompt,
                        "expected_outputs": _retry_expected_outputs,
                        "acceptance_checks": _retry_acceptance_checks,
                    }],
                    "note": (
                        "Choose a supported concrete kind from the work product. Preserve the same logical task, "
                        "framework, input_files, expected_outputs, and acceptance_checks; only split if the task is broad."
                    ),
                },
            }, ensure_ascii=False)
        if raw_kind_value and raw_kind_value not in VALID_HELPER_KINDS:
            _retry_expected_outputs = [
                str(x).strip()
                for x in (t.get("expected_outputs") or [])
                if str(x).strip()
            ][:20]
            _retry_input_files = [
                str(x).strip()
                for x in (
                    t.get("input_files")
                    or t.get("source_files")
                    or t.get("transferred_files")
                    or t.get("files")
                    or []
                )
                if str(x).strip()
            ][:60]
            _retry_acceptance_checks = [
                str(x).strip()
                for x in (t.get("acceptance_checks") or t.get("checks") or [])
                if str(x).strip()
            ][:20]
            return json.dumps({
                "ok": False,
                "error": "unsupported_helper_kind",
                "error_kind": "unsupported_helper_kind",
                "task_id": raw_tid or f"task{idx}",
                "requested_kind": raw_kind_value,
                "allowed_kinds": list(MODEL_VISIBLE_HELPER_KINDS),
                "fact": (
                    "The delegate call requested a helper kind that is not exposed in the current helper-kind "
                    "schema. No helper was started. Choose one supported kind from the work product and reissue "
                    "the same task envelope; OCR and Office are helper tools/capabilities, not public helper kinds."
                ),
                "事实": (
                    "本次 delegate 请求了当前 schema 未公开的 helper kind；没有启动 helper。请按任务产物选择一个公开 kind 后重发；"
                    "OCR 和 Office 是 helper 内工具/能力，不是公开 helper kind。"
                ),
                "original_prompt": prompt[:1200],
                "expected_outputs": _retry_expected_outputs,
                "next_delegate_shape": {
                    "action": "spawn",
                    "tasks": [{
                        "task_id": raw_tid or f"task{idx}",
                        "kind": "<choose code/read/edit/verify/draw/tts/project_map/file_summary/impact_review/inventory>",
                        "mode": str(t.get("mode") or "easy").strip().lower() or "easy",
                        "resume": False,
                        "framework": str(t.get("framework") or "").strip()[:1800],
                        "input_files": _retry_input_files,
                        "prompt": prompt,
                        "expected_outputs": _retry_expected_outputs,
                        "acceptance_checks": _retry_acceptance_checks,
                    }],
                    "note": (
                        "Choose a supported concrete kind from the work product. Preserve the same logical task, "
                        "framework, input_files, expected_outputs, and acceptance_checks."
                    ),
                },
            }, ensure_ascii=False)

        kind, mode = _normalize_helper_kind_mode(t.get("kind"), t.get("mode"))

        # 2026-05-21: resume 且主线程未显式传 kind 时,从完成 ledger 继承原 kind。

        # 病因(实测 trace c647979 08:42): code 任务 resume 时主线程没带 kind →

        # _normalize 默认成 general → code 任务用 general helper 续作(工具集/模型档错配),

        # 跑满 900s 0 产出。_auto_correct 只能把 doc/draw 纠正回去, 救不回 code。

        # 修法: resume + 未传 kind 时, 按 task_id 查 ledger 里上次的 kind 继承(保守:

        # 仅在主线程没显式指定 kind 时生效, 显式传了就尊重主线程)。

        if resume and not (str(t.get("kind") or "").strip()):

            try:

                _tid_norm = _sanitize_task_id(raw_tid or f"task{idx}", idx)

                _trace_for_ledger = debug.current_trace_id() or ""

                _prev = None

                for _e in reversed(_get_completion_ledger(_trace_for_ledger, last_n=50)):

                    if _e.get("task_id") == _tid_norm and _e.get("kind"):

                        _prev = str(_e.get("kind")).strip().lower()

                        break

                if _prev and _prev in VALID_HELPER_KINDS and _prev != kind:

                    debug.log(

                        "delegate.resume.kind_inherited",

                        f"task '{raw_tid}' resume 未传 kind → 从 ledger 继承原 kind "

                        f"{_prev!r}(原默认 {kind!r}),避免 code→general 退化",

                    )

                    kind = _prev

            except Exception as _e_inherit:

                debug.log(

                    "delegate.resume.kind_inherit_failed",

                    f"{type(_e_inherit).__name__}: {_e_inherit}",

                )

        # 2026-05-11 Tier 1.C: expected_outputs 系统比对

        # spawn 时主线程声明 ["abpt.c", "results_abpt.csv"], 完成时系统验收。

        # 把"是否齐"从 LLM 判断变成系统判断, 主线程不必再判 LLM 漏看产物。

        _expected_outputs = t.get("expected_outputs")

        if isinstance(_expected_outputs, list):

            # 净化: 只接受字符串项, 不超过 20 个

            expected_outputs = [

                str(x).strip() for x in _expected_outputs[:20] if str(x).strip()

            ]

        else:

            expected_outputs = []
        task_guard_observations: list[dict] = []
        _raw_write_scopes = t.get("write_scopes") or t.get("output_scopes") or t.get("ownership_scopes")
        if isinstance(_raw_write_scopes, str):
            write_scopes = [x.strip(" -\t") for x in _raw_write_scopes.splitlines() if x.strip(" -\t")][:20]
        elif isinstance(_raw_write_scopes, (list, tuple, set)):
            write_scopes = [str(x).strip() for x in _raw_write_scopes if str(x).strip()][:20]
        elif isinstance(_raw_write_scopes, dict):
            write_scopes = [f"{k}: {v}" for k, v in _raw_write_scopes.items() if str(k).strip()][:20]
        else:
            write_scopes = []

        # 2026-06-05: dispatch-time rewrite — helper cannot write to `_shared/` (read-only scaffold).
        # 病因(实测 14:41 trace 394304bbb02940e7): 主线程把 `_shared/file_map.json` 列入 expected_outputs,
        # helper 反复尝试 workspace.write 全部被 `_shared/` read-only 守卫拦截,iter 18-23 连续失败,
        # 最终触发 `delegate.framework_design.stuck` 浪费 224s 后中断,主线程后续重做框架。
        # 修法: spawn 时把 expected_outputs/write_scopes/prompt 中的 `_shared/...` 自动改写到
        # `_helpers_shared/...`(后者会自动合并回主区,语义保留),避免 helper 接到不可能完成的契约。
        def _rewrite_shared_to_helpers_shared(items: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
            rewrites: list[tuple[str, str]] = []
            new_items: list[str] = []
            for item in items:
                norm = item.replace("\\", "/").lstrip("./")
                if norm == "_shared" or norm.startswith("_shared/"):
                    fixed = "_helpers_shared/" + norm[len("_shared/"):] if norm.startswith("_shared/") else "_helpers_shared"
                    rewrites.append((item, fixed))
                    new_items.append(fixed)
                else:
                    new_items.append(item)
            return new_items, rewrites

        expected_outputs, _exp_rewrites = _rewrite_shared_to_helpers_shared(expected_outputs)
        write_scopes, _ws_rewrites = _rewrite_shared_to_helpers_shared(write_scopes)
        if _exp_rewrites or _ws_rewrites:
            try:
                # Rewrite only the specific deliverable paths in the prompt — leave any read-side
                # `_shared/...` references untouched (helpers can read `_shared/`, just not write).
                _seen_paths: set[str] = set()
                for _orig, _fixed in (_exp_rewrites + _ws_rewrites):
                    if _orig in _seen_paths or _orig == _fixed:
                        continue
                    _seen_paths.add(_orig)
                    if _orig in prompt:
                        prompt = prompt.replace(_orig, _fixed)
            except Exception:
                pass
            debug.log(
                "delegate.expected_outputs.shared_rewritten",
                (
                    f"task '{raw_tid or f'task{idx}'}' rewrote `_shared/` paths to `_helpers_shared/`: "
                    f"expected_outputs={_exp_rewrites} write_scopes={_ws_rewrites}. "
                    f"Reason: helper sandbox treats `_shared/` as read-only; `_helpers_shared/` auto-merges back to main."
                ),
            )

        if kind in {"project_map", "file_summary", "impact_review", "inventory", "summarize"} and expected_outputs:
            task_guard_observations.append({
                "kind": "guard_observation",
                "issue": "read_only_project_analysis_output_conflict",
                "needs_attention": True,
                "task_id": raw_tid or f"task{idx}",
                "current_kind": kind,
                "expected_outputs": expected_outputs[:20],
                "prompt_excerpt": prompt[:500],
                "details": (
                    "This project-analysis helper kind is normally read-only but the task declares output files. "
                    "The guard should decide whether this exact delegation may run as-is, should use another helper "
                    "kind, or should report text-only evidence."
                ),
            })

        # If the main process omitted expected_outputs, keep regex extraction as
        # evidence only. Do not convert candidates into the helper contract:
        # output ownership is model-planned or helper-reported, while disk
        # recovery still has the helper's final Output files JSON.

        if not expected_outputs and kind not in {"ocr", "project_map", "file_summary", "impact_review", "inventory", "summarize"}:

            _candidate_outputs = _infer_expected_outputs_from_prompt(prompt)

            if _candidate_outputs:

                debug.log(

                    "delegate.expected_outputs.candidate_fact",

                    f"task '{raw_tid}' 主线程没传 expected_outputs, "

                    f"仅记录候选产物事实: {_candidate_outputs}",

                )
                task_guard_observations.append({
                    "kind": "guard_observation",
                    "issue": "undeclared_output_candidates",
                    "needs_attention": True,
                    "task_id": raw_tid or f"task{idx}",
                    "candidate_outputs": _candidate_outputs[:20],
                    "fact": (
                        "The helper prompt mentions output-like filenames, but the main process did not declare "
                        "expected_outputs. These names are candidates from text extraction, not a system-owned "
                        "output contract."
                    ),
                })
                _candidate_lines = "\n".join(f"- {x}" for x in _candidate_outputs[:12])
                prompt += (
                    "\n\n## Candidate Output Facts\n"
                    "The main process did not declare `expected_outputs`. The prompt text mentions these output-like filenames:\n"
                    f"{_candidate_lines}\n"
                    "These are facts from prompt text, not a declared output contract. If you create files, finish by reporting exact existing paths in the required Output files JSON block; if the candidates are merely inputs, old files, or downstream outputs, say so in your report.\n"
                    "候选文件名只来自提示词文本，不是系统声明产物；创建文件后按实际存在路径自报，若只是输入/旧文件/下游产物则说明。"
                )

        if _read_only_analysis_output_conflict(prompt, expected_outputs):
            task_guard_observations.append(_read_only_analysis_output_fact(
                task_id=raw_tid or f"task{idx}",
                kind=kind,
                prompt=prompt,
                expected_outputs=expected_outputs,
            ))

        def _read_output_key(value: object) -> str:
            text = str(value or "").replace("\\", "/").strip().strip("`\"'").lstrip("./").lower()
            if text.startswith("_env/"):
                text = text[5:]
            return text.rsplit("/", 1)[-1]

        if kind == "read" and expected_outputs:
            _read_expected_before = list(expected_outputs)
            expected_outputs = _filter_read_helper_expected_outputs(prompt, expected_outputs)
            task_guard_observations.extend(_read_helper_project_visible_output_facts(
                task_id=raw_tid or f"task{idx}",
                prompt=prompt,
                expected_before=_read_expected_before,
                expected_after=expected_outputs,
            ))
            _read_kept_keys = {_read_output_key(x) for x in expected_outputs}
            _read_dropped = [
                x for x in _read_expected_before
                if _read_output_key(x) not in _read_kept_keys
            ]
            if expected_outputs != _read_expected_before:
                debug.log(
                    "delegate.expected_outputs.read_evidence_filtered",
                    (
                        f"task '{raw_tid}' kind='read' keeps only internal txt evidence outputs; "
                        f"before={_read_expected_before}, after={expected_outputs}, dropped={_read_dropped}"
                    ),
                )
            if expected_outputs:
                prompt = _rewrite_staged_read_evidence_mentions(prompt)
                _evidence_names = ", ".join(expected_outputs[:8])
                prompt += (
                    "\n\n## Internal Evidence Output Override\n"
                    "Your read evidence output is internal helper evidence, not a project file. "
                    f"Write the final evidence text at the helper sandbox root using: {_evidence_names}. "
                    "If the original request mentioned `_env/<evidence>.txt`, keep the same basename and do not write it under `_env/`; `_env/` is reserved for staged project source files.\n"
                    "内部证据写到 helper 沙箱根目录或共享区；不要写入 `_env/` 项目暂存区。"
                )
                # 2026-06-05: 当过滤掉了 .csv/.json/.png 等非 txt 产物时,显式告诉 helper
                # 这些产物不应由本 helper 直接生成,而是写到证据 .txt 里供下游消费 helper
                # 转换。原 prompt 里的 CSV 路径只是源材料引用,不是本任务交付物。
                # 病因(实测 trace 373640 16:57:29): survey-existing-algos 是 read kind,
                # 但 expected_outputs 含 complexity_table.csv,helper 按 prompt 写 CSV
                # 时被 read_helper_workspace_path_forbidden 拦,误以为是路径问题反复重试。
                if _read_dropped:
                    _dropped_show = ", ".join(_read_dropped[:6])
                    prompt += (
                        "\n\n## Non-txt outputs in original request — handle carefully\n"
                        f"The original task request listed these non-`.txt` outputs: {_dropped_show}.\n"
                        "Read helpers can only WRITE `.txt` evidence files — they are not allowed to "
                        "produce CSV, JSON, PNG, source code, or Office files directly. Treat each "
                        "non-`.txt` item above as either source material to read, OR as a deliverable "
                        "that a downstream helper will assemble from your evidence text. Capture the "
                        "structured content (e.g. CSV columns, JSON fields) inside your evidence "
                        "`.txt` so a later code/edit helper can convert it.\n"
                        "本任务是 read helper，只能写 .txt 证据；CSV/JSON/PNG/源码/Office 类产物请把结构化内容嵌在 .txt 里，由下游 code/edit helper 转换。"
                    )

        expected_outputs = _expand_environment_expected_outputs(prompt, expected_outputs)
        if kind == "read" and expected_outputs:
            _read_expected_after_expand = list(expected_outputs)
            expected_outputs = _filter_read_helper_expected_outputs(prompt, expected_outputs)
            if expected_outputs != _read_expected_after_expand:
                debug.log(
                    "delegate.expected_outputs.read_evidence_recanonicalized",
                    (
                        f"task '{raw_tid}' kind='read' restored internal evidence outputs after environment path expansion; "
                        f"before={_read_expected_after_expand}, after={expected_outputs}"
                    ),
                )
        if not write_scopes:
            write_scopes = _derive_environment_write_scopes(expected_outputs)

        def _clean_task_list_field(*names: str, max_items: int = 40) -> list[str]:
            for name in names:
                raw_value = t.get(name)
                if raw_value in (None, "", [], {}):
                    continue
                if isinstance(raw_value, str):
                    values = [x.strip(" -\t") for x in raw_value.splitlines()]
                elif isinstance(raw_value, dict):
                    values = [f"{k}: {v}" for k, v in raw_value.items()]
                elif isinstance(raw_value, (list, tuple, set)):
                    values = [str(x).strip() for x in raw_value]
                else:
                    values = [str(raw_value).strip()]
                cleaned_values = []
                seen_values = set()
                for value in values:
                    if not value or value in seen_values:
                        continue
                    seen_values.add(value)
                    cleaned_values.append(value[:500])
                    if len(cleaned_values) >= max_items:
                        break
                if cleaned_values:
                    return cleaned_values
            return []

        input_files = _clean_task_list_field(
            "input_files", "source_files", "transferred_files", "files",
            max_items=60,
        )
        acceptance_checks = _clean_task_list_field(
            "acceptance_checks", "checks",
            max_items=20,
        )
        if input_files:
            _prompt_before_source_mark = prompt
            prompt, _source_field_observations = _mark_unverified_helper_source_interpretations(
                prompt,
                input_files=input_files,
            )
            if _source_field_observations and prompt != _prompt_before_source_mark:
                debug.log(
                    "delegate.prompt.source_field_provenance_added",
                    (
                        f"task '{raw_tid or f'task{idx}'}' marked structured source summaries "
                        f"as unverified until input_files are read: {_source_field_observations[:6]}"
                    ),
                )
        if kind in {"code", "coding"} and input_files:
            _prompt_before_recipe_mark = prompt
            prompt, _command_recipe_observations = _mark_unverified_helper_command_recipes(
                prompt,
                kind=kind,
                input_files=input_files,
                acceptance_checks=acceptance_checks,
            )
            if _command_recipe_observations and prompt != _prompt_before_recipe_mark:
                debug.log(
                    "delegate.prompt.command_recipe_provenance_added",
                    (
                        f"task '{raw_tid or f'task{idx}'}' marked main-thread command recipes "
                        f"as unverified helper evidence: {_command_recipe_observations[:6]}"
                    ),
                )
            _prompt_before_scope_mark = prompt
            prompt, _scope_observations = _mark_unverified_helper_scope_expansion(
                prompt,
                kind=kind,
                input_files=input_files,
                expected_outputs=expected_outputs,
                acceptance_checks=acceptance_checks,
            )
            if _scope_observations and prompt != _prompt_before_scope_mark:
                debug.log(
                    "delegate.prompt.scope_provenance_added",
                    (
                        f"task '{raw_tid or f'task{idx}'}' marked exhaustive helper scope "
                        f"as main-thread expansion evidence: {_scope_observations[:6]}"
                    ),
                )
        if kind in {"code", "coding"} and input_files:
            _prompt_before_strip = prompt
            prompt, _omitted_source_blocks = _strip_redundant_input_file_source_blocks(prompt, input_files)
            if _omitted_source_blocks and prompt != _prompt_before_strip:
                debug.log(
                    "delegate.prompt.input_file_source_blocks_omitted",
                    (
                        f"task '{raw_tid or f'task{idx}'}' omitted "
                        f"{len(_omitted_source_blocks)} embedded source block(s) already covered by input_files: "
                        f"{_omitted_source_blocks[:6]}"
                    ),
                )
        if kind in {"code", "coding"} and input_files:
            _expected_before_code_ownership = list(expected_outputs)
            expected_outputs = _augment_code_repair_expected_outputs(
                kind=kind,
                prompt=prompt,
                input_files=input_files,
                expected_outputs=expected_outputs,
            )
            if expected_outputs != _expected_before_code_ownership:
                if not write_scopes:
                    write_scopes = _derive_environment_write_scopes(expected_outputs)
                else:
                    _derived_scopes = set(_derive_environment_write_scopes(expected_outputs))
                    if _derived_scopes:
                        write_scopes = sorted(set(write_scopes) | _derived_scopes)
                debug.log(
                    "delegate.expected_outputs.code_repair_augmented",
                    (
                        f"task '{raw_tid or f'task{idx}'}' added source ownership outputs "
                        f"from input_files: before={_expected_before_code_ownership}, after={expected_outputs}"
                    ),
                )
        if kind == "read" and acceptance_checks:
            acceptance_checks = [
                _rewrite_staged_read_evidence_mentions(check)
                for check in acceptance_checks
            ]

        if not prompt:

            continue

        if kind == "read":

            prompt = (

                "## Read Task Contract\n"

                "- Work from concrete source files. If no concrete source is available, return FAIL or request the missing resource with the exact path needed.\n"

                "- In project mode, first inspect `_env/project_inventory.md` or `_env/.resource_manifest.json` when present. Treat manifest `project_path` and `staged_path` entries as the path source of truth. Read existing staged paths exactly. If a needed project file is listed but not staged, try `fetch_to_temp(source='main', paths=[project_path])` once; if it remains unavailable, request that exact `project_path` and wait to resume with its `_env/...` copy.\n"
                "- Project-relative paths may start with folders such as `_extracted/`; those are still project files, not helper-internal paths. Fetch or request their staged `_env/...` copies before inspecting them.\n"

                "- First infer the evidence standard from the purpose: quick understanding, verbatim transcription, numbers/labels, formulas/tables/questions/options, clarity/readability, or downstream solving/document writing.\n"

                "- Use structured read tools for text and Office material when they provide reliable content. For visual or scanned content, call `ocr` with allow_upgrade=true and max_tier='accurate' when exact wording, IDs, numbers, labels, formulas, tables, questions/options, transcription, or clarity/readability matters. Reuse cache when its tier and quality satisfy the purpose. Stop when evidence is sufficient or no stronger tier is available; preserve uncertainty instead of repeating the same path/tier.\n"

                "- Use `workspace` only to write `.txt` source evidence. Keep preprocessing, scripting, final writing, and user-facing synthesis outside this helper unless a provided read tool directly supports the extraction.\n"

                "- Structure the `.txt` as source evidence: source file, purpose, confirmed content, uncertain content, quality notes, and suggested line ranges. It is internal evidence for the main thread, not user-facing copy; keep tier/cache/engine details in quality notes only when useful.\n"

                "- If characters appear only as candidates or quality hints, place them in uncertain content rather than confirmed content.\n\n"

                "read helper 按用途读取材料并写内部 txt 证据；项目文件先看资源清单，按精确 project_path/staged_path 获取和读取，缺路径就 request_resource；精确视觉证据需要 allow_upgrade。\n\n"

                f"## Main Thread Task\n{prompt}"

            )
        else:
            try:
                from app.core.runtime_mode import is_environment_mode as _is_environment_mode
                _env_mode_for_helper_prompt = bool(_is_environment_mode())
            except Exception:
                _env_mode_for_helper_prompt = False
            if _env_mode_for_helper_prompt:
                prompt = (
                    "## Environment Project Facts\n"
                    "- The helper sandbox and `_env/...` paths are staged handoff copies. Editing `_env/...` changes that staged copy; it does not by itself prove the real project path or a running service changed.\n"
                    "- For an existing running project URL/service, helper `_env/...` edits do not affect that service until the main process applies the staged change. A helper can report pre-apply live evidence and staged/local evidence; final live URL success after apply is main-process evidence unless a tool explicitly wrote the real project path.\n"
                    "- Explicit `input_files` are normally staged into the helper as `_env/<project-relative-path>` copies. Helper tools work on local workspace paths such as `_env/app.py`; env_* project tools are main-process tools, not helper tools.\n"
                    "- When an input file is listed as a bare project path such as `app.py`, first try the staged local path `_env/app.py`. A same-named bare path may be absent or unrelated helper-local scratch; use the `_env/...` copy or the resource manifest path facts as truth.\n"
                    "- For a staged Python project, `_env` is the project root for imports. Running pytest against `_env/tests/...` from the outer helper workspace can leave sibling modules outside `sys.path`; run from `_env` or set an equivalent project-root import path when validating staged Python files.\n"
                    "- If a needed project file is not staged, report the exact project-relative path needed so the main process can fetch it and resume the helper.\n"
                    "- A project-source change is ready for main acceptance only when the changed staged path is copied back/applied, or a tool explicitly writes the real project path. State which path changed and what evidence proves it.\n"
                    "- If your available tools cannot apply a staged source change to the real project, report the staged path, intended project-relative path, diff/evidence, and verification blocker so the main process can apply and verify.\n"
                    "- Run checks against the path/service that the acceptance condition uses; if staged and real-project results differ, treat that difference as evidence to resolve before claiming completion.\n\n"
                    "项目 helper 摘要：显式 input_files 通常已暂存到 `_env`；裸项目路径先查 `_env/<路径>` 或资源清单；Python 项目测试以 `_env` 为导入根；helper 用本地路径，env_* 由主进程执行；报告改动是否已应用和验证证据。\n\n"
                    f"## Main Thread Task\n{prompt}"
                )

        tid = _sanitize_task_id(raw_tid or f"task{idx}", idx)

        if any(c["task_id"] == tid for c in cleaned):

            tid = f"{tid}_{idx}"

        inherited_contract = None
        if resume:
            try:
                inherited_contract = _get_task_contract(debug.current_trace_id() or "", tid)
            except Exception as _e_contract:
                inherited_contract = None
                debug.log(
                    "delegate.resume.contract_lookup_failed",
                    f"{type(_e_contract).__name__}: {_e_contract}",
                )
        if inherited_contract:
            inherited_fields: list[str] = []
            if not str(t.get("framework") or "").strip() and not framework:
                framework = str(inherited_contract.get("framework") or "").strip()
                if framework:
                    inherited_fields.append("framework")
            if not input_files:
                input_files = list(inherited_contract.get("input_files") or [])
                if input_files:
                    inherited_fields.append("input_files")
            if not expected_outputs:
                expected_outputs = list(inherited_contract.get("expected_outputs") or [])
                if expected_outputs:
                    inherited_fields.append("expected_outputs")
            if not acceptance_checks:
                acceptance_checks = list(inherited_contract.get("acceptance_checks") or [])
                if acceptance_checks:
                    inherited_fields.append("acceptance_checks")
            if not str(t.get("kind") or "").strip():
                inherited_kind = str(inherited_contract.get("kind") or "").strip().lower()
                if inherited_kind in VALID_HELPER_KINDS and inherited_kind != kind:
                    kind = inherited_kind
                    inherited_fields.append("kind")
            if inherited_fields:
                debug.log(
                    "delegate.resume.contract_inherited",
                    (
                        f"task '{tid}' inherited missing helper envelope field(s): "
                        f"{', '.join(inherited_fields)}"
                    ),
                )

        # 2026-06-04 P131: dispatch-time fact — read helper is usually for
        # user/source material, while helper-produced artifacts often belong to
        # consumer edit/code/verify work. This is not a program decision; attach
        # the observation so the preflight guard can allow or block the exact
        # delegation after reading dispatch_reason and current task facts.
        if kind in ("read", "ocr") and not resume:
            offending = _detect_helper_produced_inputs(prompt, input_files, expected_outputs)
            if offending:
                task_guard_observations.append({
                    "kind": "guard_observation",
                    "issue": "read_helper_targets_helper_produced_artifacts",
                    "needs_attention": True,
                    "task_id": tid,
                    "current_kind": kind,
                    "inputs": offending[:12],
                    "details": (
                        "This read helper references files that look helper-produced rather than original user/source "
                        "materials. Depending on the current task, consuming those artifacts may belong to edit/code, "
                        "expanding the producer, or verify. The guard should decide whether this exact read delegation "
                        "may run as-is."
                    ),
                })

        cleaned_task = {

            "task_id": tid, "prompt": prompt,

            "resume": resume, "fork_from": fork_from,

            "kind": kind,

            "mode": mode,

            "expected_outputs": expected_outputs,  # 1.C 新增

            "write_scopes": write_scopes,

            "framework": framework,
            "dispatch_reason": dispatch_reason,

            "input_files": input_files,

            "acceptance_checks": acceptance_checks,

        }
        if task_guard_observations:
            cleaned_task["guard_observations"] = list(task_guard_observations)
        cleaned.append(cleaned_task)

    if not cleaned:

        return json.dumps({
            "ok": False,
            "error": "no valid tasks after sanitize",
            "error_kind": "empty_delegate_tasks",
            "hint": (
                "delegate spawn/spawn_async requires non-empty tasks. "
                "Use wait/collect/status actions instead of delegate tasks=[] for waiting. "
                "If helpers are already running, use delegate(action='collect'/'wait_any'/'status') "
                "with task_ids or continue other main-thread work.\n\n"
                "派发 helper 时 tasks 必须非空；等待已有 helper 请用 wait/collect/status。"
            ),
        }, ensure_ascii=False)


    try:
        from app.llm.tools.delegate_actions import _normalize_environment_output_paths_from_manifest
        _normalize_environment_output_paths_from_manifest(main_workspace, cleaned)
    except Exception as _e_env_out_norm:
        debug.log(
            "delegate.environment.expected_outputs.normalize_failed",
            f"{type(_e_env_out_norm).__name__}: {_e_env_out_norm}",
        )

    guard_observations: list[dict] = []
    broad_task_observations = _detect_broad_code_task_warning_v2(cleaned)
    if broad_task_observations:
        guard_observations.extend(broad_task_observations)
        debug.log(
            "delegate.broad_code_task.observed",
            f"{len(broad_task_observations)} broad code task fact(s) added to guard input",
        )



    # Framework concentration facts are attached before the preflight guard.
    # The symbolic detector must not decide the boundary by itself; the guard
    # LLM sees these facts and is the component that may return a hard block.
    #
    # 框架集中度检测只注入事实；是否硬拦截由启动前守卫 LLM 判断。

    _fw_warning = _detect_missing_unified_framework(cleaned)

    if _fw_warning is not None:
 
        args["_framework_warning"] = _fw_warning
        guard_observations.append(_guard_observation_from_payload(_fw_warning))
 
        debug.log(

            "delegate.framework_fallback.detected",

            f"{len(_fw_warning.get('task_ids', []))} 算法各产 results_*.csv 无统一框架: "

            f"{_fw_warning.get('task_ids')}",

        )

    if guard_observations:
        args.setdefault("_guard_observations", []).extend(guard_observations)
        by_tid = {
            str(task.get("task_id") or "").strip(): task
            for task in cleaned
            if isinstance(task, dict)
        }
        batch_target = cleaned[0] if cleaned else None
        for observation in guard_observations[:24]:
            target = by_tid.get(str(observation.get("task_id") or "").strip()) or batch_target
            if target is not None:
                target.setdefault("guard_observations", []).append(observation)



    # 2026-05-22: 图表任务重复派发检测(一个 helper 画全部 + 多个各画一张 → 两套命名混乱)

    _chart_dup = _detect_duplicate_chart_tasks(cleaned)

    if _chart_dup is not None:

        args["_chart_dup_warning"] = _chart_dup
        chart_observation = _guard_observation_from_payload(_chart_dup)
        args.setdefault("_guard_observations", []).append(chart_observation)
        if cleaned:
            cleaned[0].setdefault("guard_observations", []).append(chart_observation)

        debug.log(

            "delegate.duplicate_chart_tasks.detected",

            f"画全部={_chart_dup.get('multi_chart_task')} + "

            f"各画一张={_chart_dup.get('single_chart_tasks')}",

        )

    args["_guard_task_specs"] = [dict(c) for c in cleaned]



    # Explicit code easy/hard backup race:
    # - The runtime no longer synthesizes hard backup helpers from heuristics.
    # - When the LLM explicitly submits `task` and `task_hard`, record the pair
    #   so the first successful helper can stop the loser.

    _twin_map: dict[str, str] = {}

    _explicit_hard_by_base = {
        str(c.get("task_id", "")).rsplit("_hard", 1)[0]: c
        for c in cleaned
        if c.get("kind") in ("code", "coding")
        and c.get("mode") == "hard"
        and str(c.get("task_id", "")).endswith("_hard")
    }

    _paired_primary_tids: list[str] = []

    for c in list(cleaned):
        if c.get("kind") not in ("code", "coding"):
            continue
        if c.get("mode") == "hard" or c.get("resume"):
            continue

        _orig_tid = c["task_id"]
        if _is_legacy_paired_task_id(_orig_tid):
            continue
        _hard_task = _explicit_hard_by_base.get(_orig_tid)
        if not _hard_task:
            continue

        _hard_tid = _hard_task["task_id"]
        c["paired_with"] = _hard_tid
        _hard_task["paired_with"] = _orig_tid
        _twin_map[_orig_tid] = _hard_tid
        _twin_map[_hard_tid] = _orig_tid
        _paired_primary_tids.append(_orig_tid)

    if _paired_primary_tids:
        debug.log(
            "delegate.code_hard_explicit_paired",
            f"recorded explicit easy/hard code race pair(s): {_paired_primary_tids}",
        )
        args["_paired_task_map"] = dict(_twin_map)



    # Blanket resume detection

    _resume_tasks = [c for c in cleaned if c.get("resume")]

    if len(_resume_tasks) >= 3 and not args.get("force_blanket_resume"):

        _all_resume_alive = True

        for c in _resume_tasks:

            try:

                _h = await proc_registry().find_helper_by_task_id(

                    c["task_id"], same_trace_as=debug.current_trace_id() or "",

                )

                if _h is None:

                    _all_resume_alive = False

                    break

            except Exception:

                _all_resume_alive = False

                break

        if _all_resume_alive:

            return json.dumps({

                "ok": False,

                "error": "blanket_resume_blocked",

                "hint": (
                    f"{len(_resume_tasks)} active helpers were requested for resume at the same time. "
                    "Check current helper heartbeats first, let healthy helpers keep running, and resume "
                    "only the helpers that need a new direction. If a broad resume is intentional after "
                    "inspection, set force_blanket_resume=true.\n"
                    "批量续作前先看心跳，只续作确实需要新方向的 helper。"

                ),

            }, ensure_ascii=False)



    return cleaned





from app.llm.tools.delegate_actions import (  # noqa: E402,F401

    _spawn_helpers_only,

    _handle_delegate_spawn_async,

    _handle_delegate_status,

    _peek_all_pending_results,

    _handle_delegate_poll,

    _handle_delegate_collect,

    _handle_delegate_wait_any,

    handle_delegate,

    _dynamic_wait_loop,

    _handle_main_kill_helper,

    handle_spawn_helper,

    handle_wait_helper,

)







# 2026-05-21: spawn 路径不再 mirror 一整套清洗/配对逻辑。
# _sanitize_and_validate_tasks 是唯一清洗入口; spawn handler 直接使用
# `cleaned = cleaned_tasks`，避免双份清洗导致日志双打印和校验不一致。

# ─── spawn_async: 立即返回 proc_ids ───





# ─── status: 全局 dashboard,无需 task_ids ───





# 2026-05-11 P3.x: _peek_all_pending_results 已废弃, 改用模块级 _pending_results 直接读

# 保留空函数避免外部引用爆炸(无人调用即可删除)





# ─── poll: 零阻塞看心跳 ───





# ─── collect: 阻塞拿结果 ───





# ─── wait_any: 等任一 helper done ───













# ═══════════════════════════════════════════════════════════════════

# Kill gate — 2026-05-05 下沉到 app.core.processes.py。

# 所有 helper kill 必须经过 ProcessRegistry.kill() → validate_kill_reason()。

# 导入的常量/函数见文件顶部 from app.core.core_processes import ...。

# ═══════════════════════════════════════════════════════════════════









_MAX_SPAWN_PER_HELPER = 16    # 单个 helper 最多 spawn 16 个兄弟,防"自我增殖"











# ─── Tool schemas ─────────────────────────────────────────



SPAWN_HELPER_TOOL_SCHEMA = {

    "type": "function",

    "function": {

        "name": "spawn_helper",

        "description": (
            "(Legacy, disabled.) Helpers cannot create, wait for, resume, or terminate other helpers. "
            "All helper creation, resource fulfillment, and reactivation are controlled by the main process through "
            "delegate. When a helper lacks a resource, it should call request_resource, freeze, and explain the missing "
            "resource, current evidence, and resume condition.\n\n"
            "历史兼容工具；helper 不能自行管理其它 helper，缺资源时请求主进程协调。"

        ),

        "parameters": {

            "type": "object",

            "properties": {

                "task_id": {

                    "type": "string",

                    "description": "New helper identifier; leave empty for automatic naming.\n\n新 helper 标识符。",

                },

                "prompt": {

                    "type": "string",

                    "description": "Task instructions for the new helper.\n\n给新 helper 的任务说明。",

                },

                "kind": {

                    "type": "string",

                    "enum": list(MODEL_VISIBLE_HELPER_KINDS),

                    "default": "code",

                    "description": (

                        "Helper kind: code, read, edit, verify, draw, tts, or project-analysis kinds. "
                        "Use mode='hard' for difficult retries while preserving the same base kind; do not use kind='final'.\n\n"
                        "helper 类型表示任务本质，难度升级用 hard。"

                    ),

                },

            },

            "required": ["prompt"],

        },

    },

}



WAIT_HELPER_TOOL_SCHEMA = {

    "type": "function",

    "function": {

        "name": "wait_helper",

        "description": (
            "(Legacy, disabled.) Helpers cannot wait for or reactivate other helpers. The main process checks status "
            "through delegate/processes and decides whether to resume, interrupt, reuse resources, or spawn resource helpers.\n\n"
            "历史兼容工具；helper 状态和唤醒由主进程统一管理。"

        ),

        "parameters": {

            "type": "object",

            "properties": {

                "task_id": {

                    "type": "string",

                    "description": "Legacy task ID parameter; this tool no longer waits for helpers.\n\n历史 task_id 参数。",

                },

                "timeout_sec": {

                    "type": "integer",

                    "minimum": 1,

                    "maximum": 300,

                    "description": "Legacy timeout in seconds.\n\n历史等待秒数。",

                },

            },

            "required": ["task_id"],

        },

    },

}





# spawn_helper/wait_helper intentionally stay unexposed: all helper spawning and

# activation is controlled by the main process through delegate().
