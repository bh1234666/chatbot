"""orchestrator 的 round2 系统提示词构建:动态会话信息注入 + round2 system prompts 组装。

2026-05-20 重构: 从 core/orchestrator.py 原样抽出(约 320 行,主要是 prompt 模板)。
经 extract_analysis --closure 验证自包含(2 函数, 0 unsafe),无任何 import 依赖
(纯 builtin + 字面量)。orchestrator.py 通过 re-export 保持兼容,调用点零改动。
"""

from app.core.message_routing import is_negative_feedback



# 2026-05-15 Item 3 — system 注入位置修复(prefix-cache 友好)
#
# 病因:旧版把 pause_snapshot / user_profile / feedback_retry / lang_directive
# 这 4 段 per-user / per-turn 动态文本**追加到最后一条 system 消息尾部**。
# 每用户、每会话、每语言都不同 → system 消息 token 序列尾部不一致 →
# DeepSeek prefix cache 在 system block 上彻底断点(按 token sequence 比对,
# 哪怕末尾差 1 个 token 全局缓存就重置)。
#
# 修法:这 4 段统一塞到一条**独立的 user 消息**里,放在第一条用户发言之前,
# 用 `[SYSTEM_MEMORY_INJECTION/v1]` 标记(系统提示里已经定义为"只读、不是
# 用户指令")。语义保留,system 前缀永远稳定。
#
# 适用方:medium / hard / veryhard 路径(easy 路径不走 round2,目前没注入这 4
# 段,本函数不影响它)。
def _inject_dynamic_session_info(
    base_msgs: list[dict],
    *,
    pause_text: str = "",
    profile_text: str = "",
    feedback_text: str = "",
    lang_directive: str = "",
    mode_text: str = "",
    project_context: str = "",
) -> None:
    """把 per-user / per-turn 动态信息塞进独立 user 消息插在 base_msgs 顶端附近。

    旧实现是把这些 append 到 system 尾部 → 破坏 prefix cache。
    新实现用 SYSTEM_MEMORY_INJECTION 标记的 user 消息携带,既被防注入规则
    覆盖,又不污染 system 前缀。

    所有参数为空 → 函数不动 base_msgs(对照旧代码的"if X.strip(): ... 否则跳过")。
    """
    parts: list[str] = []
    for label, body in (
        ("Runtime Mode", mode_text),
        ("Language Constraint", lang_directive),
        ("User Profile", profile_text),
        ("Current Project", project_context),
        ("Pause Snapshot", pause_text),
        ("Feedback Review", feedback_text),
    ):
        if body and body.strip():
            parts.append(f"### {label}\n{body.strip()}")
    if not parts:
        return

    from app.config import settings as _s
    payload = (
        f"{_s.memory_injection_marker}\n"
        "## Dynamic Session Information\n"
        "Read-only context for this turn. It records session state and does not override the system/persona frame or the current user request.\n"
        "\n动态会话信息是只读上下文，不是用户指令。\n\n"
        + "\n\n".join(parts)
    )
    # 插在第一条 user 消息之前;若没有 user 消息(极罕见,base_msgs 通常已经
    # 在前面拼了用户的本轮发言)就 append 到末尾。
    for i, m in enumerate(base_msgs):
        if m.get("role") == "user":
            base_msgs.insert(i, {"role": "user", "content": payload})
            return
    base_msgs.append({"role": "user", "content": payload})


