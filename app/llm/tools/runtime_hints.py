"""Central model-visible runtime and recovery hints for tool loops.

English text is the model-facing source of truth. Short Chinese summaries are appended only as operator-facing clarification.
"""


def helper_iter_checkpoint(iteration: int, hard_cap: int, helper_kind: str | None = None) -> str:
    kind = (helper_kind or "helper").strip() or "helper"
    return (
        f"[SYSTEM_HINT/helper_iter_checkpoint]\n"
        f"You are a {kind} helper at iteration {iteration}. The hard cap is {hard_cap}. "
        "Converge on a handoff artifact now. Compare the original helper contract, concrete source files, "
        "accepted outputs, and remaining gaps. If the useful evidence is already enough for a PASS or PARTIAL "
        "handoff, stop broad exploration, write a concise evidence/progress file, and finalize with exact paths. "
        "If important evidence is missing, take only one targeted action that can close a named gap; otherwise "
        "state the gap as PARTIAL with a continuation request. Repeated broad search, repeated full-file scans, "
        "or expanding the output body inside one tool call is not progress. For large material, save extracted "
        "or summarized text into a file and let the main process or a resumed helper page that file later.\n\n"
        "helper 长跑检查点：停止泛搜，形成 PASS/PARTIAL 证据文件和可续作说明。"
    )


def helper_tool_call_bloat_checkpoint(
    *,
    iteration: int,
    helper_kind: str | None = None,
    tool_name: str | None = None,
    arg_chars: int = 0,
) -> str:
    kind = (helper_kind or "helper").strip() or "helper"
    tool = (tool_name or "tool").strip() or "tool"
    return (
        "[SYSTEM_HINT/helper_tool_call_bloat]\n"
        f"You are a {kind} helper at iteration {iteration}. The current {tool} tool call grew to "
        f"about {arg_chars} argument characters before dispatch and was stopped locally to avoid a "
        "runaway single-call body. Treat this as a recoverable planning signal. The next tool call must be much smaller than the stopped call. For long "
        "Office deliverables such as DOCX/PPTX/XLSX, the Office tool can write and append structured blocks directly; this is available alongside script-based assembly. "
        "If you choose a custom script, account for the cost of streaming the script body and keep the script or section payload bounded. For long "
        "markdown/text/prose artifacts, use this protocol: first write a compact outline or skeleton, "
        "then append or edit one named section at a time, keeping each tool-call content block under "
        "about 2,000-4,000 characters. If a single artifact needs many long sections, split it into "
        "section files and let a later assembly step merge them. Use edit_file for focused edits rather than replacing a "
        "large repeated paragraph; use read_file on the local fragment, then insert or replace a "
        "small unique block. Convert the work into smaller files, page ranges, or section-sized outputs; "
        "for read tasks, write a compact evidence file with PASS/PARTIAL/FAIL, source paths, coverage, "
        "line/block ranges, and named gaps. For document or code tasks, write or repair one bounded "
        "artifact at a time and verify it before continuing. If the content remains too large "
        "within one more action, return PARTIAL with the exact section still needed so the main process "
        "can resume or split it.\n\n"
        "工具参数过大时，后续按小骨架加分段追加处理；每段约 2-4KB，必要时拆成章节文件，不能重复同一超长调用。"
    )


def helper_repeated_tool_call_bloat_checkpoint(
    *,
    iteration: int,
    helper_kind: str | None = None,
    tool_name: str | None = None,
    arg_chars: int = 0,
    count: int = 2,
) -> str:
    kind = (helper_kind or "helper").strip() or "helper"
    tool = (tool_name or "tool").strip() or "tool"
    return (
        "[SYSTEM_HINT/helper_repeated_tool_call_bloat]\n"
        f"You are a {kind} helper at iteration {iteration}. The {tool} tool call has reached "
        f"the large-argument recovery path {count} times in this helper run; the latest stopped "
        f"call was about {arg_chars} argument characters. Treat this as a convergence checkpoint. "
        "Fact: for Office deliverables, office(action='write'/'append'/'replace_block', ...) calls can preserve structured document intent without first generating a long python-docx script, while a script remains an available choice when it is justified by formatting or validation needs. "
        "Choose the next action to preserve useful work in a bounded handoff shape: write or update "
        "one compact evidence, source, section, or progress file; verify that one file when cheap; "
        "then finalize with PASS if the helper contract is satisfied or PARTIAL if named gaps remain. "
        "For broad deliverables, split the remaining body into section files and let the main process "
        "or a resumed helper assemble them. Keep the next tool-call content small and scoped to one "
        "named block, file, or section. Include exact Output files JSON, coverage status, and the "
        "smallest continuation request needed for any unfinished part.\n\n"
        "重复触发工具参数过大时进入收敛：先保存小型证据/章节/进度文件，再以 PASS 或 PARTIAL 交接。"
    )


