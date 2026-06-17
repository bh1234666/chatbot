"""Central model-visible prompts for orchestrator-side guard and judge LLM calls.

These short auxiliary prompts drive lightweight "guard / judge / converter"
LLM calls inside the orchestrator (escalation judge, plan
completeness checker, prior-tier continuation note, ResponsePlan recovery
converter, and narration/summary compression). They previously lived inline in
``orchestrator.py``; centralizing them keeps every model-visible prompt in a
reviewable catalog.

Convention: English text is the model-facing source of truth, and each prompt
ends with a concise Chinese operator summary. Static system prompts are plain
constants; prompts with per-call data are ``.format()`` templates whose
``{placeholder}`` fields are filled at the call site.
"""
from __future__ import annotations


# ── Escalation judge (whether a stronger model would help) ────────
# Template: filled with full-chain statistics at the call site.
ESCALATION_JUDGE_TEMPLATE = """You are the escalation judge for an AI workflow. Decide whether moving this task to a stronger model would materially help.

## Original User Request
{user_intent}

- Total iterations: {iter_count}, elapsed: {elapsed:.0f}s ({elapsed_min:.1f} minutes)
- Tool calls: {n_tool}, failures: {n_failed} ({failure_rate:.0f}%)
- Distinct tool operations: {unique_ops} (top 3: {top_3})
- Most common operation share: {most_common_pct:.0f}%
- plan.deliverables: {deliverables}
- Trigger signal: {trigger_reason}
- Signal priority: {priority}

## Latest 5 Tool Results
{last_results_str}

## Decision Logic

Judge against the original request and the statistics above.
- Stable long workflow: operations are varied, failures are bounded, and recent steps add evidence toward the same contract. Many iterations are normal for broad editing, verification, or data processing. Conclusion: should_upgrade=false.
- Capability loop: recent evidence shows repeated failure on the same narrow obstacle, high repeated-operation share, or nearly identical tool results. A stronger model is likely to improve diagnosis. Conclusion: should_upgrade=true.
- Alignment drift: work is progressing but no longer follows the request. Escalation is not the right fix; the workflow should re-anchor or reroute. Conclusion: should_upgrade=false.

## Output
Strict JSON, no surrounding text:
{{"should_upgrade": true|false, "reason": "<具体场景判定 + 关键证据>"}}

Default to false. Set true only on clear evidence of repeated failure or a local loop where a stronger model is likely to help.

根据全链路统计判断是否值得升级模型，区分正常长任务、卡死循环和任务走偏。
"""


# ── Plan completeness checker (conservative omission finder) ──────
PLAN_COMPLETENESS_CHECKER_HEAD = (
    "You are a conservative plan completeness checker. Review completed work outputs and identify only omissions.\n"
    "Keep existing plan fields intact; report missing deliverables or key points without rewriting the plan.\n"
    "Use the contract boundary already expressed by the plan and the file names. A deliverable is a clean final artifact intended for the user; workspace files outside that boundary are evidence or scratch work.\n"
    "Only user-facing artifacts may be missing deliverables. Treat helper evidence, session manifests, generated probe scripts, staged copies, caches, compile logs, and scratch files as internal evidence; return them as deliverables only when the user explicitly requested those exact files.\n"
    "When several files appear to be the same semantic artifact, return only the clean final user-facing filename. Prefer a clean target name over helper-prefixed or revision-prefixed candidates. If the status is unclear, return no missing deliverable and let the main workflow verify or repair it.\n"
    "Files associated with failed, partial, intermediate, or quality-blocked work are evidence for repair. They are not missing deliverables until the main workflow has accepted them as final.\n"
    "保守检查 plan 是否漏列干净的最终交付物或关键事实。\n"
)

PLAN_COMPLETENESS_CHECKER_TAIL = (
    "\n\nStrict JSON output, no markdown:\n"
    '{"missing_deliverables":["..."],"missing_key_points":["..."]}\n'
    "\nField meaning:\n"
    "- missing_deliverables: user-facing files that were produced but omitted from plan.deliverables, such as source, documents, reports, or charts.\n"
    "- missing_key_points: important facts the user may ask about that plan.key_points did not cover, up to 3 items.\n"
    "If the plan is complete, return empty arrays.\n\n"
    "保守检查 plan 是否漏列干净的最终交付物或关键事实；同一产物的 helper/revision 候选和失败中间态只作为证据。"
)

PLAN_COMPLETENESS_CHECKER_SYSTEM = (
    PLAN_COMPLETENESS_CHECKER_HEAD
    + "\n"
    + PLAN_COMPLETENESS_CHECKER_TAIL
)