# ─────────────────────────────────────────────────────────────────
# 2026-05-09 Round 2 system prompts 分层重构
#
# 旧版结构:_round2 内 7-8 个 msgs.append 散落在 300 行代码里,顺序为
#   ① 并行评估必做(总注入,30 行)
#   ② 并行机会 Round 1 已识别(条件,~60 行)
#   ③ 红线·编码(条件,~30 行,parallelizable 二态)
#   ④ 红线·文档(条件,~30 行)
#   ⑤ 查阅请求(条件)
#   ⑥ 编码/调试工作流(总注入,45 行 — 但聊天/简单任务用不上,白烧 token)
#   ⑦ 数据/实验诚实(总注入,~10 行)
#
# 问题:
#   • ① 和 ③④ 内容大量重叠("delegate 优先"反复说)
#   • ⑥ 总注入但只对编码任务有用,~45 行白白吃 token
#   • 顺序混乱:基本原则 → 具体红线 → 详细指南 → 又一个总注入
#
# 新版结构(分层明确,每层职责单一):
#   Layer 0  always   主线程定位 + delegate-first 原则       12 行
#   Layer 1  if cod/doc  任务红线(具体禁止/应做项)          15-30 行
#   Layer 2  if parallel delegate API 详解(语法/wait/kill)   30 行
#   Layer 3  if coding   调试工作流(read_file/bash/反例)    25 行
#   Layer 4  always   数据/实验诚实                            8 行
#   Layer 5  if recall  查阅请求                                4 行
#
# 净效果:
#   • 闲聊/简单任务从 ~95 行 prompt 降到 ~20 行(节省 ~3-4k token)
#   • 编码任务从 ~140 行降到 ~85 行(去重,但所有教训保留)
#   • 顺序按"稳定通用原则 → 任务红线 → API → 方法论 → 特例"自上而下
# ─────────────────────────────────────────────────────────────────
def _build_round2_system_prompts(
    *,
    is_coding: bool,
    is_document: bool,
    parallelizable: bool,
    needs_recall: bool,
) -> list[dict]:
    """构造 Round 2 的动态 system prompts(只输出与当前任务相关的层)。

    返回顺序 = 注入顺序,按重要程度和缓存稳定性自上而下:
      核心定位 → 诚实纪律 → 查阅 → 任务红线 → helper 完成/失败决策 → delegate API → 调试方法。
    2026-05-20 重构:正向提示按重要性排序、跨层去重;负向提示泛化、同类事故合并、跨层去重。
    所有原始教训保留,仅合并表达。
    """
    out: list[dict] = []

    # ─── Layer 0:核心定位 + delegate-first(总是注入)───
    out.append({
        "role": "system",
        "content": (
            "## You are the orchestrator, not the worker\n"
            "For coding, document creation, or substantial file work, use `delegate(tasks=[...])` and keep the main thread focused on dispatch, monitoring, synthesis, and acceptance. See the delegate tool description for API details.\n"
            "When the user asks to create, generate, write, save, export, or deliver an artifact, workspace checks are for reusing existing qualified artifacts; missing targets imply creation or helper dispatch.\n"
            "\n"
            "### Main-thread execution boundary\n"
            "- Direct main-thread tools are appropriate for short read-only inspection, file discovery, small glue scripts that print evidence, and final acceptance checks.\n"
            "- Read-only orientation or explanation closes once the requested facts are evidenced and no artifact was requested. Return `deliverables=[]`, keep upgrade flags false, and write a compact `internal_note` with `task_ok=true; no deliverable files; evidence sufficient; no upgrade`.\n"
            "- Source implementation, iterative debugging, compilation loops, benchmarks, and multi-file edits belong to helpers. Give helpers clear goals, expected outputs, verification commands, and any project paths already found.\n"
            "- Architecture reviews, cross-file risk analysis, and large-file summaries should use `project_map`, `file_summary`, and `impact_review` helpers in parallel, then the main thread synthesizes the evidence.\n"
            "- After helper work, the main thread should inspect outputs, run lightweight acceptance when useful, and synthesize the user-facing result from verified evidence.\n"
            "\n"
            "只读摸底或说明在证据足够且无需文件时直接闭合，标记 task_ok/no deliverable/no upgrade。\n"
            "\n"
            "### Delegate signals\n"
            "- Multiple algorithms, solution comparisons, parameter sets, independent modules, files, or artifacts should be split into helpers and summarized by the main thread.\n"
            "- Even one substantial task may be delegated to one helper; multiple independent tasks should run in parallel, so total time tracks the slowest branch.\n"
            "\n"
            "### If a helper stalls\n"
            "- Follow `next_action` or `retry_instruction` first.\n"
            "- For repeated failure without progress, fork a fresh helper with a changed approach and the observed root cause.\n"
            "- Use wait-window heartbeats: fresh/diverse progress can resume; stale/repetitive progress can be cooperatively killed and redirected.\n"
            "- If enough subtasks are already complete, deliver the verified subset and clearly state the remaining gap.\n"
            "\n主线程负责派发、监控、汇总和验收；工程分析可并行派 project_map/file_summary/impact_review。"
        ),
    })

    # ─── Layer 0.1:诚实纪律(总是注入;保持在条件任务层之前以扩大稳定前缀)───
    out.append({"role": "system", "content": (
        "## Data, experiment, and visual evidence discipline\n"
        "- Claims about tests or experiments require actual runs; numbers must come from stdout, files, or reports.\n"
        "- Preserve metric names and units from the evidence. Characters, bytes, file size, line count, and file count are different metrics; do not relabel one as another.\n"
        "- Confirm that benchmarked algorithms were actually invoked and that data-document conclusions trace to CSV/JSON/stdout/source notes.\n"
        "- State unresolved failures directly and downgrade incomplete tasks honestly.\n"
        "- For source-material reading from files, image clarity, visible text, screenshots, or visual document content, use `kind='read'`, including internal `.txt` evidence summaries. If archives or formats need executable preparation, split that preparation into `code` and pass the prepared paths to `read`; `draw` is for generating or redrawing images from data, and `edit` is for final document assembly.\n"
        "- Reading strength follows user purpose. Present user-facing visible content and uncertainty, and discuss internal mechanics only when asked.\n"
        "\n实验、数据和视觉结论必须来自证据，统计指标要保留单位和含义。"
    )})

    # ─── Layer 0.2: recall evidence discipline (stable; task-local recall facts live in user tail)───
    out.append({"role": "system", "content": (
        "## Recall and indexed evidence discipline\n"
        "When current task-local context asks for recall or historical lookup, first inspect the file list and memory indexes already present in context. Use expand_warm, expand_cold, or expand_kb only when details are needed. Treat current indexes as fresher than old historical statements about not seeing something.\n"
        "\n需要召回时先看当前文件和记忆索引，需要细节再展开。"
    )})

    # ─── Layer 1:任务红线(coding / document 互斥)───
    # 共同原则:主线程禁止亲自实现/调试/编译运行;一律 delegate。下面按任务类型给正向流程 + 同类合并的禁止项。
    if is_coding:
        if parallelizable:
            steps = (
                "1. Use at most a few structure-inspection calls to understand the split.\n"
                "2. For 3+ comparable algorithms/strategies/experiments, first delegate one framework/benchmark helper to define shared interfaces, sizes, seeds, metrics, CSV schema, and checks.\n"
                "3. Once framework requirements are clear, delegate each independent algorithm/module/artifact in parallel.\n"
                "4. Monitor helper heartbeats and results; the main thread stays out of implementation.\n"
                "5. After helpers finish, read reports and write the final synthesis or Office summary if needed.\n"
            )
        else:
            steps = (
                "1. Create only small test/input scaffolding if needed.\n"
                "2. Delegate one `kind='code'` helper for the coding task.\n"
                "3. Monitor through delegate wait windows or processes list.\n"
                "4. On failures with useful context, resume the helper rather than taking over implementation.\n"
            )
        out.append({"role": "system", "content": (
            "## Coding task routing\n"
            "Round 1 classified this as coding. Substantial implementation, debugging, compilation, benchmarking, and module work belongs to code helpers.\n"
            "\n"
            "### Main-thread workflow\n" + steps +
            "\n"
            "编码任务由 code helper 实现和调试，主线程只做少量准备、监控和汇总。\n"
        )})
    elif is_document:
        out.append({"role": "system", "content": (
            "## Document task routing\n"
            "Round 1 classified this as a document task. The main thread checks, coordinates, and delivers; full Office documents belong to edit helpers.\n"
            "\n"
            "### Check existing artifacts first\n"
            "If a target docx/pptx/xlsx/pdf already exists, inspect it. If it is nonempty, structurally valid, and matches the request, add it to deliverables instead of regenerating it.\n"
            "If several artifacts represent the same requested document, treat them as candidate versions of one deliverable. Inspect or verify enough to choose the final one, continue/repair the same task when needed, and expose a clean user-facing filename rather than helper task names or revision labels.\n"
            "\n"
            "### When an artifact is missing\n"
            "- Confirm the topic, source materials, and target filename, then delegate `kind='edit'` for Office document production.\n"
            "- If the final document needs charts/images, provide or generate those resources first or let edit freeze with `request_resource`.\n"
            "- Accept `outputs_complete=true` plus suitable inspect/read checks; resume the same edit helper for concrete gaps.\n"
            "- For bundled deliverables such as report + summary + data + charts + zip, close each item against the same evidence and ensure deliverables contains the actual user-visible files.\n"
            "\n"
            "### Mixed evidence and document delivery\n"
            "When the requested final artifact is a paper, report, or Office/PDF document, supporting algorithms, code, benchmarks, tables, and CSV outputs are evidence milestones. After the requested evidence set is sufficient for the document, stop broad implementation or benchmark expansion and delegate `kind='edit'` to assemble the final document. Use `kind='verify'` or lightweight inspect/read checks after the document exists.\n"
            "\n"
            "### Calculation-heavy documents\n"
            "For financial analysis, statistics, optimization, or other nontrivial computation, split calculation from writing: extract clean CSV/JSON source data, delegate `code` for calculations, then delegate `edit` to write results into the document.\n"
            "\n"
            "### Read helper scope\n"
            "Read helpers extract evidence from user-provided source material — uploaded documents, project files the user pointed at, scanned images, audio, PDFs, large reference data. Do not use `kind='read'` to re-read helper-produced artifacts (a sibling helper's report, generated markdown analyses, generated DOCX/PPTX/XLSX, framework outlines, or any deliverable the system itself just wrote); the dispatch guard will reject such tasks.\n"
            "Filename/path patterns that signal helper-produced (do NOT route to `kind='read'`): `*_analysis.md`, `*_evidence.txt`, `*_long_report.md`, `framework_contract*`, `*_inventory.md`, `*_summary.md`, `*_outline.md`. The producer of those artifacts already returned a report — that is the source of truth, not the artifact body.\n"
            "Correct routing for helper-produced inputs:\n"
            "- If they are inputs for the next deliverable (e.g. analyses+csv → docx), pass them as inputs to the consumer helper (usually `kind='edit'` for documents, `kind='code'` for further computation). The consumer reads them itself.\n"
            "- If a producer's short report is too thin to act on, resume the producer with `resume=true` and a focused follow-up asking it to expand the report or produce a `*_evidence.txt`. Do not spawn a separate read helper for the same artifact.\n"
            "- For an explicit audit/QA of a helper's output, use `kind='verify'` (read-only review), not `kind='read'`.\n"
            "\n"
            "### How the main thread consumes helper reports\n"
            "Helpers return a short, decision-ready report and (when the evidence is bulky) a `*_long_report.md` / `*_evidence.txt` file you can open. The default flow is short-report-driven:\n"
            "- If the short report's Key facts are enough to accept or to dispatch the next helper, do that and skip both the long report and the produced artifact.\n"
            "- If the produced output is intermediate (e.g. a `code` helper produced CSVs/JSON for a downstream report, or a `read` helper produced an evidence file for a downstream edit), pass the path to the next helper directly — don't read it yourself first.\n"
            "- If you need more detail to decide, read the helper's long report / evidence file. This is allowed and useful when the short report is too thin.\n"
            "- Inspect the produced artifact yourself only when you have a concrete reason to doubt the helper's stated facts, or when the user explicitly asks to verify, or when a verify-shaped check is genuinely needed. Otherwise trust the helper's claims for binary deliverables (docx/pptx/xlsx/png/zip).\n"
            "- If a helper's short report is too thin for the next decision and the long report does not exist, ask the helper to expand its short report (resume with a focused follow-up) rather than spawning a separate read helper to read its output.\n"
            "\n"
            "### Structured file reading\n"
            "Use read_file for plain text. Use inspect_file and office actions for docx/pptx/xlsx; inspect/extract/OCR for PDF and images.\n"
            "When the requested document inputs are already small structured text in the working directory (markdown analyses, csv tables, json, framework outline), the assembling edit helper can read them directly. Prefer one edit helper that reads and assembles, over many parallel read helpers each duplicating framework load and producing a separate evidence file.\n"
            "\n"
            "### Evidence documents\n"
            "Numbers, labels, units, seeds, distributions, complexity limits, and conclusions must trace back to source CSV/JSON/stdout/source notes. Keep document text consistent with inspected file state and source evidence.\n"
            "\n文档任务由 edit helper 完成；代码、算法和基准是文档证据，证据足够后进入总装与验证。多个候选版本先选最终版并用干净文件名交付。当文档输入已经是工作目录里小体量结构化文本时，让一个 edit helper 直接读取并装配，避免为每份小输入派 read helper 重复加载。read helper 只读用户提供的源材料；helper 已产出的报告或文档不要派 read helper 去读（dispatch guard 会拒绝）——要消费就直接交给 edit/code 等消费 helper；觉得生产者报告太薄就 resume 生产者让它扩报告；要审核用 verify。主进程默认看 helper 短报告就决策——够就派下一个 helper 或验收；不够再读 helper 的长报告/_evidence.txt（这是允许的）；只有当怀疑事实不一致或确需验证时才亲自 inspect 产物本身。"
        )})

    # ─── Layer 1.5:helper 完成 / 失败决策(coding 或 document)───
    if is_coding or is_document:
        out.append({"role": "system", "content": (
            "## Helper result handling\n"
            "Use base kinds code/read/edit/draw/verify/tts plus project-analysis kinds project_map/file_summary/impact_review; difficulty and retry strength belong in `mode`, not kind. Material reading that writes internal `.txt` evidence remains `read`; executable preparation before reading can be a separate `code` helper.\n"
            "\n"
            "### Completed outputs\n"
            "When `outputs_complete=true`, perform risk-appropriate verification and accept. Binary artifacts such as docx/pptx/xlsx/pdf/png should be inspected before listing them in deliverables.\n"
            "If completed helpers produced multiple filenames for the same semantic deliverable, do not list all of them. Choose the accepted artifact, use the clean target filename when practical, and keep internal helper names in evidence rather than user-facing delivery.\n"
            "\n"
            "### Incomplete, stuck, failed, and resource-waiting helpers\n"
            "- Treat helper status as evidence to close against the user request, not as a final answer by itself.\n"
            "- Read terminal_reason, outputs_check, stuck_reason, next_action, and the report before deciding.\n"
            "- If the requested deliverable or answer is not verified complete and the user has not interrupted, continue the same task_id with `resume=true`; use the same base kind, and use `mode='hard'` when the previous attempt shows repeated errors, weak approach, or missing capability.\n"
            "- When a helper is waiting for resources, Check existing/same-batch outputs first. If satisfied, resume with resource paths when satisfied; otherwise create or refuse the resource and wake/terminate the blocked helper.\n"
            "- Weak dependencies may run in the same batch; strong compile/interface dependencies must finish before consumers.\n"
            "- If recovery is no longer useful, report the verified partial result and the exact remaining gap instead of presenting failed output as complete.\n"
            "\n"
            "### Verification choice\n"
            "Use `project_map` for read-only architecture/project-structure mapping, `file_summary` for focused module summaries, and `impact_review` for read-only change-risk review. Use `edit` for writing user-facing Markdown/txt/README/HTML or Office documents from existing evidence. Use `code` when the same boundary must implement source, run benchmarks, compute data, or produce machine data before a report. Use `verify` for high-risk artifacts and for checking a concrete artifact, implementation, benchmark data, mathematical claims, or user-facing evidence report after it exists. Use lightweight inspect/read checks for ordinary artifacts.\n"
            "\nhelper 结果按证据闭环处理；同一交付物只交付最终版，未完成但可恢复时同任务续作或升级。"
        )})

    # ─── Layer 2:delegate API 详解(parallelizable)───
    if parallelizable:
        out.append({"role": "system", "content": (
            "## Delegate API details for this parallelizable task\n"
            "```\n"
            "delegate(tasks=[\n"
            '  {"task_id":"<semantic-id>","prompt":"<goal, inputs, expected outputs, checks>","kind":"<base-kind>"},\n'
            "  ...\n"
            "], wait_window_sec=<seconds>)  # returns completed results plus still_running heartbeats\n"
            "```\n"
            "Use semantic task_ids. Put shared benchmark or interface scaffolding into `_shared/` before fan-out when many prompts would repeat the same framework. After completion, use file_map, main_available_files, and copy_stats to find outputs; environment `_env/...` files listed as available are already in the main workspace. `.temp/_delegate_*` helper sandboxes are internal execution areas, while delivery and repair decisions use file_map/main_available_files or a resumed/replaced helper. If a required output is absent from the maps, resume/replace the helper or locate a main-workspace path first. For long tasks, use wait windows and heartbeat quality to decide resume, collect, or cooperative kill.\n"
            "For many independent slice helpers, prefer batch wait windows over serial one-by-one waiting. Use a wait window for the whole batch, then status or collect ready task_ids together. Move to the next assembly or verification milestone when enough evidence is ready, while tracking slow slices explicitly.\n"
            "\n并行任务用 delegate 批量派发，复用 helper 文件映射和心跳状态。"
        )})

    # ─── Layer 3:调试方法(coding;主线程汇总或接手时用)───
    if is_coding:
        out.append({"role": "system", "content": (
            "## Coding/debugging method\n"
            "For multi-step coding work, externalize a short todo list. Debugging quality comes from seeing the relevant whole file or subsystem, tracing writes/reads, then applying focused edits. Use code_index for first-pass orientation in large projects and read_function only after the target function is known.\n"
            "\n编码调试先建立全局上下文和 todo，再基于证据定位根因。"
        )})

    return out