def auto_recall_checkpoint(
    *,
    chars_kb: int,
    iteration: int,
    contract_snapshot: str,
    helper_count: int,
    contract_count: int,
    ready_artifact_count: int,
) -> str:
    return (
        f"[SYSTEM_RECALL/auto_at_{chars_kb}KB]\n"
        f"You have run {iteration} iterations and the context is about {chars_kb}KB.\n"
        f"{contract_snapshot}\n"
        f"Completed helper count: {helper_count} (delegate(action='status') gives details).\n"
        f"Structured contracts recorded: {contract_count}; ready artifacts recorded: {ready_artifact_count}.\n"
        f"Pause briefly and compare the current ready evidence, artifacts, helper status, and remaining gaps against the current task contract. "
        f"For long source material, prefer helper coverage summaries and segment-readable evidence paths over reading full extracted content into the main context.\n\n"
        f"长上下文里程碑提醒，回顾目标、已有证据和下一步。"
    )


def helper_pace_check(iteration: int) -> str:
    return (
        f"[SYSTEM_HINT/helper_pace_check]\n"
        f"You have run {iteration} iterations. Review the original prompt, current evidence, and remaining work. "
        f"Use progress_note once to summarize current progress and the next concrete action.\n\n"
        f"helper 节奏检查，整理进度并明确下一步。"
    )


def helper_long_run(iteration: int) -> str:
    return (
        f"[SYSTEM_HINT/helper_long_run]\n"
        f"You have run {iteration} iterations. Prepare a detailed progress summary file if useful, then either "
        f"finish with a clear report or state the precise continuation needed for resume=true. Distinguish "
        f"healthy long progress from repeated errors.\n\n"
        f"helper 长跑时形成进度摘要，便于主进程续作或收尾。"
    )


def helper_finalize_window(iteration: int) -> str:
    return (
        f"[SYSTEM_HINT/helper_finalize_window]\n"
        f"You have run {iteration} iterations and are near the cap. Stop broad exploration. "
        "Write or update a compact handoff artifact if one is missing, then finalize with completed work, "
        "remaining gaps, artifact paths, coverage status, and recommended continuation. Use PASS when the "
        "helper contract is satisfied, PARTIAL when useful evidence exists but named gaps remain, and FAIL "
        "only when no useful evidence can be produced.\n\n"
        "接近上限时停止扩展，输出 PASS/PARTIAL/FAIL 交接报告和路径。"
    )


def helper_completed_todos_handoff(
    *,
    iteration: int,
    helper_kind: str | None,
    completed: int,
) -> str:
    kind = (helper_kind or "helper").strip() or "helper"
    return (
        "[SYSTEM_HINT/helper_completed_todos_handoff]\n"
        f"You are a {kind} helper at iteration {iteration}. The latest todo_write result says "
        f"all {completed} checklist items are completed. Treat this as a handoff checkpoint, "
        "not as permission to start a new exploration loop. Re-read the original helper contract, "
        "expected outputs, current artifact paths, and verification evidence. If the contract is "
        "satisfied, stop tool use and produce the final PASS report with exact Output files JSON. "
        "If a required artifact, verification, or source coverage item is still missing, name the "
        "single concrete gap and do only the smallest targeted action needed to close it; otherwise "
        "return PARTIAL with a continuation request. After completed todos are checked, shift from inspection to final handoff instead of revisiting the same completed "
        "artifact unless a named acceptance gap remains.\n\n"
        "todo 全部完成时进入交接检查：核对契约、产物和验收；满足则收尾，不满足只处理明确缺口。"
    )


