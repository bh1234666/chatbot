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
        "expected artifact. Treat this as enough evidence for a first handoff draft unless a named source is "
        "still missing. Write or update the expected output now with the verified sections you already have. "
        "If the expected artifact now satisfies the contract, finish with PASS and exact paths. If coverage is "
        "incomplete, mark the missing section explicitly inside the artifact or in a PARTIAL handoff, then "
        "continue only with a targeted read for that named gap. For merge or synthesis tasks, "
        "create the merged file when the current evidence is sufficient instead of waiting for perfect source coverage; create the merged file "
        "from available sections, then page missing sections into it.\n\n"
        "连续读取但未写产物时，先把已有证据写成可验收草稿或 PARTIAL，再只针对明确缺口继续。"
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


SOURCE_WRITE_DELEGATION_HINT = (
    "[SYSTEM_HINT/source_write_delegation]\n"
    "The last model output started a substantial project-file write from the main thread. "
    "That path was stopped before dispatch because substantial source authoring, "
    "multi-file edits, benchmark scripts, and iterative debugging belong to a code helper. "
    "Recover by changing the work shape rather than repeating the same write call or shortening it just to bypass the stop. "
    "Continue by preserving the latest stable evidence, fetching any needed project files, "
        "and immediately spawning or resuming focused helpers with a helper request envelope: "
        "`task_id`, `kind`, `mode`, `framework`, `input_files`, focused `prompt`, "
        "`expected_outputs`, and `acceptance_checks`. For a new project, large feature, "
        "analysis report, or long document, the shared framework, outline, interface/schema, "
        "evidence map, and merge order are themselves helper-owned artifacts when they require "
        "project files, source-like content, or more than a compact plan. The main thread may "
        "state the contract in `agent_state` and in delegate `framework` fields, while helpers own "
        "shared interfaces, framework files, benchmark harnesses, source modules, and "
        "long sections. Delegate one coherent vertical slice or a small batch of "
        "independent files/sections instead of streaming a long body from the main thread. "
    "If a helper already owns the failing file or section, resume that "
    "same task_id with the same envelope fields and exact failure output. The main thread "
    "should inspect helper outputs, apply project diffs or merge sections, and run final verification.\n\n"
    "主进程长产物写入被停止；框架文件、共享接口、脚手架和源码也应由 helper 产出。主进程只记录契约、派发/续作分片、验收、合并和最终验证。"
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
        "[SYSTEM_HINT/retry_required_before_final]\n"
        f"Recovery checkpoint {current}/{maximum}. "
        "The latest delegate result shows recoverable incomplete helper work, and the target "
        "deliverable has not been confirmed complete. Continue with delegate before finalizing: "
        "reuse the same task_id and preserved workspace, upgrade mode when the evidence shows "
        "capability or repeated-error gaps, or split the work when the scope is too broad. "
        "Only end now if the user interrupted, the deliverable is already verified complete, "
        "or the remaining gap is verified unrecoverable and must be reported as incomplete.\n"
        + "\n".join(retry_lines)
        + "\n\nhelper 未完成且产物未确认时，优先续作、升级、拆分，或说明已验证的不可恢复缺口。"
    )


def retry_still_required_before_final(retry_lines: list[str]) -> str:
    return (
        "[SYSTEM_HINT/retry_still_required_before_final]\n"
        "A recoverable helper gap is still blocking final synthesis. The previous reminders were "
        "not satisfied, so final synthesis should wait for recovery. Make a delegate "
        "tool call now using the listed same-task resume/escalation parameters, or split the same "
        "coverage gap into a replacement helper with a clear framework and acceptance checks. Only "
        "finish without retry if the user explicitly interrupted or a tool result proves the gap is "
        "unrecoverable.\n"
        + "\n".join(retry_lines)
        + "\n\n可恢复 helper 缺口仍阻塞收束；现在必须调用 delegate 续作、升级或拆分同一缺口。"
    )


def repeated_failure(stuck_reason: str) -> str:
    return (
        f"[SYSTEM_HINT/repeated_failure]\n"
        f"Repeated failure pattern detected: {stuck_reason}\n\n"
        f"Recover by reading the latest failure evidence, identifying the concrete error and next verifiable step, "
        f"then changing strategy. If partial results are useful, summarize them in the plan and record remaining gaps "
        f"in internal_note. If a clear repair path remains and the task matters, consider escalation or a better helper split.\n\n"
        f"检测到反复失败时，基于最新证据换策略或升级。"
    )


def retry_before_finalize(task_names: str, *, short: bool = False) -> str:
    if short:
        return (
            "[SYSTEM_HINT/retry_before_finalize]\n"
            f"A helper next_action is available for task(s): {task_names}. Continue or escalate first before ending.\n\n"
            "先处理可续作任务，再结束。"
        )
    return (
        "[SYSTEM_HINT/retry_before_finalize]\n"
        f"A helper next_action is available for task(s): {task_names}. "
        "Continue via delegate using the suggested resume/escalation parameters before finalizing, "
        "unless the user interrupted, deliverables are already sufficient, or retry has proven unrecoverable.\n\n"
        "有 next_action 时优先续作，满足交付或不可恢复时再总结。"
    )


def strategy_recovery(stuck_reason: str, *, consecutive: bool = False) -> str:
    if consecutive:
        return (
            f"[SYSTEM_HINT/strategy_recovery]\n"
            f"The workflow is still repeating a failed pattern after several iterations: {stuck_reason}. "
            "Pause the current tactic and choose a different recoverable path. Prefer a helper "
            "resume or hard-mode retry for the same narrow task when prior work is useful; split "
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