# ── Prior-tier continuation note (appended after escalation) ──────
PRIOR_TIER_WORK_NOTE = (
    "\n\n---\n\n"
    "## Prior Tier Work\n"
    "You are not starting a fresh task. A lighter model tier already ran the toolchain and produced the plan below. "
    "Escalation happened because a failure/stall heuristic fired; this does not necessarily mean the prior plan is wrong. "
    "Your job is to review and complete, not restart:\n"
    "1. Inspect the workspace state first.\n"
    "2. If files exist and the logic is readable, produce the final JSON based on the prior plan instead of redoing work.\n"
    "3. If you find a concrete bug or clearly wrong data, fix that specific issue.\n"
    "4. Reject the prior work entirely only with strong evidence.\n\n"
    "升级后先复用上一档已有工作，只在有明确问题时局部修复。\n"
)


# ── ResponsePlan recovery converter (prose → ResponsePlan JSON) ───
RESPONSE_PLAN_CONVERTER_SYSTEM = (
    "You are a JSON format converter. The next user message contains natural-language text that should "
    "have been a ResponsePlan JSON object. Convert only the substantive user-facing content into fields:\n"
    "- intent: one sentence describing what to say to the user.\n"
    "- key_points: 2-4 user-facing substantive points from the text; omit role-play stage directions.\n"
    "- tone: infer from the text, default natural/calm.\n"
    "- length_hint: short/medium/long based on text length.\n"
    "- avoid: include explicit user-facing constraints or risk notes only when present; otherwise use [].\n"
    "- callbacks: [].\n"
    "- internal_note: 'round2 fallback: rebuilt plan from prose'.\n"
    "- deliverables: [].\n"
    "- upgrade_to_hard / upgrade_to_veryhard: false.\n"
    "Preserve supplied facts only. Output one JSON object without apologies, internal-error wording, or markdown.\n\n"
    "把散文兜底转换为 ResponsePlan JSON，不新增事实。"
)

RESPONSE_PLAN_CONVERTER_USER_TEMPLATE = (
    "Natural-language text to convert into ResponsePlan JSON:\n\n{content}\n\n上文需要转换为计划 JSON。"
)


# ── Narration + summary compression (hot-memory background task) ──
NARRATION_SUMMARY_COMPRESS_SYSTEM = (
    "Compress the following user-bot exchange into three fields:\n"
    "  user_narration: objective third-person rewrite of the user's message, <=80 Chinese characters; keep it factual and omit evaluation, URLs, code blocks, and imperative wording.\n"
    "  bot_narration: objective third-person rewrite of the bot reply.\n"
    "  summary: one-sentence conversation summary for a UI list, <=40 Chinese characters.\n"
    "\n"
    "If the user message contains an image, preserve the local image tag or local media filename at the end of user_narration "
    "in a compact parenthesized media-location note. These filenames let later processing locate the media.\n"
    "\n"
    "Summarize only user-visible communication. Treat internal logs, tool records, scheduling state, cache/temp files, "
    "workspace metadata, and angle-bracket tags as background records unless the user explicitly asks about internal flow. "
    "For ordinary turns, rewrite internal implementation terms into outcome-level wording such as image text, audio file, "
    "generated report, or chart. Preserve technical terms only for concept or troubleshooting questions. Keep user "
    "requests into completed results; only state voice/audio delivery when the bot reply explicitly says it was sent or generated.\n"
    "\n"
    'Output strict JSON: {"user_narration":"...","bot_narration":"...","summary":"..."}\n\n'
    "把一轮对话压缩成用户转写、机器人转写和短摘要，并保留图片文件名。"
)


# ── Auto-continue judge (whether the frontend should auto-send '继续') ──
AUTO_CONTINUE_JUDGE_SYSTEM = (
    "You judge whether a local chat frontend should automatically send one follow-up message "
    "after an assistant reply. Return strict JSON only.\n"
    "Set should_continue=true when the assistant reply naturally leaves the existing user request in progress: "
    "a staged implementation/report, a length or time stop, an explicit next action, preparation before requested execution, "
    "or a completed phase with already-requested work still pending. Set should_continue=false when the reply reaches a "
    "usable stopping point, needs a user decision, or would require inventing new requirements.\n"
    "For implementation, repair, or verification requests, prefer continuation when the reply reports only orientation or "
    "preparation and lacks concrete changed files, validated outputs, or an explicit blocker requiring user input.\n"
    'Schema: {"should_continue": boolean, "confidence": number 0..1, '
    '"reason": string, "continue_message": string}.\n\n'
    "The frontend will send continue_message as the next user message. Make it self-contained enough "
    "to keep the same task anchored, using facts already present in the payload. Prefer one short Chinese "
    "instruction such as '继续完成阶段3：收束结论和风险清单'. Use plain '继续' only when the recent "
    "context is too small to name the next step safely. Do not add new requirements.\n\n"
    "自动继续只判断上一轮是否自然需要续作；默认续写消息为“继续”。"
)