def helper_read_to_write_checkpoint(
    *,
    iteration: int,
    helper_kind: str | None,
    recent_reads: int,
) -> str:
    kind = (helper_kind or "helper").strip() or "helper"
    return (
        "[SYSTEM_HINT/helper_read_to_write_checkpoint]\n"
        f"You are a {kind} helper at iteration {iteration}. Your recent workflow has spent about "
        f"{recent_reads} consecutive tool results on reading, searching, or inspecting without writing the "
        "expected artifact or read-helper evidence file. Treat this as enough evidence for a first handoff draft unless a named source is "
        "still missing. Write or update the expected output now with the verified sections you already have. For a read helper, the expected output is usually a compact `*_evidence.txt` or `*_long_report.md` with PASS/PARTIAL/FAIL, covered files or ranges, unresolved gaps, and representative evidence. "
        "If the expected artifact now satisfies the contract, finish with PASS and exact paths. If coverage is "
        "incomplete, mark the missing section explicitly inside the artifact or in a PARTIAL handoff, then "
        "continue only with a targeted read for that named gap. For merge or synthesis tasks, "
        "create the merged file when the current evidence is sufficient instead of waiting for perfect source coverage; create the merged file "
        "from available sections, then page missing sections into it.\n\n"
        "连续读取但未写产物时，先把已有证据写成可验收草稿或 PARTIAL，再只针对明确缺口继续。"
    )


def helper_office_write_convergence_checkpoint(
    *,
    iteration: int,
    helper_kind: str | None,
    artifact: str,
    write_count: int,
) -> str:
    kind = (helper_kind or "helper").strip() or "helper"
    path = (artifact or "the Office artifact").strip() or "the Office artifact"
    return (
        "[SYSTEM_HINT/helper_office_write_convergence]\n"
        f"You are a {kind} helper at iteration {iteration}. The same Office artifact `{path}` "
        f"has already had {write_count} successful write/append/edit-style tool calls in this helper run. "
        "Treat this as a document-build convergence checkpoint, not as an automatic stop or forced batch rule. Compare the "
        "current artifact, original task contract, expected outputs, required sections/tables/images, and "
        "available evidence. Fact: office(action='write'/'append', ...) can carry multiple coherent blocks within the active "
        "adaptive size limits, and the tool reports arg_size_warning when those limits matter. If the artifact "
        "now satisfies the requested deliverable, finalize with a PASS report, exact Output files JSON, and "
        "verification facts. If one acceptance gap remains, name that gap and take a targeted repair or verification "
        "step. If the remaining gap cannot be closed with available evidence, return a PARTIAL handoff with the "
        "specific continuation request. If several related sections remain and the JSON payload stays reliable, a "
        "coherent batched call is available; if smaller calls are safer for quoting, tables, or verification, keep "
        "them bounded and evidence-driven.\n\n"
        "Office 文档已多次成功写入：这是收敛事实；可按剩余缺口、当前上限和 JSON 稳定性决定批量或小步修复。"
    )


def helper_office_read_convergence_checkpoint(
    *,
    iteration: int,
    helper_kind: str | None,
    artifact: str,
    read_count: int,
) -> str:
    kind = (helper_kind or "helper").strip() or "helper"
    path = (artifact or "the Office artifact").strip() or "the Office artifact"
    return (
        "[SYSTEM_HINT/helper_office_read_convergence]\n"
        f"You are a {kind} helper at iteration {iteration}. The same Office artifact `{path}` "
        f"has already had {read_count} successful read/inspect-style checks in this helper run. "
        "This is a factual convergence signal, not a rule that forbids another read. A successful "
        "office(action='read', ...) normally provides headings plus paragraph/block, table, and image counts. "
        "Before reading the same artifact again, compare those existing structural facts with the "
        "current acceptance contract and evidence already gathered. If they are enough, finalize "
        "with PASS, the exact path, structure facts, and Output files JSON. If one named detail is "
        "missing, use a targeted block/section check or repair step rather than another whole-artifact "
        "read. If the artifact changed since the last read, state that changed fact and inspect the "
        "changed area. If useful evidence exists but a required acceptance point remains unresolved, "
        "return PARTIAL with the exact gap and continuation request.\n\n"
        "同一 Office 产物已多次读取：这是收敛事实；若结构事实足够则交付，若仍有缺口则只针对明确缺口读取或修复。"
    )