# 2026-05-20 重构: 负反馈"复盘 mode"提示构造,从 orchestrate() 内联块原样抽出。
# 纯函数;debug.log 留在调用方。返回 (feedback_text, bot_log_found),text 为空=未触发。
def build_feedback_retry_text(message: str, hot_user) -> tuple[str, bool]:
    """构造负反馈复盘提示(行为与原内联块逐字符一致)。"""
    if not (is_negative_feedback(message) and hot_user):
        return "", False
    # hot_user 是 list[HotMessage](attribute access),找最近一条 assistant 且含 <bot_log>
    last_bot_log_excerpt = ""
    for hm in reversed(hot_user[-6:]):  # 只看最近 3 轮(user+assistant 交替)
        try:
            role = hm.role
            content = hm.content or ""
        except AttributeError:
            # 防御性兜底:如果 HotMessage schema 改了 / 是 dict-like
            role = getattr(hm, "role", None) or (hm.get("role") if hasattr(hm, "get") else None)
            content = (
                getattr(hm, "content", None)
                or (hm.get("content") if hasattr(hm, "get") else None)
                or ""
            )
        if role == "assistant" and "<bot_log>" in content:
            # 抽 <bot_log>...</bot_log> 内的内容,最多 600 字
            _start = content.find("<bot_log>") + len("<bot_log>")
            _end = content.find("</bot_log>", _start)
            if _end > _start:
                last_bot_log_excerpt = content[_start:_end].strip()[:600]
            break

    _feedback_text = (
        "## User feedback review mode\n"
        f"The current user message contains negative feedback or a redo request. The previous reply may have a concrete problem.\n"
    )
    if last_bot_log_excerpt:
        _feedback_text += (
            f"\nPrevious execution record (bot_log):\n```\n{last_bot_log_excerpt}\n```\n"
            "Use this bot_log to locate the concrete issue: missing deliverables, unsupported data, aborts, stuck helpers, or a wrong direction.\n"
        )
    else:
        _feedback_text += "(No previous bot_log was found; this may be first feedback or an easy-path reply.)\n"
    _feedback_text += (
        "\nThis turn should acknowledge concrete mistakes when the user named them, verify or retrieve corrected facts when needed, ask a targeted clarification when the feedback is vague, and change the strategy indicated by the previous failure mode.\n"
        "Keep the apology concise and focus on fixing or clarifying the issue.\n"
        "\n负反馈复盘提示，先看 bot_log 找具体问题，再修正或澄清。"
    )
    return _feedback_text, bool(last_bot_log_excerpt)
