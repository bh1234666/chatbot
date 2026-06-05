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

# "这个角色是否同意做这些任务"。守卫否决 → cancel 整棵 trace 的 helper +

# 返回 persona_veto 给主线程,主线程下一轮看到反馈会切到拒绝模式。

#

# 设计原则:

# - 与 helper 同步并行,不阻塞 spawn(用户原话:"避免不拦截时浪费时间")

# - 守卫保守:不明确拒绝 → 默认放行(避免误杀正常请求)

# - 守卫失败 → 默认放行(可用性优先)

_current_persona_excerpt: ContextVar[str] = ContextVar(

    "_current_persona_excerpt", default=""

)

_current_user_message: ContextVar[str] = ContextVar(

    "_current_user_message", default=""

)





def set_current_persona_excerpt(persona: str):

    """orchestrator round2 入口调用,告知 delegate 守卫人设核心。"""

    return _current_persona_excerpt.set((persona or "").strip()[:800])





def set_current_user_message(msg: str):

    """orchestrator round2 入口调用,告知 delegate 守卫用户原 message。"""

    return _current_user_message.set((msg or "")[:600])





def reset_current_persona_excerpt(token):

    if token is not None:

        _current_persona_excerpt.reset(token)





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
        if intent or key_points or deliverables:
            lines = ["Thread task contract:"]
            if intent:
                lines.append("intent=" + intent[:500])
            if key_points:
                lines.append("key_points=" + json.dumps(key_points[:10], ensure_ascii=False)[:900])
            if deliverables:
                lines.append("deliverables=" + json.dumps(deliverables[:10], ensure_ascii=False)[:500])
            parts.append("\n".join(lines))
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
            "suggested_kind must be one of these base kinds; do not suggest final.\n",
            "- Broad project architecture mapping -> project_map; selected-file summaries -> file_summary; change-risk review -> impact_review.\n",
        )
    return (
        "- inventory: environment-only first-pass project inventory, file-type coverage, README/entry/config/test discovery, exact lightweight statistics, and unread source-material notes.\n",
        "suggested_kind must be one of these base kinds, plus environment-only inventory when the task is first-pass project inventory; do not suggest final.\n",
        "- First-pass unfamiliar project inventory -> inventory; deeper architecture mapping -> project_map; selected-file summaries -> file_summary; change-risk review -> impact_review.\n"
        "- Framework/contract/spec/outline tasks that must write `.txt`, `.md`, or `.json` files are artifact-producing. Suggest code when the contract controls runnable project files, benchmark execution, generated datasets, APIs, schemas consumed by code, or implementation interfaces. Suggest edit when the contract controls an article, report, paper, prose chapter plan, literature review structure, document acceptance checklist, or final-document assembly plan.\n"
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

) -> tuple[bool, str, list[dict], list[dict], dict]:

    """LLM quality guard for persona fit, split needs, helper kind, and framework needs.

    Kind reference: reading or extracting user/source materials from files -> read; summarizing selected source/config files in a code project -> file_summary; final Office/PDF delivery -> edit.

    材料读取归 read；工程源码小范围摘要归 file_summary；最终文档装配归 edit。




    返回 (should_act, reason, split_recommendations, kind_recommendations, framework_block)

    - should_act: 是否允许执行(人设否决时 false)

    - reason: 总理由

    - split_recommendations: 拆分建议(P19)

    - kind_recommendations: kind 类型匹配建议(P21 新)

    - framework_block: 同类对比缺统一框架时 {block,task_ids,reason}(2026-05-21 新),否则 {}



    保守原则:不明确拒绝 → True;guard 失败/异常 → True(可用性优先)。

    用 lite 模型,~2-5s 完成。



    2026-05-12 P19+P21 增强:

    - P19: 同时判断任务可拆分性

    - P21: 同时判断 kind 类型是否匹配 (用户洞察: paper_final kind=code 导致 STUCK)

    死循环防御:同一 task_id 被 split/kind block ≥ 2 次后第 3 次放行(沿用 trace 4 次上限)。

    """

    if not tasks:

        return True, "", [], []

    # 任务摘要(每个 prompt 截 400 字 — 比之前 150 多, 让 LLM 看清拆分维度)

    _task_brief_lines = []

    for t in tasks[:5]:

        _tid = t.get("task_id", "?")

        _kind = t.get("kind", "")

        _mode = t.get("mode", "easy")

        _prompt = (t.get("prompt") or "")[:400]

        _eo = t.get("expected_outputs") or []
 
        _task_brief_lines.append(
 
            f"- task_id={_tid} kind={_kind} mode={_mode} expected_outputs={_eo}\n"
 
            f"  prompt first 400 chars: {_prompt}"
 
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


    _env_helper_kind_line, _suggested_kind_line, _project_kind_principle = _task_quality_guard_environment_helper_text()


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
        suggested_kind_line=_suggested_kind_line,
        project_kind_principle=_project_kind_principle,
        existing_block_counts=_existing_block_counts,
        existing_kind_block_counts=_existing_kind_block_counts,
    )
    msgs = [

        {"role": "system", "content": _aux.build_task_quality_guard_system(
            persona="",
            env_helper_kind_line="",
            suggested_kind_line="",
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

        reason = str(result.get("reason", ""))[:200]
        if should_act is False and _guard_false_persona_veto_looks_like_workflow_issue(reason, result, tasks):
            debug.log(
                "delegate.guard_persona_veto.softened_to_workflow",
                (
                    "guard returned should_act=false for a workflow-scoped technical task; "
                    f"reason={reason[:160]}"
                ),
            )
            should_act = True

        # split recommendations (P19)

        split_recs = result.get("split_recommendations") or []

        if not isinstance(split_recs, list):

            split_recs = []

        cleaned_splits = []

        for r in split_recs[:10]:

            if not isinstance(r, dict): continue

            _tid = str(r.get("task_id", "")).strip()

            _ss = bool(r.get("should_split", False))

            if _tid and _ss:

                cleaned_splits.append({

                    "task_id": _tid,

                    "should_split": True,

                    "split_into": [str(x) for x in (r.get("split_into") or [])[:10]],

                    "reason": str(r.get("reason", ""))[:200],

                })

        # kind recommendations (P21 新)

        kind_recs = result.get("kind_recommendations") or []

        if not isinstance(kind_recs, list):

            kind_recs = []

        cleaned_kinds = []

        for r in kind_recs[:10]:

            if not isinstance(r, dict): continue

            _tid = str(r.get("task_id", "")).strip()

            _cur = str(r.get("current_kind", "")).strip().lower()

            _sug = str(r.get("suggested_kind", "")).strip().lower()

            if _tid and _sug and _sug != _cur and _sug in VALID_HELPER_KINDS:

                _rec = {
                    "task_id": _tid,
                    "current_kind": _cur,
                    "suggested_kind": _sug,
                    "reason": str(r.get("reason", ""))[:200],
                }
                _sm = str(r.get("suggested_mode", "")).strip().lower()
                _mr = str(r.get("mode_reason", ""))[:240]
                if _sm:
                    _rec["suggested_mode"] = _sm
                if _mr:
                    _rec["mode_reason"] = _mr
                cleaned_kinds.append(_rec)

        _filtered_kinds = []
        for rec in cleaned_kinds:
            task = next((t for t in tasks if str(t.get("task_id", "")).strip() == rec.get("task_id")), None)
            if task:
                _prompt = str(task.get("prompt") or "")
                _expected = task.get("expected_outputs") or []
                _current = str(rec.get("current_kind") or "").strip().lower()
                _suggested = str(rec.get("suggested_kind") or "").strip().lower()
                # 2026-06-04 P133: 不再代 LLM 修正 "read→user-facing-text" / "code→prose" 等模糊判断;
                # guard LLM 已自行做出建议, 启发式覆盖会引入冲突循环。物理硬约束仍由 _deterministic_kind_recommendations 兜底。
                if (
                    _current == "code"
                    and _suggested == "edit"
                    and _is_code_project_companion_output(_prompt, _expected)
                ):
                    debug.log(
                        "delegate.guard_kind.ignored_code_companion_to_edit",
                        (
                            f"task '{rec.get('task_id')}' kind recommendation ignored: "
                            "code-project companion artifacts remain with code helper"
                        ),
                    )
                    continue
            _filtered_kinds.append(rec)
        cleaned_kinds = _filtered_kinds

        # 单一 Office 产物由 edit helper 收敛构建，不能被拆分守卫打断成多个文本草稿。

        _filtered_splits = []

        for rec in cleaned_splits:

            task = next((t for t in tasks if str(t.get("task_id", "")).strip() == rec.get("task_id")), None)

            if task:
                if (
                    _split_recommendation_is_source_read(rec)
                    and not _task_has_concrete_source_material_inputs(task)
                ):
                    debug.log(
                        "delegate.guard_split.ignored_source_read_without_inputs",
                        (
                            f"task '{rec.get('task_id')}' source-read split ignored: "
                            "no concrete source files, material batches, or directory-material scope"
                        ),
                    )
                    continue

                _kind = str(task.get("kind", "")).strip().lower()

                _expected = task.get("expected_outputs") or []

                if _kind == "edit" and len(_expected) == 1 and _has_office_document_output(str(task.get("prompt") or ""), _expected):

                    debug.log(

                        "delegate.guard_split.ignored_single_office",

                        f"task '{rec.get('task_id')}' split recommendation ignored: single Office output handled by edit helper",

                    )

                    continue

                # 2026-05-15 P63: scaffold/framework 任务豁免 — 接口耦合, 拆开后兄弟集成必崩

                # 实测教训(05-15 16:26 comp_framework): 守卫 LLM 把 7-file scaffold 拆成 6 个

                # 子任务, helper 被 killed, 主线程立即改派 comp_infra 单 helper 走通 → P19 此处负优化。

                _scaffold_signal = _is_scaffold_task(

                    str(task.get("prompt") or ""), task.get("expected_outputs") or []

                )

                if _scaffold_signal:

                    _kind = str(task.get("kind", "")).strip().lower()

                    _split_targets = [str(x).strip() for x in (rec.get("split_into") or []) if str(x).strip()]

                    if _kind not in ("code", "coding") or len(_split_targets) < 2:

                        debug.log(

                            "delegate.guard_split.ignored_scaffold",

                            f"task '{rec.get('task_id')}' split recommendation ignored: "

                            f"scaffold/framework task (signals={_scaffold_signal})",

                        )

                        continue

                    debug.log(

                        "delegate.guard_split.scaffold_softened",

                        f"task '{rec.get('task_id')}' is scaffold-like but code split recommendation is kept: "

                        f"signals={_scaffold_signal}, split_into={_split_targets[:8]}",

                    )

            _filtered_splits.append(rec)

        cleaned_splits = _filtered_splits

        # 2026-05-21: 解析 framework_block(同类对比缺统一框架)

        _fb_raw = result.get("framework_block") or {}

        _framework_block = {}

        if isinstance(_fb_raw, dict) and bool(_fb_raw.get("block")):

            _fb_tids = [str(x).strip() for x in (_fb_raw.get("task_ids") or []) if str(x).strip()]

            if _fb_tids:
                _framework_counter_key = _framework_block_counter_key(_trace_id_for_guard, _fb_tids)
                _trace_framework_blocks = _guard_framework_block_trace_total.get(_framework_counter_key, 0)
                _embedded_contract = _has_embedded_peer_framework_contract(tasks, _fb_tids)
                if _trace_framework_blocks >= 1 or _embedded_contract:
                    debug.log(
                        "delegate.guard_framework.ignored_redundant_block",
                        (
                            f"ignored framework_block for tasks={_fb_tids[:8]} "
                            f"counter_key={_framework_counter_key} "
                            f"trace_blocks={_trace_framework_blocks} embedded_contract={_embedded_contract}"
                        ),
                    )
                else:
                    _framework_block = {

                        "block": True,

                        "task_ids": _fb_tids[:20],

                        "reason": str(_fb_raw.get("reason", ""))[:200],

                    }

        if _framework_block:

            return should_act, reason, cleaned_splits, cleaned_kinds, _framework_block

        return should_act, reason, cleaned_splits, cleaned_kinds

    except Exception as e:

        # 守卫失败 → 放行(保守:不耽误正常请求)

        return True, f"guard_error: {e}", [], []


def _guard_false_persona_veto_looks_like_workflow_issue(
    reason: str,
    result: dict,
    tasks: list[dict],
) -> bool:
    """Return True when should_act=false is really split/kind/framework guidance.

    Persona veto is reserved for role/safety refusal. If the same guard result
    contains actionable workflow guidance for concrete technical/document work,
    keep that guidance but allow the main thread to re-plan from it.

    人设否决只用于明确角色或安全拒绝；技术任务的拆分、类型、框架问题不应变成 persona_veto。
    """
    reason_l = (reason or "").lower()
    workflow_words = (
        "no user request",
        "detached",
        "standalone",
        "split",
        "framework",
        "contract",
        "kind",
        "not serving",
    )
    has_workflow_reason = any(w in reason_l for w in workflow_words)
    has_actionable_guidance = bool(result.get("split_recommendations") or result.get("kind_recommendations"))
    fb = result.get("framework_block") or {}
    if isinstance(fb, dict) and fb.get("block"):
        has_actionable_guidance = True
    if not (has_workflow_reason and has_actionable_guidance):
        return False
    task_text = json.dumps(tasks or [], ensure_ascii=False).lower()
    concrete_markers = (
        "docx",
        "paper",
        "论文",
        "报告",
        "benchmark",
        "算法",
        "tree",
        "b+",
        "skiplist",
        "code",
        "read",
        "office",
        "pdf",
        "csv",
        "json",
        "framework",
        "contract",
        "analysis",
        "document",
    )
    return any(marker in task_text for marker in concrete_markers)


def _deterministic_kind_recommendations(tasks: list[dict]) -> list[dict]:
    """Return clear artifact-to-helper mismatches before any helper starts.

    This is a guard signal, not an auto-correction: the main thread still sees
    task_kind_mismatch and must re-delegate the same logical work with the
    matching helper kind.

    明确产物类型和 helper 工具族不匹配时返回结构化建议；不静默改写任务。

    2026-06-04 P133: 限制为**物理硬约束**——只在 kind 完全无法产出预期产物时
    建议（read/draw/tts 想产 docx/pptx/xlsx 等可执行/二进制；code 想产 docx
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
        if not tid or not expected_outputs:
            continue

        # Skip deprecated kinds - they should be rejected upstream, not recommended
        if kind in {"general", "final"}:
            continue

        suggested = ""
        reason = ""
        has_non_text_impl = _has_non_text_implementation_output(expected_outputs)

        # Hard physical constraint: edit helper owns Office/PDF document assembly.
        if _has_office_document_output(prompt, expected_outputs) and kind != "edit":
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
                "suggested_kind": suggested,
                "reason": reason,
            }
            recommendations.append(rec)
    return recommendations


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
        r"(?m)^\s*(?:\d+[.)、]|[-*])\s+[^:\n]{1,80}(?:组|folder|batch|group|目录|/)\s*[:：]?",
        p,
        re.I,
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
    text = (
        str(rec.get("reason") or "") + " " + " ".join(str(x) for x in (rec.get("split_into") or []))
    ).lower()
    return any(marker in text for marker in ("source material", "source-material", "read_sources", "read helper", "read helpers"))


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
            "should_split": True,
            "split_into": [f"read_sources_batch_{i}" for i in range(1, batch_count + 1)],
            "reason": (
                f"{source_count} source material items/groups should be read by parallel read helpers "
                "before downstream code/edit synthesis consumes evidence."
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



# 2026-05-09 Patch 40: historical paired hard helper design notes

# 旧设计曾尝试在长跑 code helper 旁路派高资源 paired helper,后来删除自动触发。

# 现在主线程可见协议只推荐 base kind + mode;历史 auto_final 参数仅兼容旧调用。

# 2026-05-10 Patch 60: 已删除 _HELPER_AUTO_FINAL_THRESHOLD 常量(P40 自动派 paired helper 删除)



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

    expected_outputs: list[str] | None,

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

        expected_outputs=expected_outputs or [],

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

                    f"建议:先派 1 个 infra helper 建统一框架(高精度计时器+统一内存口径+统一 CSV schema),"

                    f"各算法 helper 引用它,而非各自实现 benchmark。"

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
    deterministic hard-block payload.
    """
    return None

def _is_single_compact_framework_contract_task_for_guard(task: dict) -> bool:
    """Return True for a compact framework/spec helper that should not be split."""
    try:
        from app.llm.tools.delegate_framework import is_compact_framework_contract_task
        return is_compact_framework_contract_task(task)
    except Exception:
        return False


def _is_scoped_framework_fanout_task_for_guard(
    task: dict,
    *,
    total_task_count: int,
    outputs: list[str],
    prompt_l: str,
    enum_signals: list[str],
    comparison_pipeline: bool,
) -> bool:
    """Allow a bounded component helper inside an already split fanout batch."""
    if total_task_count < 2:
        return False
    if not str(task.get("framework") or "").strip():
        return False
    if comparison_pipeline or enum_signals:
        return False
    if not outputs or len(outputs) > 8:
        return False
    checks = task.get("acceptance_checks") or []
    if not checks:
        return False
    if len(prompt_l) > 2400:
        return False
    normalized = [output.replace("\\", "/").lstrip("./").strip() for output in outputs]
    if not all(path == "_env" or path.startswith("_env/") for path in normalized):
        return False
    lowered = " ".join(normalized).lower()
    if any(
        marker in prompt_l
        for marker in (
            "benchmark all",
            "final paper",
            "generate final docx",
            "assemble final report",
            "implement all algorithms",
            "compare all algorithms",
        )
    ):
        return False
    component_groups = (
        ("core", "_env/core/", "_env/run.py", "_env/requirements.txt"),
        ("ui", "_env/ui/"),
        ("native", "_env/native/"),
        ("test", "_env/tests/", "_env/fixtures/"),
        ("fixture", "_env/tests/", "_env/fixtures/"),
        ("doc", "_env/docs/", "_env/readme.md"),
        ("script", "_env/scripts/"),
    )
    task_id_l = str(task.get("task_id") or "").lower()
    for group in component_groups:
        name, *prefixes = group
        if name not in task_id_l:
            continue
        if all(any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in prefixes) for path in normalized):
            return True
    first_dirs = {
        parts[1]
        for path in normalized
        if (parts := path.split("/")) and len(parts) >= 3 and parts[0] == "_env"
    }
    if len(first_dirs) == 1:
        return True
    return bool(lowered and len(normalized) <= 5 and any(k in task_id_l for k in ("readme", "docs", "script", "fixture")))


def _is_bounded_framework_scaffold_task_for_guard(
    task: dict,
    *,
    outputs: list[str],
    prompt_l: str,
    enum_signals: list[str],
    comparison_pipeline: bool,
) -> bool:
    """Allow one compact technical scaffold that defines shared project interfaces."""
    if not str(task.get("framework") or "").strip():
        return False
    if comparison_pipeline or enum_signals:
        return False
    if not outputs or len(outputs) > 8:
        return False
    if not (task.get("acceptance_checks") or []):
        return False
    task_id_l = str(task.get("task_id") or "").lower()
    role_text = f"{task_id_l} {prompt_l}"
    if not any(marker in role_text for marker in ("framework", "scaffold", "infra", "contract", "harness", "spec")):
        return False
    if any(marker in prompt_l for marker in ("implement all algorithms", "benchmark all", "final paper", "generate final docx")):
        return False
    normalized = [output.replace("\\", "/").lstrip("./").strip() for output in outputs]
    if not all(path == "_env" or path.startswith("_env/") for path in normalized):
        return False
    return True


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
        if _is_single_compact_framework_contract_task_for_guard(task):
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



        scoped_framework_fanout = _is_scoped_framework_fanout_task_for_guard(
            task,
            total_task_count=total_task_count,
            outputs=outputs,
            prompt_l=prompt_l,
            enum_signals=enum_signals,
            comparison_pipeline=comparison_pipeline,
        )
        bounded_framework_scaffold = _is_bounded_framework_scaffold_task_for_guard(
            task,
            outputs=outputs,
            prompt_l=prompt_l,
            enum_signals=enum_signals,
            comparison_pipeline=comparison_pipeline,
        )

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


def _should_pair_code_hard_backup(task: dict, *, auto_final: bool, environment_mode: bool) -> bool:

    """Decide whether an easy code task deserves a hard paired race backup.

    Keep ordinary small code tasks single. Use easy/hard pairing when the task
    has enough independent project breadth that a race backup is likely to
    improve quality rather than duplicate a small implementation.

    普通小任务单跑；项目级、多文件、长链路任务才自动配 hard 竞速备份。
    """

    if auto_final:

        return True

    if not environment_mode:

        return False

    if task.get("kind") not in ("code", "coding"):

        return False

    if task.get("mode") == "hard" or task.get("resume"):

        return False

    prompt = str(task.get("prompt") or "").replace("\\", "/")

    expected_outputs = [

        str(output).replace("\\", "/").lstrip("./").strip()

        for output in (task.get("expected_outputs") or [])

        if str(output).strip()

    ]

    task_id_l = str(task.get("task_id") or "").lower()
    prompt_l = prompt.lower()
    if any(marker in f"{task_id_l} {prompt_l}" for marker in (
        "framework", "contract", "benchmark_spec", "schema", "outline", "inventory",
        "框架", "契约", "规格", "大纲", "清单",
    )):
        implementation_outputs = [
            path for path in expected_outputs
            if re.search(r"\.(py|js|ts|tsx|jsx|c|cc|cpp|h|hpp|rs|go|java)$", path, re.I)
        ]
        if len(implementation_outputs) <= 2:
            return False

    env_scoped = (

        "_env/" in prompt

        or (

            bool(expected_outputs)

            and len(expected_outputs) <= 40

            and all(path == "_env" or path.startswith("_env/") for path in expected_outputs)

        )

    )

    if not env_scoped:

        return False

    broad_signals = 0
    strong_breadth = False

    if len(expected_outputs) >= 5:

        strong_breadth = True
        broad_signals += 2

    env_paths = set(re.findall(r"_env/[A-Za-z0-9_./\\-]+", prompt))

    env_source_paths = {
        path for path in env_paths
        if re.search(r"\.(py|js|ts|tsx|jsx|c|cc|cpp|h|hpp|rs|go|java|kt|cs|php|rb|swift)$", path)
    }

    non_doc_expected = [
        path for path in expected_outputs
        if not re.search(r"(^|/)(readme|docs?)(/|$)|\.(md|txt|rst)$", path, re.I)
    ]

    if len(env_paths) >= 5:

        broad_signals += 1

    if len(env_source_paths) >= 3 or len(non_doc_expected) >= 4:

        broad_signals += 1

    if len(re.findall(r"^\s*\d+[.)]\s+\S{2,40}", prompt, re.MULTILINE)) >= 4:

        broad_signals += 1

    if len(prompt) >= 900:

        broad_signals += 1

    named_algorithms = {
        item.lower()
        for item in re.findall(
            r"\b(Dijkstra|A\*|Floyd[- ]Warshall|Bellman[- ]Ford|Huffman|LZ77|LZW|BWT|RLE)\b",
            prompt,
            re.I,
        )
    }
    counted_work_units = 0
    for match in re.finditer(
        r"\b(\d+)\s+(?:algorithms|modules|experiments|benchmarks|subsystems)\b",
        prompt,
        re.I,
    ):
        try:
            counted_work_units = max(counted_work_units, int(match.group(1)))
        except ValueError:
            pass
    independent_work_units = max(len(named_algorithms), counted_work_units)

    if independent_work_units >= 3:

        broad_signals += 1

    project_words = (

        "project", "工程", "multi-file", "多文件", "scaffold", "架构",

        "refactor", "重构", "migration", "迁移", "文档", "README",

        "package", "module set", "subsystems", "子系统",

    )

    if any(word in prompt for word in project_words):

        broad_signals += 1

    # A long prompt that edits one source file plus tests/docs is usually still
    # a single implementation loop. Pair only when there is independent breadth.
    has_independent_breadth = (
        strong_breadth
        or len(env_source_paths) >= 3
        or len(non_doc_expected) >= 4
        or independent_work_units >= 3
    )

    return has_independent_breadth and broad_signals >= 3





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


# 2026-06-04 P131: dispatch-time guard for read helpers — patterns defined earlier
# (see _deterministic_source_read_split_recommendations exemption).


def _detect_helper_produced_inputs(prompt: str, input_files: list, expected_outputs: list) -> list[str]:
    """Return basenames of inputs that look like helper-produced artifacts.

    Examines explicit input_files plus path-like tokens in the prompt body. Excludes
    expected_outputs (the helper is supposed to produce those).
    """
    expected_set = {str(e or "").strip().lower() for e in (expected_outputs or [])}
    candidates: list[str] = []
    for f in input_files or []:
        s = str(f or "").strip()
        if s and s.lower() not in expected_set:
            candidates.append(s)
    # extract path-like tokens from prompt
    for m in _re_p131.finditer(r"[A-Za-z0-9_\-./]+\.(?:md|markdown|txt|csv)", str(prompt or "")):
        tok = m.group(0)
        if tok.lower() not in expected_set and tok not in candidates:
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
        try:
            from app.llm.tools.delegate_framework import normalize_framework_contract
            framework = normalize_framework_contract(t.get("framework"))
        except Exception:
            framework = str(t.get("framework") or "").strip()[:1800]

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
        _raw_write_scopes = t.get("write_scopes") or t.get("output_scopes") or t.get("ownership_scopes")
        if isinstance(_raw_write_scopes, str):
            write_scopes = [x.strip(" -\t") for x in _raw_write_scopes.splitlines() if x.strip(" -\t")][:20]
        elif isinstance(_raw_write_scopes, (list, tuple, set)):
            write_scopes = [str(x).strip() for x in _raw_write_scopes if str(x).strip()][:20]
        elif isinstance(_raw_write_scopes, dict):
            write_scopes = [f"{k}: {v}" for k, v in _raw_write_scopes.items() if str(k).strip()][:20]
        else:
            write_scopes = []

        if kind in {"project_map", "file_summary", "impact_review", "inventory", "summarize"} and expected_outputs:
            debug.log(
                "delegate.expected_outputs.readonly_cleared",
                (
                    f"task '{raw_tid or f'task{idx}'}' kind={kind!r} is read-only; "
                    f"clearing expected_outputs={expected_outputs}"
                ),
            )
            expected_outputs = []

        # 2026-05-12 P17: 主线程没传 expected_outputs 时, 系统自动从 prompt 推断

        # 病因(实测 23:46 trace): 主线程 23 个 helper 全无 expected_outputs,

        # → Tier 1.C 验收 + quality_warnings + P14.G/K + workflow_incomplete 全失效。

        # 修法: prompt 含明显的"产出动词 + 文件名"模式时, 系统自动提取作为默认值。

        # 主线程仍可显式覆盖(自己传 expected_outputs 优先)。

        if not expected_outputs and kind not in {"ocr", "project_map", "file_summary", "impact_review", "inventory", "summarize"}:

            expected_outputs = _infer_expected_outputs_from_prompt(prompt)

            if expected_outputs:

                debug.log(

                    "delegate.expected_outputs.auto_inferred",

                    f"task '{raw_tid}' 主线程没传 expected_outputs, "

                    f"系统从 prompt 推断: {expected_outputs}",

                )

        if kind == "read" and expected_outputs:
            _read_expected_before = list(expected_outputs)
            expected_outputs = _filter_read_helper_expected_outputs(prompt, expected_outputs)
            if expected_outputs != _read_expected_before:
                debug.log(
                    "delegate.expected_outputs.read_evidence_filtered",
                    (
                        f"task '{raw_tid}' kind='read' keeps only internal txt evidence outputs; "
                        f"before={_read_expected_before}, after={expected_outputs}"
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

        # 2026-06-04 P131: dispatch-time guard — read helper must not target helper-produced artifacts.
        # The producer of those artifacts already owns a report. If content seems missing, resume the
        # producer with a focused follow-up rather than spawning a read helper to read its output.
        if kind in ("read", "ocr") and not resume:
            offending = _detect_helper_produced_inputs(prompt, input_files, expected_outputs)
            if offending:
                shown = ", ".join(offending[:6])
                more = f", ... total {len(offending)}" if len(offending) > 6 else ""
                return json.dumps({
                    "ok": False,
                    "error": "read_helper_targets_helper_produced_artifacts",
                    "blocked_inputs": offending[:12],
                    "hint": (
                        f"task '{tid}' uses kind='read' but its inputs include helper-produced artifacts "
                        f"({shown}{more}). Read helpers are for user-provided source material (uploaded "
                        f"documents, project files, scans, PDFs, audio, large reference data), not for "
                        f"re-reading what an earlier helper just wrote. The producer of those artifacts "
                        f"already returned a report.\n"
                        f"Correct routing:\n"
                        f"  1) If you need their content for the next deliverable (e.g. assembling a docx "
                        f"from analyses+csv), pass them as inputs to the consumer helper directly — usually "
                        f"`kind='edit'` for documents, `kind='code'` for further computation. The consumer "
                        f"reads them itself.\n"
                        f"  2) If a producer's short report is too thin and you genuinely need more detail, "
                        f"resume the producer with `resume=true` and a focused follow-up prompt asking it to "
                        f"expand its report or produce a `*_evidence.txt`. Do not spawn a read helper for the "
                        f"same artifact.\n"
                        f"  3) The exception is an explicit AUDIT/QA of a helper's output — in that case, use "
                        f"`kind='verify'` (read-only review), not `kind='read'`.\n"
                        f"read helper 不读 helper 已产出的产物：要消费就直接交给 edit/code 等消费 helper；要更多内容就 resume 生产者扩报告；要审核就用 verify。"
                    ),
                }, ensure_ascii=False)

        cleaned.append({

            "task_id": tid, "prompt": prompt,

            "resume": resume, "fork_from": fork_from,

            "kind": kind,

            "mode": mode,

            "expected_outputs": expected_outputs,  # 1.C 新增

            "write_scopes": write_scopes,

            "framework": framework,

            "input_files": input_files,

            "acceptance_checks": acceptance_checks,

        })



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



    # 2026-05-21: 确定性框架兜底(不依赖守卫 LLM / 提示词部署)。
    # 2026-05-27: 从“spawn 后警告”改为“spawn 前返回 framework_first_required”。
    # 实测直接压测中,主线程先拉起 sort_quick/sort_merge/sort_heap 三个 helper,
    # 随后才收到 framework_first_required,已经浪费了 helper 启动和早期推理成本。
    # 这里在任何 helper 创建前返回结构化修正建议,让主线程先建统一框架。

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



    # Code hard backup race:
    # - auto_final keeps the historical hard twin as a final-quality safeguard.
    # - isolated environment project tasks may also get an easy/hard race when
    #   they are broad enough to benefit from a backup.
    # - ordinary small code helpers stay single to avoid waste.

    _twin_map: dict[str, str] = {}

    _auto_final = args.get("auto_final", False)

    from app.core.runtime_mode import is_environment_mode as _is_environment_mode

    _environment_mode_for_pairing = bool(_is_environment_mode())

    _should_pair_code_hard = bool(_auto_final) or any(
        _should_pair_code_hard_backup(
            c,
            auto_final=False,
            environment_mode=_environment_mode_for_pairing,
        )
        for c in cleaned
    )

    if _should_pair_code_hard:

        _base_task_count = len(cleaned)

        _available_twin_slots = max(0, _MAX_DELEGATE_TASKS_PER_CALL - _base_task_count)

        _existing_tids = {c["task_id"] for c in cleaned}

        _twins: list[dict] = []

        _paired_primary_tids: list[str] = []

        _skipped_pair_primary_tids: list[str] = []

        _explicit_hard_by_base = {
            str(c.get("task_id", "")).rsplit("_hard", 1)[0]: c
            for c in cleaned
            if c.get("kind") in ("code", "coding")
            and c.get("mode") == "hard"
            and str(c.get("task_id", "")).endswith("_hard")
        }

        for c in list(cleaned):

            if c.get("kind") not in ("code", "coding"):

                continue

            if c.get("mode") == "hard" or c.get("resume"):

                continue

            if not _should_pair_code_hard_backup(
                c,
                auto_final=bool(_auto_final),
                environment_mode=_environment_mode_for_pairing,
            ):

                continue

            if _is_legacy_paired_hard_task(c):

                continue

            _orig_tid = c["task_id"]

            if _is_legacy_paired_task_id(_orig_tid):

                continue

            if _orig_tid in _explicit_hard_by_base:

                _hard_task = _explicit_hard_by_base[_orig_tid]

                _hard_tid = _hard_task["task_id"]

                c["paired_with"] = _hard_tid

                _hard_task["paired_with"] = _orig_tid

                _twin_map[_orig_tid] = _hard_tid

                _twin_map[_hard_tid] = _orig_tid

                continue

            if len(_twins) >= _available_twin_slots:

                _skipped_pair_primary_tids.append(_orig_tid)

                continue

            _twin_tid = f"{_orig_tid}_hard"

            _suffix_n = 2

            while _twin_tid in _existing_tids:

                _twin_tid = f"{_orig_tid}_hard_{_suffix_n}"

                _suffix_n += 1

            _existing_tids.add(_twin_tid)

            _twins.append({

                "task_id": _twin_tid,

                "prompt": c["prompt"],

                "resume": False,

                "fork_from": "",

                "kind": "code",

                "mode": "hard",

                "expected_outputs": list(c.get("expected_outputs") or []),

                "framework": c.get("framework", ""),

                "input_files": list(c.get("input_files") or []),

                "acceptance_checks": list(c.get("acceptance_checks") or []),

                "paired_with": _orig_tid,

            })

            c["paired_with"] = _twin_tid

            _twin_map[_orig_tid] = _twin_tid

            _twin_map[_twin_tid] = _orig_tid

            _paired_primary_tids.append(_orig_tid)

        if _twins:

            cleaned.extend(_twins)

            if len(cleaned) > _MAX_DELEGATE_TASKS_PER_CALL:
                overflow = len(cleaned) - _MAX_DELEGATE_TASKS_PER_CALL
                removed = cleaned[-overflow:]
                cleaned = cleaned[:-overflow]
                removed_tids = [str(t.get("task_id", "")) for t in removed]
                removed_set = set(removed_tids)
                _paired_primary_tids = [
                    tid for tid in _paired_primary_tids
                    if _twin_map.get(tid) not in removed_set
                ]
                for tid in list(_twin_map):
                    if tid in removed_set or _twin_map.get(tid) in removed_set:
                        _twin_map.pop(tid, None)
                for c2 in cleaned:
                    if c2.get("paired_with") in removed_set:
                        c2.pop("paired_with", None)
                debug.log(
                    "delegate.code_hard_pairing_trimmed",
                    "trimmed surplus generated hard backup helper(s) to preserve the primary batch: "
                    f"{removed_tids}",
                )

            debug.log(

                "delegate.code_hard_paired",

                f"paired {len(_twins)} hard code backup helper(s) with primaries: "

                f"{_paired_primary_tids}",

            )

            if _auto_final:

                debug.log(

                    "delegate.auto_final_paired",

                    f"paired {len(_twins)} hard helper(s) with primaries: "

                    f"{_paired_primary_tids}",

                )

            args["_paired_task_map"] = dict(_twin_map)

        if _skipped_pair_primary_tids:

            debug.log(

                "delegate.code_hard_pairing_capped",

                f"skipped hard backup helper(s) for primaries due to delegate limit: "

                f"{_skipped_pair_primary_tids}",

            )



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