def main_helper_completion_checkpoint(
    *,
    iteration: int,
    files: list[str] | None = None,
    facts: list[str] | None = None,
    warning_count: int = 0,
    contract_snapshot: str = "",
) -> str:
    clean_files = [
        str(item).strip()
        for item in (files or [])
        if str(item).strip()
    ][:8]
    clean_facts = [
        str(item).strip()
        for item in (facts or [])
        if str(item).strip()
    ][:6]
    file_text = ", ".join(f"`{item}`" for item in clean_files) or "the helper output file(s)"
    fact_text = "; ".join(clean_facts) or "helper producer_self_verified/output facts"
    warning_text = (
        f" The helper result also contains {warning_count} quality warning(s); weigh those warnings against the current acceptance contract."
        if warning_count
        else " No quality warnings were reported by the helper result."
    )
    contract_text = (
        f"\nCurrent task contract snapshot:\n{contract_snapshot}\n"
        if str(contract_snapshot or "").strip()
        else ""
    )
    return (
        "[SYSTEM_HINT/main_helper_completion_checkpoint]\n"
        f"You are the main process at iteration {iteration}. A helper returned completed output evidence for {file_text}. "
        f"Known facts from the helper/tool result: {fact_text}.{warning_text} "
        f"{contract_text}"
        "Treat this as model-visible evidence. Trust the successful helper's producer-owned content judgment when the result is clean. "
        "Do not re-read helper-produced text, Markdown, source, or project artifacts, and do not re-run checks over helper-owned artifacts merely to validate them again. If the active contract is covered by "
        "the helper report, output map, and helper-run check facts, finalize with PASS from those compact facts and exact deliverables. "
        "If a separate boundary remains, keep it outside helper-owned content QA: explicit user display/quote requests, main-owned apply/transfer/accounting, "
        "or helper warnings/contradictions. Send helper-owned quality, verifier, build, or acceptance gaps back to the producer helper or a verify helper; "
        "use main-thread inspection only for narrow main-owned runtime/file-management facts. Report PARTIAL only for an exact unresolved non-helper-trust boundary, "
        "not for uninspected helper-owned content.\n\n"
        "helper 自检且干净完成时信任其内容和结构判断；主进程用精简事实交付，不复读正文或重跑 helper 自有检查；helper 产物质量缺口交回生产 helper 或 verify helper。"
    )


def artifact_acceptance_convergence_hint(
    *,
    iteration: int,
    helper_kind: str | None,
    tool_name: str,
    artifact: str,
    count: int,
) -> str:
    kind = (helper_kind or "main").strip() or "main"
    return (
        "[SYSTEM_HINT/artifact_acceptance_convergence]\n"
        f"The {kind} workflow has checked the same artifact `{artifact}` with `{tool_name}` "
        f"{count} times by iteration {iteration}. Treat this as an acceptance convergence point. "
        "Compare the observed structure, content coverage, required terms/sections/tables, and "
        "requested output format against the current contract. If the artifact satisfies the "
        "acceptance points, stop tool use and return a PASS/final plan with the exact path and "
        "verification evidence. If it does not satisfy the contract, name the single remaining "
        "acceptance gap and take one targeted repair or resource action. Prefer section, block, "
        "or line-range checks over another full read of the same artifact.\n\n"
        "同一产物已多次验收：若满足契约就收束；若不满足，只针对一个明确缺口继续。"
    )


def main_milestone_checkpoint(iteration: int, hard_cap: int) -> str:
    return (
        f"[SYSTEM_HINT/main_milestone_checkpoint]\n"
        f"The main tool loop has reached iteration {iteration}. The safety cap is {hard_cap}. "
        "This is a planning checkpoint, not a failure. Compare current verified evidence against the user's request. "
        "If a coherent milestone is already applied and checked, stop tool use and output the final JSON for this round, "
        "including completed work, exact validation, and remaining continuation in internal_note. "
        "If key acceptance points are still missing, choose only the smallest next action needed to reach one checkable milestone. "
        "After a verified milestone exists, preserve momentum by finalizing instead of polishing or broad exploration.\n\n"
        "主进程里程碑检查：已有可验证里程碑时先收束交付；未完成时只做最小下一步，避免长链无限细修。"
    )


def main_finalize_window(iteration: int, hard_cap: int) -> str:
    return (
        f"[SYSTEM_HINT/main_finalize_window]\n"
        f"The main tool loop has reached iteration {iteration} and is close to the safety cap {hard_cap}. "
        "Stop starting new broad work. Either run one final cheap verification command if absolutely necessary, "
        "or output the final JSON from current evidence. Preserve unfinished work as continuation notes rather than "
        "trying to finish every possible improvement in this round.\n\n"
        "主进程接近上限：停止新增大任务，基于当前证据收束，未完成内容写入续作说明。"
    )


def main_env_run_convergence_hint(
    *,
    family: str,
    count: int,
) -> str:
    label = (family or "env_run family").strip() or "env_run family"
    return (
        "[SYSTEM_HINT/main_env_run_convergence]\n"
        f"Current loop fact: the main process has run {count} env_run commands in the same evidence family "
        f"`{label}`. These command results are already available in the tool transcript. Before running another "
        "command in the same family, compare the existing stdout/stderr, schema/query/verifier facts, active task, "
        "and remaining named gap. If the existing evidence is enough, move to the artifact, verifier, plan, or final "
        "step. If one fact is still missing, run one targeted command that names that missing fact. This is a "
        "convergence fact, not a forced stop.\n\n"
        "主进程同类 env_run 已多次执行；先比较已有 stdout/schema/query/verifier 事实和剩余缺口，再决定是否还需一个有明确目标的命令。"
    )


SOURCE_WRITE_DELEGATION_HINT = (
    "[SYSTEM_HINT/source_write_delegation]\n"
    "The last model output started a substantial project-file write from the main thread. "
    "That path was stopped before dispatch because substantial source authoring, "
    "multi-file edits, benchmark scripts, and iterative debugging belong to a code helper. "
    "The recoverable facts are the target path, content size, latest stable evidence, and whether the active task still "
    "needs durable project/source work. Repeating the same write call or shortening content only to bypass the stop does "
    "not add evidence. "
    "If the active task still needs durable project/source work, "
        "use a focused helper request envelope: "
        "`task_id`, `kind`, `mode`, `framework`, `input_files`, focused `prompt`, "
        "`expected_outputs`, and `acceptance_checks`. For a new project, large feature, "
        "analysis report, or long document, the shared framework, outline, interface/schema, "
        "evidence map, and merge order are themselves helper-owned artifacts when they require "
        "project files, source-like content, or more than a compact plan. The main thread may "
        "state the contract in `agent_state` and in delegate `framework` fields, while helpers own "
        "shared interfaces, framework files, benchmark harnesses, source modules, and "
        "long sections. A recoverable shape is one coherent vertical slice or a small batch of "
        "independent files/sections instead of streaming a long body from the main thread. "
    "If a helper already owns the failing file or section, the preserved workspace may support resuming that "
    "same task_id with the same envelope fields and exact failure output. The main thread "
    "can consume helper reports, apply project diffs or merge sections, and keep final acceptance/accounting compact.\n\n"
    "主进程长产物写入被停止；框架文件、共享接口、脚手架和源码也应由 helper 产出并自验。主进程只记录契约、派发/续作分片、应用、合并和验收记账。"
)


LLM_TIMEOUT_RECOVERY_HINT = (
    "[SYSTEM_HINT/llm_call_recovery]\n"
    "The previous LLM call timed out after the normal continuation and low-reasoning "
    "retries. Treat this as a recoverable execution interruption, not as task completion. "
    "Continue from the latest stable evidence. Prefer smaller next steps, reuse preserved "
    "workspace state, and for incomplete helper work use the same task_id with resume=true; "
    "escalate mode only when the prior failure shows a capability or repeated-error gap. "
    "If no checkable evidence exists yet, gather evidence before making concrete claims.\n\n"
    "LLM 调用超时是可恢复中断；继续缩小步骤、续作或升级 helper，不要假装完成。"
)


LLM_RETRY_FAILURE_RECOVERY_HINT = (
    "[SYSTEM_HINT/llm_call_recovery]\n"
    "The previous LLM call failed after retry. Treat this as a recoverable execution "
    "interruption, not as task completion. Continue from the latest stable evidence, "
    "choose a smaller next action, and use resume=true or a stronger helper only when "
    "the existing evidence supports that change. Report success only with verified "
    "outputs or concrete tool evidence.\n\n"
    "LLM 调用失败是可恢复中断；基于已有证据调整下一步，不能无证据报完成。"
)


LLM_REPEAT_TIMEOUT_RECOVERY_HINT = (
    "[SYSTEM_HINT/llm_call_recovery]\n"
    "The LLM call timed out again. Continue from stable evidence with a smaller concrete "
    "action, or resume/escalate incomplete helper work. Finalize as complete only when "
    "the deliverable is verified.\n\n"
    "再次超时后缩小动作或续作 helper，未验证前不要完成收束。"
)


def retry_required_before_final(current: int, maximum: int, retry_lines: list[str]) -> str:
    return (
        "[SYSTEM_HINT/unresolved_helper_facts_before_final]\n"
        f"Recovery checkpoint {current}/{maximum}. "
        "The latest delegate result shows recoverable incomplete helper work, and the target "
        "deliverable has not been confirmed complete. This checkpoint records facts for the next "
        "decision; it does not by itself choose a tool. Decide from the active task and evidence "
        "whether to resume the same task_id, escalate or split the gap, verify an already-complete "
        "deliverable, or finish with an explicit incomplete-state summary.\n"
        + "\n".join(retry_lines)
        + "\n\nhelper 未完成事实检查点；由模型根据当前任务和证据决定续作、升级/拆分、验收或说明未完成。"
    )


def retry_still_required_before_final(retry_lines: list[str]) -> str:
    return (
        "[SYSTEM_HINT/unresolved_helper_facts_still_visible]\n"
        "Recovery checkpoints were already shown, but the same helper gap remains. This is a "
        "factual checkpoint, not a forced tool decision: the model should decide whether to retry "
        "the same task_id, split/escalate the gap, or finish with an explicit incomplete-state "
        "summary when the available evidence supports that. Success still requires verified "
        "deliverables or concrete tool evidence.\n"
        + "\n".join(retry_lines)
        + "\n\n恢复检查点已用尽；陈述缺口事实，由模型决定续作、拆分/升级，或基于证据说明未完成。"
    )


def repeated_failure(stuck_reason: str) -> str:
    return (
        f"[SYSTEM_HINT/repeated_failure]\n"
        f"Repeated failure pattern detected: {stuck_reason}\n\n"
        f"Latest failure evidence, the concrete error, and the next verifiable step are the relevant facts for deciding whether "
        f"to change strategy. Useful partial results can be summarized in the plan with remaining gaps in internal_note. "
        f"A clear repair path and task importance can justify escalation or a better helper split.\n\n"
        f"检测到反复失败时，基于最新证据换策略或升级。"
    )


def retry_before_finalize(task_names: str, *, short: bool = False) -> str:
    if short:
        return (
            "[SYSTEM_HINT/retry_before_finalize]\n"
            f"A helper next_action is available for task(s): {task_names}. Compare it with the active task before ending.\n\n"
            "存在 helper 续作事实；先与当前任务契约对照。"
        )
    return (
        "[SYSTEM_HINT/retry_before_finalize]\n"
        f"A helper next_action is available for task(s): {task_names}. "
        "This is recovery evidence, not an automatic decision. Compare the suggested resume/escalation parameters "
        "with the active task, verified deliverables, user interruptions, and retry history before finalizing.\n\n"
        "有 next_action 是恢复事实；由模型结合当前任务、交付物和重试历史决定续作或收束。"
    )


def strategy_recovery(stuck_reason: str, *, consecutive: bool = False) -> str:
    if consecutive:
        return (
            f"[SYSTEM_HINT/strategy_recovery]\n"
            f"The workflow is still repeating a failed pattern after several iterations: {stuck_reason}. "
            "This is evidence that the current tactic may need a different recoverable path. A helper "
            "resume or hard-mode retry can fit the same narrow task when prior work is useful; split "
            "the task if the boundary is too broad; use targeted verification before accepting "
            "or rejecting helper output. Continue only if the next action can add evidence.\n\n"
            "连续卡住时换策略：续作、升级、拆分或验证，不直接结束。"
        )
    return (
        f"[SYSTEM_HINT/strategy_recovery]\n"
        f"The main workflow has repeated the same failing pattern: {stuck_reason}. "
        "Continue recovery from this failure. Re-read the latest concrete error, then change "
        "one part of the strategy: narrow the task boundary, choose the correct helper kind, "
        "resume the same task_id from preserved work, upgrade mode for a specific difficult "
        "retry, or verify that the remaining gap is genuinely blocked. Continue only with "
        "a step that can produce new evidence.\n\n"
        "主流程反复失败时改策略、续作或升级；不要仅凭失败事实收尾。"
    )
