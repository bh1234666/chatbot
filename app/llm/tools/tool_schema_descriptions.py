"""Central model-visible descriptions for tool JSON schemas.

The schema objects live in tool_schemas.py for compatibility. This module owns
the final English-first descriptions and short Chinese summaries applied to
those schemas at import time.
"""
from __future__ import annotations


def _append_cn_summary(text: str, summary: str) -> str:
    return f"{text.rstrip()}\n\n{summary.strip()}"


def _set_tool_description(schema: dict, text: str, summary: str) -> None:
    schema["function"]["description"] = _append_cn_summary(text, summary)


def _set_prop_description(schema: dict, prop: str, text: str, summary: str) -> None:
    props = schema.get("function", {}).get("parameters", {}).get("properties", {})
    if prop in props:
        props[prop]["description"] = _append_cn_summary(text, summary)


def _set_nested_prop_description(schema: dict, array_prop: str, prop: str, text: str, summary: str) -> None:
    props = schema.get("function", {}).get("parameters", {}).get("properties", {})
    item_props = props.get(array_prop, {}).get("items", {}).get("properties", {})
    if prop in item_props:
        item_props[prop]["description"] = _append_cn_summary(text, summary)


def _apply_english_schema_descriptions() -> None:
    """Normalize model-facing tool descriptions to English with short Chinese summaries."""

    _set_tool_description(
        PYTHON_TOOL_SCHEMA,
        (
            "Run small Python snippets in an isolated calculator sandbox. Workspace files are outside this sandbox: "
            "its current directory is temporary, so filesystem scans only see sandbox-local files. Use this for pure "
            "calculation, parsing short literals, or quick transformations with the standard library. For workspace "
            "files, third-party libraries, CSV/PNG/DOCX IO, or saved artifacts, write a script into the workspace and "
            "run it with the workspace tool instead. The last expression is returned as `result`, and `print()` output "
            "is returned as `stdout`. Limits: CPU 10 seconds, memory 256 MB, no file IO, no network."
        ),
        "小型 Python 计算沙箱；看不到工作区文件，涉及文件或第三方库时改用 workspace。",
    )
    _set_prop_description(PYTHON_TOOL_SCHEMA, "code", "Python code to execute.", "要执行的 Python 代码。")

    _set_tool_description(
        EXPAND_WARM_SCHEMA,
        (
            "Expand warm-memory entries by ID. Use this when an indexed warm-memory headline looks relevant and you "
            "need its summary, hints, entities, or tendencies before replying. Expand only entries that can affect the "
            "current answer. IDs are opaque handles from the current memory index; semantic guesses such as topic names "
            "are not evidence IDs and return empty results."
        ),
        "按当前记忆索引里的真实 ID 展开温记忆；主题词猜测不是 ID。",
    )
    _set_prop_description(EXPAND_WARM_SCHEMA, "ids", "Warm-memory IDs such as `w_xxx`.", "温记忆 ID 列表。")
    _set_tool_description(
        EXPAND_COLD_SCHEMA,
        (
            "Expand cold-memory graph nodes by ID. Depth 1 returns direct neighbors; depth 2 returns two-hop neighbors "
            "for deeper investigation when the current task needs long-term facts. IDs are opaque handles from the "
            "current memory index; semantic guesses such as topic names are not evidence IDs and return empty results."
        ),
        "按当前记忆索引里的真实 ID 展开冷记忆；主题词猜测不是 ID。",
    )
    _set_prop_description(EXPAND_COLD_SCHEMA, "ids", "Cold-memory node IDs such as `c_xxx`.", "冷记忆节点 ID 列表。")
    _set_prop_description(EXPAND_COLD_SCHEMA, "depth", "Graph traversal depth, usually 1 or 2.", "邻居遍历深度。")
    _set_tool_description(
        EXPAND_KB_SCHEMA,
        (
            "Expand knowledge-base nodes from compressed shared history. Use it like cold memory, scoped to the KB. "
            "Use concrete KB IDs from the current index; semantic guesses such as topic names are not evidence IDs."
        ),
        "用当前索引里的真实 KB ID 展开知识库节点；主题词猜测不是 ID。",
    )
    _set_tool_description(
        MARK_AVOID_SCHEMA,
        (
            "Record that the user explicitly prefers reduced proactive mention of a topic. The underlying memories "
            "remain available for direct user questions; this only changes proactive mention behavior. Use it when the "
            "user clearly asks for a topic to stay out of proactive replies."
        ),
        "记录用户明确希望减少主动提及的主题，不删除记忆。",
    )
    _set_prop_description(MARK_AVOID_SCHEMA, "topics", "Natural-language topic descriptions for reduced proactive mention.", "希望减少主动提及的主题。")
    _set_prop_description(MARK_AVOID_SCHEMA, "reason", "Optional user-provided reason or context.", "可选原因或上下文。")

    _set_tool_description(
        WORKSPACE_TOOL_SCHEMA,
        (
            "Lightweight persistent workspace operations: create directories, write small files, run bounded commands, "
            "and locate files. The main process uses this for orchestration, inspection, and very small checks. "
            "Substantial implementation, charting, Office work, OCR, TTS, and long analysis are helper-owned in the "
            "normal workflow. In environment project work, this tool works in the chat workspace; real project files are handled "
            "through env tools. `_env/...` paths are staged copies supplied for inspection or editing; project creation "
            "uses env_apply_create. Existing project files follow env_fetch -> edit_file/multi_edit/insert_in_file on the "
            "staged copy -> env_diff -> env_apply_replace. New project files should be created with env_apply_create "
            "after confirming the target is absent, using direct content for text files or a workspace_path for prepared "
            "artifacts; env_apply_create creates parent directories for file targets. Use workspace.mkdir only for "
            "temporary chat-workspace artifact folders, not for intended project folders. Always provide `action`; for "
            "`run`, pass a realistic `timeout_sec`."
        ),
        "轻量工作区工具；项目模式下它操作聊天工作区，真实项目新文件走 env_apply_create。",
    )
    _set_prop_description(
        WORKSPACE_TOOL_SCHEMA,
        "action",
        "Required action: `mkdir`, `write`, `run`, or `locate`. In environment project work, `mkdir` creates chat-workspace folders only.",
        "必填操作类型；项目模式下 mkdir 只建聊天工作区目录。",
    )
    _set_prop_description(
        WORKSPACE_TOOL_SCHEMA,
        "path",
        "Workspace-relative path. In environment project work, `_env/...` is a staged copy path; real project operations use env tools.",
        "工作区相对路径；项目模式下 _env 是暂存副本路径。",
    )
    _set_prop_description(
        WORKSPACE_TOOL_SCHEMA,
        "content",
        "File content for `write`. Existing staged `_env/...` project copies should be edited through staged-file edit tools.",
        "写入内容；已有 _env 项目副本应通过暂存文件编辑工具修改。",
    )
    _set_prop_description(WORKSPACE_TOOL_SCHEMA, "command", "Shell command for `run`.", "要运行的命令。")
    _set_prop_description(WORKSPACE_TOOL_SCHEMA, "timeout_sec", "Maximum runtime in seconds for `run`; default is intentionally short.", "命令超时时间。")
    _set_prop_description(WORKSPACE_TOOL_SCHEMA, "pattern", "Filename or glob pattern for `locate`.", "文件名或 glob 匹配。")

    _set_tool_description(
        OCR_WORKSPACE_TOOL_SCHEMA,
        (
            "Read-helper-only text writer. Use it to save extracted source evidence, visual text evidence, uncertainty notes, quality observations, "
            "and suggested line ranges into `.txt` files. These files are internal source material, not user-facing copy."
        ),
        "read helper 专用，只写内部证据 txt。",
    )
    _set_prop_description(OCR_WORKSPACE_TOOL_SCHEMA, "action", "Fixed value: `write`.", "固定为写入文本材料。")
    _set_prop_description(OCR_WORKSPACE_TOOL_SCHEMA, "path", "Relative `.txt` path for the internal evidence file.", "内部证据 txt 相对路径。")
    _set_prop_description(
        OCR_WORKSPACE_TOOL_SCHEMA,
        "content",
        "Confirmed text, uncertain text, quality notes, and suggested line ranges for the main thread.",
        "可确认内容、不确定内容、质量备注和建议行号。",
    )
    _set_tool_description(
        TTS_WORKSPACE_TOOL_SCHEMA,
        (
            "TTS helper scoped text-material writer. Use only for short internal transcript or manifest notes; "
            "audio generation itself uses the dedicated TTS tool; this writer only saves text notes."
        ),
        "tts helper 专用内部文本写入工具，仅写文本材料。",
    )
    _set_prop_description(TTS_WORKSPACE_TOOL_SCHEMA, "action", "Fixed value: `write`.", "固定为写入文本材料。")
    _set_prop_description(TTS_WORKSPACE_TOOL_SCHEMA, "path", "Relative `.txt` path for an internal TTS note.", "内部 TTS 文本材料相对路径。")
    _set_prop_description(TTS_WORKSPACE_TOOL_SCHEMA, "content", "Short transcript, manifest, or quality note for the main thread.", "给主线程看的短文本材料。")
    _set_tool_description(
        SEARCH_FILES_SCHEMA,
        "Search the file knowledge index by keyword and return matching filenames, descriptions, and workspace paths.",
        "按关键词搜索文件知识索引。",
    )
    _set_prop_description(SEARCH_FILES_SCHEMA, "query", "Search keywords matched against file names and descriptions.", "搜索关键词。")
    _set_prop_description(SEARCH_FILES_SCHEMA, "limit", "Maximum number of results to return.", "返回结果数量上限。")
    _set_tool_description(
        FETCH_GROUP_FILE_SCHEMA,
        "Fetch a referenced indexed/shared-file node into the workspace and return the concrete workspace path to use.",
        "把索引文件节点取到工作区，返回实际路径。",
    )
    _set_prop_description(FETCH_GROUP_FILE_SCHEMA, "kb_node_id", "Target shared-file node ID from the file index.", "共享文件节点 ID。")
    _set_tool_description(
        INSPECT_FILE_SCHEMA,
        (
            "Inspect a workspace file without reading it as plain text. Use this for Office documents, PDFs, images, "
            "archives, audio/video, or any file where structure and metadata matter."
        ),
        "检查文件结构和元数据，适合二进制或结构化文件。",
    )
    _set_prop_description(INSPECT_FILE_SCHEMA, "path", "Workspace-relative file path.", "工作区相对文件路径。")
    _set_tool_description(
        READ_FILE_SCHEMA,
        (
            "Read a text file or a selected line range from the workspace. Helpers may use this for source and evidence "
            "reading. The main process has a stricter read schema and should normally delegate broad reading. For "
            "Office/PDF/images/media, inspect first and use the suggested specialized tool."
        ),
        "读取文本文件，可按行段读取；结构化文件先 inspect。",
    )
    _set_prop_description(READ_FILE_SCHEMA, "path", "Workspace-relative text file path.", "工作区相对文本路径。")
    _set_prop_description(READ_FILE_SCHEMA, "start_line", "Starting line number, 1-indexed.", "起始行号。")
    _set_prop_description(READ_FILE_SCHEMA, "end_line", "Ending line number, inclusive; -1 means end of file.", "结束行号。")
    _set_prop_description(READ_FILE_SCHEMA, "max_chars", "Maximum characters to return.", "返回字符上限。")
    _set_tool_description(
        EDIT_FILE_SCHEMA,
        (
            "Replace one exact text span in a plain text file. Use it for precise small edits where `old_str` appears "
            "exactly `expected_count` times. `old_str` is literal file text, not the JSON-escaped representation of a "
            "tool result; backslashes before quotes belong only when the file itself contains them. For broad rewrites "
            "or repeated failed edits, re-read the relevant region and use a better edit strategy."
        ),
        "精确替换纯文本片段；old_str 是文件原文，不是 JSON 转义文本。",
    )
    _set_prop_description(
        EDIT_FILE_SCHEMA,
        "path",
        "Workspace-relative text file path. In environment project helpers, edit the staged `_env/...` copy, not the bare project-relative path.",
        "文件相对路径；项目 helper 编辑已暂存的 `_env/...` 副本。",
    )
    _set_prop_description(EDIT_FILE_SCHEMA, "old_str", "Exact text to replace, including whitespace and newlines.", "要替换的精确文本。")
    _set_prop_description(EDIT_FILE_SCHEMA, "new_str", "Replacement text; an empty string deletes the matched span.", "替换后的新文本。")
    _set_prop_description(EDIT_FILE_SCHEMA, "expected_count", "How many times `old_str` must appear.", "期望匹配次数。")
    _set_tool_description(
        INSERT_IN_FILE_SCHEMA,
        "Insert text into a plain text file at a specified line position. Prefer edit tools for replacement scenarios.",
        "按行位置插入文本，替换场景用 edit。",
    )
    _set_prop_description(
        INSERT_IN_FILE_SCHEMA,
        "path",
        "Workspace-relative text file path. In environment project helpers, insert into the staged `_env/...` copy, not the bare project-relative path.",
        "文件相对路径；项目 helper 使用已暂存的 `_env/...` 副本。",
    )
    _set_prop_description(INSERT_IN_FILE_SCHEMA, "after_line", "Insert after this line number; 0 means beginning, -1 means end.", "插入位置行号。")
    _set_prop_description(INSERT_IN_FILE_SCHEMA, "content_to_insert", "Text to insert.", "要插入的内容。")
    _set_tool_description(
        MULTI_EDIT_SCHEMA,
        "Apply multiple exact text replacements atomically to a plain text file. Each old_str is literal file text, not JSON-escaped tool-result text.",
        "一次性原子应用多个精确替换；old_str 是文件原文，不是 JSON 转义文本。",
    )
    _set_prop_description(
        MULTI_EDIT_SCHEMA,
        "path",
        "Workspace-relative target for the atomic edit batch; environment project helpers use the staged `_env/...` copy for coordinated replacements, not a bare project-relative path.",
        "原子批量替换目标；项目 helper 使用 `_env/...` 暂存副本，不用裸项目路径。",
    )
    _set_prop_description(MULTI_EDIT_SCHEMA, "edits", "Ordered exact replacements to apply atomically.", "按顺序原子应用的替换列表。")
    _set_nested_prop_description(
        MULTI_EDIT_SCHEMA,
        "edits",
        "old_str",
        "Exact literal file text to replace; do not copy JSON escape backslashes unless they exist in the file.",
        "要替换的文件原文；不要复制 JSON 转义反斜杠。",
    )
    _set_nested_prop_description(MULTI_EDIT_SCHEMA, "edits", "new_str", "Replacement text; an empty string deletes the matched span.", "替换后的新文本。")
    _set_nested_prop_description(MULTI_EDIT_SCHEMA, "edits", "expected_count", "How many times `old_str` must appear.", "期望匹配次数。")
    _set_tool_description(
        SEARCH_IN_FILE_SCHEMA,
        (
            "Search inside plain text files for literal text or a Python regular expression. Use this for source "
            "code, Markdown, TXT, CSV/TSV, JSON/YAML, and logs. For Office/PDF/image/binary containers, first use "
            "`inspect_file`; use `office(action='read')` for DOCX/PPTX/XLSX body content and OCR/extraction tools "
            "for PDF or images. If this tool returns `binary_or_structured_file_not_readable_as_text`, switch to "
            "the suggested structured reader instead of retrying the same search."
        ),
        "只搜索纯文本；Office/PDF/图片先 inspect，再用 office/OCR 等结构化读取。",
    )
    _set_prop_description(SEARCH_IN_FILE_SCHEMA, "path", "Workspace-relative text file path.", "文件相对路径。")
    _set_prop_description(SEARCH_IN_FILE_SCHEMA, "pattern", "Literal search text or regular expression pattern.", "搜索文本或正则模式。")
    _set_prop_description(SEARCH_IN_FILE_SCHEMA, "is_regex", "Whether `pattern` is a regular expression.", "是否按正则匹配。")
    _set_prop_description(SEARCH_IN_FILE_SCHEMA, "max_results", "Maximum number of matches to return.", "最大匹配数量。")
    _set_tool_description(
        CODE_INDEX_SCHEMA,
        (
            "Build a compact outline of source files, including definitions and imports. Use it before targeted "
            "function reads in unfamiliar source files, broad reviews, or large projects. Treat the returned symbol "
            "names as evidence; if the target is not listed, search or read the relevant file section instead of "
            "guessing symbol names."
        ),
        "先建立源码符号索引；只根据已确认符号继续精读。",
    )
    _set_prop_description(CODE_INDEX_SCHEMA, "path", "Workspace-relative source file path.", "工作区相对源码路径。")
    _set_prop_description(CODE_INDEX_SCHEMA, "include_includes", "Whether to include import/include statements.", "是否包含导入列表。")
    _set_prop_description(CODE_INDEX_SCHEMA, "name_filter", "Optional symbol-name filter, glob or regex.", "可选符号名过滤。")
    _set_prop_description(CODE_INDEX_SCHEMA, "kinds", "Optional symbol kinds to include.", "可选符号类型过滤。")
    _set_tool_description(
        READ_FUNCTION_SCHEMA,
        (
            "Read one supported source-code function by an exact, evidence-backed symbol name. Use this after "
            "`code_index`, `search_in_file`, or a file read has confirmed the function exists in that file. It is a "
            "precision tool for known functions; discovery uses indexes, search, or targeted file ranges. When the function is not found, switch to "
            "`code_index`, search, or a relevant file range and update the hypothesis before calling this again."
        ),
        "只用于已确认存在的函数名；找不到时先回到索引/搜索/片段阅读。",
    )
    _set_prop_description(READ_FUNCTION_SCHEMA, "path", "Workspace-relative source file path.", "工作区相对源码路径。")
    _set_prop_description(
        READ_FUNCTION_SCHEMA,
        "function_name",
        "Exact function name already confirmed by index, search, or file evidence.",
        "由索引、搜索或文件证据确认过的精确函数名。",
    )
    _set_prop_description(READ_FUNCTION_SCHEMA, "include_xref", "Whether to include caller/callee references.", "是否包含调用关系。")
    _set_prop_description(READ_FUNCTION_SCHEMA, "xref_scope", "Caller/callee reference scope: current file or whole workspace.", "调用关系搜索范围。")
    _set_tool_description(
        SEARCH_ACROSS_FILES_SCHEMA,
        "Search across workspace text files with optional glob filtering and regex mode.",
        "跨文件搜索文本或正则。",
    )
    _set_prop_description(SEARCH_ACROSS_FILES_SCHEMA, "pattern", "Literal search text or regular expression pattern.", "搜索文本或正则模式。")
    _set_prop_description(SEARCH_ACROSS_FILES_SCHEMA, "file_glob", "Optional filename glob filter.", "可选文件名通配符。")
    _set_prop_description(SEARCH_ACROSS_FILES_SCHEMA, "is_regex", "Whether `pattern` is a regular expression.", "是否按正则匹配。")
    _set_prop_description(SEARCH_ACROSS_FILES_SCHEMA, "max_results_per_file", "Maximum matches to return per file.", "单文件最大匹配数量。")
    _set_prop_description(SEARCH_ACROSS_FILES_SCHEMA, "max_files", "Maximum number of matching files to scan.", "最多扫描文件数。")

    _set_tool_description(
        AGENT_STATE_SCHEMA,
        (
            "Maintain the structured task ledger for complex work. Use it to write or read task contracts, evidence, "
            "artifact manifests, resource waits, and status snapshots. Record only compact facts backed by tools, "
            "helper reports, or explicit main-process checks. This is the shared source of truth for long toolchains; "
            "failed or partial records remain visible as recovery state until verified completion. For resource waits, "
            "first call `action='status'` and read `resource_requests[].request_id`; update a resource request only when "
            "you have that request_id and a concrete status decision. Producing and verifying a requested file can be "
            "recorded as evidence or an artifact without forcing a resource-request update first. This ledger is internal "
            "structured state; it is not a user-visible note file, memory file, or handoff artifact. When a user explicitly "
            "asks to store facts as memory, notes, or later handoff, record each compact fact here as evidence or a "
            "contract in addition to any requested or naturally needed visible note/handoff file; neither channel replaces the other."
        ),
        "结构化任务账本，不替代可见笔记/交接文件；后续记忆事实入 ledger 的同时保留需要的可见产物。",
    )
    _set_prop_description(
        AGENT_STATE_SCHEMA,
        "action",
        "Ledger operation. Use `status` before `update_resource_request` so you can supply the correct request_id.",
        "账本操作；更新资源请求前先 status。",
    )
    _set_prop_description(AGENT_STATE_SCHEMA, "task_id", "Task or helper identifier related to this entry.", "相关任务 ID。")
    _set_prop_description(AGENT_STATE_SCHEMA, "goal", "Task goal for a contract.", "任务目标。")
    _set_prop_description(AGENT_STATE_SCHEMA, "acceptance", "Acceptance points that define completion.", "验收点列表。")
    _set_prop_description(AGENT_STATE_SCHEMA, "evidence_required", "Evidence needed before final delivery.", "交付前需要的证据。")
    _set_prop_description(AGENT_STATE_SCHEMA, "deliverables", "Expected user-facing deliverable files or outputs.", "预期面向用户的产物。")
    _set_prop_description(AGENT_STATE_SCHEMA, "risks", "Known risks or uncertainty sources.", "已知风险。")
    _set_prop_description(AGENT_STATE_SCHEMA, "current_stage", "Current stage such as map, read, modify, verify, or report.", "当前阶段。")
    _set_prop_description(AGENT_STATE_SCHEMA, "source", "Evidence source such as command, helper, inspect_file, or manual_check.", "证据来源。")
    _set_prop_description(AGENT_STATE_SCHEMA, "status", "Evidence/artifact status.", "证据或产物状态。")
    _set_prop_description(AGENT_STATE_SCHEMA, "summary", "Short factual summary.", "简短事实摘要。")
    _set_prop_description(AGENT_STATE_SCHEMA, "kind", "Evidence, helper, or artifact kind.", "类型。")
    _set_prop_description(AGENT_STATE_SCHEMA, "data", "Compact structured data supporting the summary.", "紧凑结构化数据。")
    _set_prop_description(AGENT_STATE_SCHEMA, "path", "Artifact path.", "产物路径。")
    _set_prop_description(AGENT_STATE_SCHEMA, "artifact_type", "Artifact type such as report, code, audio, chart, image, or data.", "产物类型。")
    _set_prop_description(AGENT_STATE_SCHEMA, "created_by", "Creator identifier for an artifact.", "产物创建者。")
    _set_prop_description(AGENT_STATE_SCHEMA, "verified_by", "Verifier identifier for an artifact.", "产物验证者。")
    _set_prop_description(AGENT_STATE_SCHEMA, "evidence_ids", "Evidence IDs supporting an artifact.", "支持产物的证据 ID。")
    _set_prop_description(AGENT_STATE_SCHEMA, "metadata", "Compact artifact metadata.", "产物元数据。")
    _set_prop_description(
        AGENT_STATE_SCHEMA,
        "request_id",
        "Resource request ID from a prior `status` result; required for add_resource_task and update_resource_request.",
        "从 status 结果取得的资源请求 ID。",
    )
    _set_prop_description(AGENT_STATE_SCHEMA, "resource_task_id", "Resource-producing helper task ID.", "资源 helper 任务 ID。")
    _set_prop_description(AGENT_STATE_SCHEMA, "reason", "Decision reason for marking a resource request ready, refused, or closed.", "资源请求状态决策原因。")
    _set_prop_description(AGENT_STATE_SCHEMA, "satisfied_by", "Concrete resource paths that satisfy a resource request.", "满足资源请求的路径。")

    _set_tool_description(
        TASK_PLAN_SCHEMA,
        (
            "Maintain the active task plan snapshot for the main process. Use it after reading memory, files, "
            "continued toolchain context, or agent_state when those facts clarify or change the active task beyond "
            "the current turn text. This updates the thread plan and mirrors compact facts into agent_state; it "
            "does not verify artifacts or replace final JSON. Updates retain prior acceptance/evidence facts for "
            "the same task unless the model states task-change evidence in the plan."
        ),
        "维护当前主线任务快照；读取记忆、文件或续作证据后可更新；同任务旧验收事实会保留。",
    )
    _set_prop_description(TASK_PLAN_SCHEMA, "action", "Read or update the active task plan snapshot.", "读取或更新当前任务快照。")
    _set_prop_description(TASK_PLAN_SCHEMA, "goal", "Resolved active task goal.", "解析后的当前主线任务目标。")
    _set_prop_description(TASK_PLAN_SCHEMA, "key_points", "Compact current-task facts or acceptance notes.", "当前任务事实或验收要点。")
    _set_prop_description(TASK_PLAN_SCHEMA, "deliverables", "Expected current-task user-facing deliverables, if known.", "预期本任务交付物。")
    _set_prop_description(TASK_PLAN_SCHEMA, "acceptance", "Checkable completion criteria for the active task. Omitted prior criteria for the same task remain visible in agent_state for comparison.", "可检查验收标准；同任务未重复的旧验收仍保留供对照。")
    _set_prop_description(
        TASK_PLAN_SCHEMA,
        "evidence_required",
        (
            "Evidence needed before finalizing this active task. This records final evidence needs, not who must collect them. "
            "For coding/debugging, code helpers can satisfy source reading, failure diagnosis, edits, and tests through input_files and acceptance_checks; "
            "the main thread can keep final diff/apply and acceptance evidence compact."
        ),
        "最终交付前所需证据；记录证据需求，不表示必须由主进程收集。",
    )
    _set_prop_description(TASK_PLAN_SCHEMA, "risks", "Known ambiguity or stale-context risks.", "已知歧义或旧上下文风险。")
    _set_prop_description(TASK_PLAN_SCHEMA, "current_stage", "Current active-task stage.", "当前阶段。")
    _set_prop_description(TASK_PLAN_SCHEMA, "reason", "Brief factual reason for the update.", "更新原因。")


    _set_tool_description(
        DELEGATE_TOOL_SCHEMA,
        (
            "Spawn, monitor, resume, collect, or interrupt helper tasks in isolated workspaces. Use helpers for "
            "substantial implementation, debugging, benchmarks, data analysis, source-material reading, Office document assembly, charts, TTS, "
            "verification, and independent parallel exploration. The main process remains the coordinator: it sets "
            "goals, manages dependencies, waits or resumes based on heartbeats, validates outputs, and integrates "
            "results. Use `kind` for task nature (`code`, `read`, `edit`, `draw`, `tts`, `verify`, or project "
            "analysis kinds) and `mode` for difficulty (`easy` or `hard`). Reuse the same `task_id` with `resume=true` "
            "for interrupted work, missing output, same-deliverable repair, or a stronger retry of useful work. Keep "
            "the same base kind when resuming; upgrade `mode` when the task needs stronger reasoning or recovery. "
            "Create a new `task_id` only when the work boundary is genuinely different or a fresh workspace is required. "
            "Use `fork_from` to branch from a completed helper workspace. Frozen or "
            "resource-waiting helpers should be resumed only after the main process has supplied or refused the named "
            "resource. Failed, stuck, interrupted, partial, or resource-waiting helper reports are recovery-state "
            "evidence until verified completion. For broad project understanding, prefer one project_map or inventory-style "
            "helper first, then add file_summary or impact_review helpers for specific files or risks discovered by "
            "that map. Read helpers may expose internal evidence through `internal_evidence_files`, "
            "`main_available_files`, or `copy_stats.env_copied_files`; read those main-workspace paths when more "
            "detail is needed, and treat helper sandbox paths in prose as non-authoritative. For review-only requests, "
            "ask helpers for evidence, risks, and concrete low-risk proposals; "
            "start implementation helpers only after the user request or accepted plan requires actual changes. For "
            "large multi-part work, create or obtain a compact shared framework contract before broad fan-out: interface, "
            "schema, outline, evidence/source map, validation plan, ownership boundaries, merge order, and an exact "
            "output matrix for each downstream helper: task_id, kind, mode, input_files, expected_outputs, acceptance "
            "checks, and final merge/apply target. The first framework helper owns compact structural outputs: "
            "a canonical contract or manifest, downstream output matrix, file list, interface/schema, command plan, acceptance checks, and any minimal interface/skeleton. "
            "Support utilities, fixtures, generated data helpers, package glue, and acceptance scripts belong to later bounded helpers "
            "unless the whole task is a tiny vertical slice. The contract defines slots and acceptance; later bounded helpers own implementation bodies, "
            "long scripts, experiments, evidence-backed analysis, research claims, citations, chapters, tables with final values, charts, "
            "and final document assembly. Then spawn "
            "bounded slice helpers with the task-level `framework` field filled, and fan in their verified artifacts instead "
            "of assigning the entire deliverable to one oversized helper. For one ultra-large file, long log, or long source material that needs broad coverage, "
            "fan out focused `read` or `file_summary` helpers by concrete line ranges, chapters, pages, headings, or natural sections; require coverage summaries, "
            "gaps, evidence paths, and merge anchors from each slice. Long code, documents, tables, reports, OCR/source evidence, and analysis outputs "
            "should be produced in inspectable segments that can be resumed and merged. When many raw source files, Office/PDF/image materials, "
            "archive folders, or long text sources must be read, first fan out focused `read` helpers by source group or batch and let later "
            "`code` or `edit` helpers consume their evidence; keep the raw reading phase with read helpers even when scripts are mentioned. "
            "When a final text/Markdown/Office artifact depends on a small bounded set of explicit `input_files`, one `edit` helper may read those files directly and assemble the final artifact; use separate read helpers when extraction is broad, long, visual, uncertain, or reusable. "
            "Every spawned task should be a compact structured request envelope. Keep `prompt` to the helper's local mission, slice boundary, "
            "input references, output paths, checks, and recovery conditions. Use `framework` for shared contracts, `input_files` for files or manifests, "
            "and `expected_outputs` for owned paths. State required output format, evidence, and validation facts; do not preselect an "
            "implementation route such as python-docx, custom scripts, or a helper-internal tool unless the user explicitly requires it or verified limitations make it necessary. "
            "Full implementation source, long document prose, large benchmark scripts, complete tables, "
            "and copied file bodies belong in helper-owned workspace outputs that the helper authors and verifies. "
            "When the user-visible deliverable is a paper, report, or Office/PDF file, code, benchmark, and analysis helpers produce supporting evidence; "
            "after sufficient evidence exists, delegate `edit` for final document assembly instead of continuing implementation expansion. "
            "`_helpers_shared/...` files are handoff evidence for downstream helpers. Final project deliverables or user-facing artifacts "
            "must be assembled into clean non-shared workspace files or staged `_env/...` project paths before acceptance. "
            "In environment project work, project validators and check scripts inspect the real project/workspace state; a bare helper output filename is chat-workspace state, while `_env/<project-relative-path>` is a staged project output for main acceptance and apply. "
            "long tasks, set `wait_window_sec` on spawn/collect/wait_any and decide from returned progress whether to wait, resume, collect, or "
            "cooperatively interrupt."
        ),
        "统一派发和管理 helper；超大单文件可按范围/章节等派多个 helper；源码正文、长文和完整表格由 helper 在工作区生成。",
    )
    _set_prop_description(DELEGATE_TOOL_SCHEMA, "action", "Operation: spawn/spawn_async, poll, collect, wait_any, kill, or status. Poll is an immediate heartbeat query; use collect or wait_any to wait.", "helper 管理操作。")
    _set_prop_description(DELEGATE_TOOL_SCHEMA, "task_ids", "Task IDs for poll, collect, wait_any, or batch kill.", "要查询或操作的 task_id 列表。")
    _set_prop_description(DELEGATE_TOOL_SCHEMA, "tasks", "Task list for spawning helpers. Each task needs at least `task_id` and `prompt`.", "要派发的 helper 任务列表。")
    _set_nested_prop_description(
        DELEGATE_TOOL_SCHEMA,
        "tasks",
        "task_id",
        (
            "Short semantic task identifier. Reuse the same value with `resume=true` when continuing, repairing, "
            "or strengthening the same deliverable or subtask. Choose a new ID only for a different work boundary."
        ),
        "语义化任务 ID；同一任务续作、修复和升级复用它。",
    )
    _set_nested_prop_description(
        DELEGATE_TOOL_SCHEMA,
        "tasks",
        "prompt",
        (
            "Focused helper instructions: goal, required inputs, deliverables, validation points, and relevant constraints. "
            "Keep it concise and self-contained; pass only context needed for this helper. Include concrete paths, expected "
            "outputs, evidence standards, and recovery instructions when the helper may need resources. For framework-first "
            "work, include this helper's slice boundary, segment output names, local checks, and the gaps it should report instead "
            "of absorbing another slice. Put the shared framework contract text itself in the `framework` field. For the first framework "
            "helper, request compact structural outputs that downstream helpers can consume, such as a contract, manifest, output matrix, or minimal skeleton; "
            "ask later slice helpers to own support utilities, fixtures, glue files, acceptance scripts, final-value tables, citations, conclusions, long prose, implementation bodies, and benchmark results. "
            "Write the request as a structured helper request envelope covering goal, slice boundary, inputs, outputs, checks, and recovery. "
            "Use it for concise behavior and specification detail. Full source code, long prose, file bodies, complete benchmark scripts, "
            "and filled tables belong in the helper's workspace outputs. "
            "For source files that can be named, put the paths in `input_files` and do not paste their bodies into this field. For long file lists or repeated instructions, put the list in a workspace manifest or `input_files` and keep this field compact. "
            "When source/test paths are available through `input_files`, a root-cause analysis is not required in this field; state the goal and acceptance facts and let the helper read, diagnose, edit, and test. "
            "If the main thread already read a source body, summarize only the fact needed for routing or acceptance instead of copying the complete body. "
            "State required output format, evidence, and validation facts; do not preselect an implementation route such as python-docx, custom scripts, "
            "or a helper-internal tool unless the user explicitly requires it or verified limitations make it necessary. "
            "For structured source fields, pass raw field names, values, notes, and acceptance constraints. If a value is paired with counts, units, durations, quantities, booleans, or risk flags, state the ambiguity and ask the helper to compute from evidence; total/package/safe/satisfied interpretations need explicit source wording. "
            "Use `code` for project scaffold/source/script/benchmark files or technical framework/spec contracts that need workspace writes or commands. "
            "Use `edit` for final user-facing documents and prose/report outlines or contracts assembled from verified evidence. "
            "Choose a concrete helper kind whenever expected_outputs require a workspace file."
        ),
        "prompt 是短请求信封；有 input_files 时无需先写根因分析，不默认指定实现工具，已读源码也只摘要必要事实，结构化源字段保留原始字段和值，完整源码、长文和表格作为 helper 产物输出。",
    )
    _set_nested_prop_description(
        DELEGATE_TOOL_SCHEMA,
        "tasks",
        "framework",
        (
            "Shared framework contract for broad or multi-part work. The main process should create or obtain it before "
            "fan-out, then pass the relevant contract to each slice helper. Include goal, interfaces/schema, outline, "
            "evidence or source map, ownership boundary, validation checks, segment outputs, merge order, and the exact "
            "output matrix for downstream helpers: task_id, kind, mode, input_files, expected_outputs, acceptance checks, "
            "and final merge/apply target. The first framework helper owns only this structural contract; it defines slots, "
            "ownership, and acceptance rather than filling those slots. Keep it compact enough to paste into later helpers; keep substantive evidence, "
            "evidence-backed analysis, research claims, citations, final numeric values, citations, conclusions, implementation detail, and long section text in named producer outputs "
            "instead of this field. A prompt-only reference to a contract file is useful context; still include "
            "this field for each dependent slice. `_helpers_shared/...` files are handoff evidence; final deliverables and user-facing artifacts should be assembled into clean non-shared "
            "workspace files or `_env/...` project paths. In environment project work, include project-visible final targets in the output matrix as `_env/<project-relative-path>` rather than bare filenames when validators or project checks need to see them."
        ),
        "共享框架契约需紧凑可传递，只放结构、槽位、归属、依赖和验收；实质内容由分片产物承载。",
    )
    _set_nested_prop_description(
        DELEGATE_TOOL_SCHEMA,
        "tasks",
        "dispatch_reason",
        (
            "Main-thread reason for this exact helper boundary, kind, mode, and split choice. "
            "Use it after a guard block, or when the guard may reasonably question the plan, to state the factual justification in free-form language: "
            "why this boundary, kind, mode, framework, split, or retry is intentional. "
            "This field informs the task-quality guard; it does not override safety, resource, path, or guard decisions."
        ),
        "主进程给守卫的自由派发理由；说明当前派发事实，不绕过守卫。"
    )
    _set_nested_prop_description(
        DELEGATE_TOOL_SCHEMA,
        "tasks",
        "input_files",
        (
            "Concrete files, staged paths, source ranges, line ranges, page ranges, section labels, or artifacts transferred or expected to be readable by this helper. "
            "In environment project work, put likely project-relative paths here even when they are not yet staged; helper startup can stage exact `_env/...` copies. "
            "Use this instead of pasting full source bodies or long bug analysis into `prompt`; keep only files or ranges relevant to this helper's slice. "
            "Even if the main thread has already read a file body, the helper can reread the path from input_files, so prompt should carry compact facts rather than complete source text. "
            "For coding/debugging, file paths plus acceptance checks are usually enough for the helper to read, diagnose, edit, and test."
        ),
        "传给 helper 的具体文件、路径、行/页/章节范围或产物；项目模式可填项目相对路径以自动暂存，通常无需把源码正文或长分析塞进 prompt。",
    )
    _set_nested_prop_description(
        DELEGATE_TOOL_SCHEMA,
        "tasks",
        "acceptance_checks",
        (
            "Focused checks the helper should run or report against: compile/test commands, smoke tests, schema checks, coverage points, "
            "document sections, OCR completeness, or exact evidence requirements."
        ),
        "该 helper 需要执行或汇报的聚焦验收项。",
    )
    _set_nested_prop_description(
        DELEGATE_TOOL_SCHEMA,
        "tasks",
        "resume",
        (
            "Continue a preserved workspace for the same task. Use it for interrupted work, incomplete outputs, "
            "same-deliverable repairs, or escalation to a stronger mode after reading the prior result."
        ),
        "继续同一任务保留的工作区，用于中断、缺产物、修复或升级。",
    )
    _set_nested_prop_description(DELEGATE_TOOL_SCHEMA, "tasks", "fork_from", "Start from a copy of another completed helper workspace.", "从已完成 helper 工作区复制分支。")
    _set_nested_prop_description(
        DELEGATE_TOOL_SCHEMA,
        "tasks",
        "kind",
        (
            "Task nature. Use `code` for writing/fixing programs, HTML, scripts, complex data analysis, benchmarks, "
            "build/test interpretation, executable file-preparation steps before reading, and directory/source statistics "
            "that need commands or scripts. Use `read` for source-material reading, classification, labeling, triage, transcription, and evidence extraction from user "
            "materials, prepared archive contents, text files, images, PDFs, Office files, screenshots, forms, and scanned "
            "or visual content; script or library wording stays read when the script is only a reading method. For many source files, split into parallel read helpers by group or batch before downstream code/edit work. A bounded final text/Markdown/Office synthesis task with explicit `input_files` can be `edit` directly. Use `edit` for "
            "documents, prose/report sections assembled from verified evidence, and Office/PDF assembly. For document-delivery tasks, benchmark/code outputs are inputs to edit, not substitutes for the final document. Use `draw` for final image/chart files from data or specs, `verify` for checking "
            "existing code/images/documents, `tts` for audio generation, and "
            "`project_map`/`file_summary`/`impact_review` for project analysis; selected source/config file summaries "
            "in a code project stay `file_summary`. Use mode='hard' for stronger retries while "
            "preserving the same base kind; new work uses the supported base kinds."
        ),
        "任务类型；广泛材料读取优先 read 并按批并行，小型明确 input_files 可由 edit 直接组装最终文档，code 处理实现/计算/脚本产物。",
    )
    _set_nested_prop_description(
        DELEGATE_TOOL_SCHEMA,
        "tasks",
        "mode",
        (
            "Difficulty/resource mode: `easy` or `hard`. Mode changes reasoning strength, not task ownership or tool "
            "family. For a harder retry of useful work, keep the same task_id and base kind, set resume=true, and "
            "switch to hard only after the narrow task shows enough difficulty."
        ),
        "难度和资源档位；升级不改变 kind，同任务 hard 重试应续作。",
    )
    _set_nested_prop_description(
        DELEGATE_TOOL_SCHEMA,
        "tasks",
        "expected_outputs",
        (
            "Expected deliverable or staged project-copy paths the helper may produce or edit. In environment work, "
            "include every `_env/...` project file this helper is allowed to touch, including package init files, "
            "test glue, config, scripts, docs, reports, data files, and any file added during a same-task resume. When source/test/config "
            "paths are known and the helper is likely to edit them, include those targets here as owned staged outputs. "
            "When project validators or check scripts must see a produced artifact, declare the intended project-relative target as `_env/<path>`; a bare filename is only a chat-workspace artifact and is not project-verifier-visible until the main process creates or applies a project file. For `kind=read`, `.txt` expected_outputs are internal evidence and should normally be helper-local names such as `read_evidence.txt`; `_env/...` is reserved for staged project files and project-visible artifacts. "
            "`input_files` records readable inputs, while `expected_outputs` records produce/modify ownership for copyback and acceptance."
        ),
        "预期产物或允许编辑的 _env 项目文件清单；read 证据用内部 txt 名称；input_files 是可读输入，expected_outputs 是产出/修改归属。",
    )
    _set_prop_description(DELEGATE_TOOL_SCHEMA, "wait_window_sec", "Seconds to wait for spawn, collect, or wait_any before returning progress for still-running helpers. Ignored by poll.", "等待窗口秒数；poll 忽略。")
    _set_prop_description(DELEGATE_TOOL_SCHEMA, "min_results_to_return", "Return early once this many spawned helpers have completed.", "达到指定完成数量后提前返回。")
    _set_prop_description(DELEGATE_TOOL_SCHEMA, "task_id", "Target task ID for single-helper actions such as kill.", "单个目标 task_id。")
    _set_prop_description(DELEGATE_TOOL_SCHEMA, "reason", "Reason code for cooperative helper interruption.", "协作中断原因。")
    _set_prop_description(DELEGATE_TOOL_SCHEMA, "force", "Deprecated compatibility flag; helper interruption is cooperative.", "历史兼容参数。")
    _set_prop_description(DELEGATE_TOOL_SCHEMA, "force_blanket_resume", "Confirm an intentional resume of multiple active helpers.", "确认批量续作。")
    _set_prop_description(DELEGATE_TOOL_SCHEMA, "helper_think", "Enable low reasoning for spawned coding or verification helpers.", "为部分 helper 开启低档思考。")

    _set_tool_description(
        OFFICE_TOOL_SCHEMA,
        (
            "Read, write, edit, OCR embedded images, and verify Office documents. The file extension selects DOCX, "
            "PPTX, or XLSX, and each format has different valid actions. For DOCX, read/inspect body structure with "
            "`read`, edit with `write`, `append`, `replace_section`, `replace_block`, `replace_blocks`, `insert_block`, `delete_block`, "
            "`fill_empty_headings` for batch-populating existing empty DOCX headings, "
            "embed images with `image` blocks or `insert_image`, and verify data claims with `verify_numbers` or "
            "`verify_rigor`. `verify_integrity` is XLSX-only and is not a DOCX validity check. Valid DOCX block types are `heading`, `paragraph`, "
            "`list`, `table`, `image`, `equation`, and `page_break`; plain prose is `paragraph`, subtitles are also `paragraph`, bullets are `list`, "
            "and Table rows must contain at least one non-empty cell in a 2D array such as rows:[[\"A\",\"B\"],[\"C\",\"D\"]]. Image blocks require an existing workspace `path`. "
            "For targeted DOCX edits, call `read` first and use the exact non-negative `block_index` from the "
            "read output. A successful DOCX read returns headings plus paragraph/block, table, and image counts; "
            "repeat reads are useful when the artifact changed or a named block range/detail remains unchecked. For large documents, page body text with `start_block`/`max_blocks`, OCR embedded images "
            "with `ocr_images` into `save_to`, and inspect outputs before delivery. For formulas, data-rigor verification parameters, and detailed Office recipes, "
            "load `read_skill` with `name=\"office-recipes\"` when those details affect the next action. For DOCX structural acceptance, "
            "`read` returns headings plus paragraph/table/image counts; `search_in_file` is only for plain text files."
        ),
        "Office 按扩展名选择能力；DOCX 合法 block/type/action 要匹配，verify_integrity 仅用于 XLSX。",
    )
    _set_prop_description(
        OFFICE_TOOL_SCHEMA,
        "action",
        (
            "Office operation. DOCX supports read/write/append/replace_section/replace_block/delete_block/"
            "replace_blocks/insert_block/fill_empty_headings/extract_images/ocr_images/insert_image/verify_numbers/verify_rigor. PPTX supports slide "
            "read/write/edit/image extraction/OCR/insert_image/verify_numbers. XLSX supports read/write/append/"
            "update_cells/extract_images/ocr_images/verify_integrity. Choose a verifier supported by the file type: "
            "DOCX structural checks use `read`, DOCX data checks use `verify_numbers` or `verify_rigor` with `csv_paths`, "
            "and `verify_integrity` is XLSX-only, not a DOCX validity check."
        ),
        "Office 操作类型；DOCX 用 read/verify_numbers/verify_rigor，verify_integrity 仅 XLSX。",
    )
    _set_prop_description(OFFICE_TOOL_SCHEMA, "path", "Workspace-relative Office file path.", "Office 文件相对路径。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "title", "Optional cover/title text for document creation.", "可选标题。")
    _set_prop_description(
        OFFICE_TOOL_SCHEMA,
        "blocks",
        (
            "Structured DOCX blocks for write/append/edit actions. Valid types: heading, paragraph, list, table, "
            "image, equation, page_break. Use paragraph for ordinary text/subtitles, list for bullet/numbered items, "
            "table rows as non-empty 2D arrays like [[\"A\",\"B\"],[\"C\",\"D\"]], and image blocks with an existing workspace path."
        ),
        "DOCX 结构化块；普通文字用 paragraph，项目符号用 list，表格和图片需字段完整。",
    )
    _set_prop_description(OFFICE_TOOL_SCHEMA, "slides", "Slide definitions for presentation write/edit actions.", "幻灯片定义列表。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "slide", "Single slide definition for one-slide edit actions.", "单页幻灯片定义。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "sheets", "Worksheet definitions for spreadsheet write/append actions.", "工作表定义列表。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "updates", "Spreadsheet cell updates such as sheet, cell/range, and value.", "表格单元格更新。")
    _set_prop_description(
        OFFICE_TOOL_SCHEMA,
        "index",
        "Zero-based non-negative block or slide index for targeted edit actions. For DOCX block edits, read the document first and copy the exact `block_index`.",
        "从零开始的块或页索引；DOCX 先 read 再用准确 block_index。",
    )
    _set_prop_description(OFFICE_TOOL_SCHEMA, "count", "Number of consecutive blocks or slides to delete.", "连续删除数量。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "heading_text", "Section heading text to match for section replacement.", "章节标题匹配文本。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "keep_heading", "Whether to keep the matched heading during section replacement.", "替换章节时是否保留标题。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "create_sheet_if_missing", "Whether to create a worksheet when an update targets a missing sheet.", "工作表缺失时是否创建。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "sheet_name", "Worksheet name to read; omit to read all sheets.", "要读取的工作表名。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "out_dir", "Workspace-relative output directory for extracted images.", "提取图片输出目录。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "start_block", "Zero-based starting body-block index for segmented Word reads.", "Word 分段读取起始块。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "end_block", "Inclusive zero-based ending body-block index for segmented Word reads.", "Word 分段读取结束块。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "max_blocks", "Maximum number of Word body blocks to return in one read.", "单次返回正文块上限。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "max_images", "Maximum embedded images to OCR in this batch.", "本批 OCR 图片上限。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "image_offset", "Zero-based embedded-image offset for batched OCR.", "图片批处理起始偏移。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "max_size_mb", "Maximum single embedded-image size in MB for OCR.", "单图 OCR 大小上限。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "image_path", "Workspace-relative existing image path to insert.", "要插入的已存在图片路径。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "width_inches", "Rendered image width in inches.", "图片宽度英寸数。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "caption", "Optional image caption text.", "图片说明文字。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "csv_paths", "Source CSV/data paths used for numeric verification.", "数字核对源数据路径。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "pivot_col", "Grouping/pivot column for consistency and formula checks.", "分组或透视列。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "source_path", "Source text path for document-vs-source verification.", "用于对照验证的源文件路径。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "source_text", "Inline source text for document-vs-source verification.", "用于对照验证的源文本。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "number_pattern", "Regular expression for extracting numbers during verification.", "提取数字的正则。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "tolerance", "Numeric comparison tolerance.", "数值比对容差。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "threshold", "Primary verification threshold.", "主判定阈值。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "warn_threshold", "Warning threshold for verification.", "告警阈值。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "pivot_aliases", "Alias mapping for pivot/grouping columns.", "透视列别名映射。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "pivot_values", "Subset of pivot values to verify.", "限定核对的透视值。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "comparison_assertions", "Comparison assertions to verify.", "对比断言列表。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "internal_facts", "Facts or numbers that should be internally consistent.", "内部一致性事实。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "scaling_group_cols", "Columns used to group scaling/complexity checks.", "复杂度检查分组列。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "scaling_n_col", "Input-size column for scaling checks.", "规模列名。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "scaling_value_col", "Measured-value column for scaling checks.", "耗时或数值列名。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "scaling_super_linear_threshold", "Threshold for flagging super-linear growth.", "超线性增长阈值。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "expect_reproducibility_metadata", "Whether reproducibility metadata is expected.", "是否要求可复现性元数据。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "edits", "Batch edit instructions for block replacement.", "批量编辑指令。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "save_to", "Workspace-relative path for saving long OCR or verification text.", "长文本保存路径。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "max_workers", "Maximum OCR worker concurrency for embedded-image OCR.", "OCR 并发上限。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "per_image_timeout", "Timeout in seconds for each embedded-image OCR operation.", "单图 OCR 超时。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "text", "Text content for actions that accept direct text.", "文本内容。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "sheet", "Worksheet name when operating on spreadsheets.", "工作表名称。")
    _set_prop_description(OFFICE_TOOL_SCHEMA, "cells", "Spreadsheet cell updates or ranges, depending on action.", "表格单元格数据。")
    _set_tool_description(
        TODO_WRITE_SCHEMA,
        (
            "Write the current short task todo list, replacing the previous list. Use it to track complex multi-step work, "
            "not to carry long prose, scripts, document bodies, tables, patches, or final answers. Use workspace, office, "
            "or edit tools for artifact content. "
            "At most one todo may be `in_progress` at a time. Even when multiple helpers run in parallel, mark only "
            "the current coordinating step as `in_progress`; keep other parallel branches `pending` until the main "
            "process is actively handling them."
        ),
        "覆盖写入短任务清单；不承载长正文、脚本或文档内容；并行 helper 场景也只能有一个主控步骤处于 in_progress。",
    )
    _set_prop_description(TODO_WRITE_SCHEMA, "todos", "Complete replacement todo list.", "完整任务清单。")
    _set_nested_prop_description(TODO_WRITE_SCHEMA, "todos", "id", "Unique todo ID.", "唯一任务 ID。")
    _set_nested_prop_description(TODO_WRITE_SCHEMA, "todos", "content", "Brief concrete task description, not long artifact content.", "简短任务描述，不是长产物内容。")
    _set_nested_prop_description(TODO_WRITE_SCHEMA, "todos", "status", "Todo status; at most one item should be in progress.", "任务状态。")
    _set_tool_description(TODO_READ_SCHEMA, "Read the current task todo list.", "读取当前任务清单。")
    _set_tool_description(
        COMMIT_TO_MAIN_SCHEMA,
        (
            "Promote selected helper/workspace outputs into the main workspace when they are accepted final or "
            "milestone deliverables. Use this only after concrete workspace-relative files exist and should persist "
            "as user-facing artifacts. Read-only analysis, direct final-answer summaries, status updates, and empty "
            "or uncertain file lists can finish without this tool. Before promoting, choose one accepted artifact for each "
            "user-requested deliverable. If the current file name contains helper task ids, revision labels, "
            "failed/intermediate wording, or other internal naming, create or copy the accepted artifact to a clean "
            "user-facing filename and promote that clean path."
        ),
        "只在已有明确最终或里程碑产物文件时提升到主工作区；只读分析和空路径不需要调用。",
    )
    _set_prop_description(
        COMMIT_TO_MAIN_SCHEMA,
        "paths",
        "Non-empty workspace-relative final files to promote. Prefer clean user-facing names over helper-prefixed or intermediate filenames.",
        "非空的最终文件路径，优先使用干净文件名。",
    )
    _set_tool_description(
        FETCH_TO_TEMP_SCHEMA,
        "Copy files or directories from the main workspace or previous snapshot into the current temporary workspace.",
        "从主区或上一轮快照复制文件到临时区。",
    )
    _set_prop_description(FETCH_TO_TEMP_SCHEMA, "source", "Copy source: main workspace or previous snapshot.", "复制来源。")
    _set_prop_description(FETCH_TO_TEMP_SCHEMA, "paths", "Workspace-relative files or directories to copy.", "要复制的路径。")
    _set_tool_description(
        RECALL_THREAD_SCHEMA,
        (
            "Recall compressed thread or task-alignment history when the current long-chain task is losing the original "
            "request, acceptance points, todo state, or previous user constraints. Use it for alignment within the "
            "current task; previous execution-chain evidence is attached through continue_toolchain."
        ),
        "长链路回看当前任务契约、验收点和约束，用于对齐而非续接旧执行链。",
    )
    _set_tool_description(
        CONTINUE_TOOLCHAIN_SCHEMA,
        (
            "Attach previous toolchain context only when this round is a continuation of the same execution chain, "
            "same deliverable, same project milestone, or explicit repair/verification of earlier tool work. Use it "
            "near the start and state the concrete continuity reason. Prefer `recall_thread` for remembering user "
            "requirements, and start fresh for a new question that merely happens to share the same project."
        ),
        "只在同一执行链、同一交付物或明确续修验时接入旧工具链；普通同项目新问题应重新开始。",
    )
    _set_prop_description(
        CONTINUE_TOOLCHAIN_SCHEMA,
        "reason",
        "Concrete continuity reason, such as the same deliverable, project milestone, repair, or verification chain.",
        "续链原因。",
    )
    _set_prop_description(
        CONTINUE_TOOLCHAIN_SCHEMA,
        "max_chars",
        "Maximum characters of previous toolchain context to attach.",
        "续链上下文字符上限。",
    )
    _set_tool_description(
        PROGRESS_NOTE_SCHEMA,
        "Helper-only: publish a short progress note for the main process while a long task is running.",
        "helper 专用，向主进程报告简短进度。",
    )
    _set_prop_description(PROGRESS_NOTE_SCHEMA, "text", "Short factual progress note.", "简短进度摘要。")
    _set_tool_description(
        REQUEST_RESOURCE_SCHEMA,
        (
            "Helper-only: freeze this helper and request a resource from the main process, such as draw, code, OCR, "
            "TTS, or edit support. Include the missing resource, current evidence, needed outputs, and the concrete "
            "condition for resuming. The main process decides whether an "
            "existing resource satisfies the request, spawns a resource helper, refuses it, or terminates the blocked task."
        ),
        "helper 专用，缺资源时冻结并请求主进程协调；不会自行派发资源 helper。",
    )
    _set_prop_description(REQUEST_RESOURCE_SCHEMA, "kind", "Resource helper kind requested from the main process.", "需要的资源 helper 类型。")
    _set_prop_description(REQUEST_RESOURCE_SCHEMA, "reason", "What resource is missing and where progress is blocked.", "缺少资源的原因。")
    _set_prop_description(REQUEST_RESOURCE_SCHEMA, "needed_outputs", "Desired output files or evidence from the resource helper.", "期望资源产物或证据。")
    _set_prop_description(REQUEST_RESOURCE_SCHEMA, "resume_instruction", "Instruction the main process should use when resuming this helper.", "唤醒后的续作指令。")
    _set_tool_description(
        ASK_USER_QUESTION_SCHEMA,
        (
            "Ask the user a concise clarifying question when progress depends on a missing decision or external input. "
            "A current request to save, create, edit, or jot down an artifact is already task authorization for that artifact; "
            "use the appropriate file/project tool unless content or destination facts are missing."
        ),
        "需要用户决策或外部信息时提问；用户已要求保存/创建/记录时即有该产物授权。",
    )
    _set_prop_description(ASK_USER_QUESTION_SCHEMA, "question", "Complete concise question to ask the user.", "要问用户的问题。")
    _set_prop_description(ASK_USER_QUESTION_SCHEMA, "options", "Optional short answer choices.", "可选答案。")
    _set_prop_description(ASK_USER_QUESTION_SCHEMA, "context", "Brief reason why this decision is needed.", "提问原因。")
    _set_tool_description(
        INSPECT_FILE_TOOL_SCHEMA,
        "Inspect a produced file and report structure, metadata, and validity signals before relying on it.",
        "检查产物文件结构和有效性。",
    )
    _set_prop_description(INSPECT_FILE_TOOL_SCHEMA, "path", "Workspace-relative produced file path.", "产物相对路径。")
    _set_tool_description(
        OCR_TOOL_SCHEMA,
        (
            "Read visual text from images, PDFs, or Office documents when the current task needs evidence from a "
            "specific visual/document file. Concept, principle, log, or scheduling questions about OCR should be answered "
            "directly without using this tool. Choose tier by task stakes and result quality; `allow_upgrade=true` lets "
            "the OCR layer escalate up to `max_tier`. Present confirmed content and uncertainty to the user, while "
            "treating engine names and tier labels as internal."
        ),
        "读取图片/PDF/Office 中的文字证据，可按质量逐级升档。",
    )
    _set_prop_description(OCR_TOOL_SCHEMA, "image_path", "Workspace path to an image, PDF, or Office document.", "工作区内图片或文档路径。")
    _set_prop_description(OCR_TOOL_SCHEMA, "image_base64", "Base64 image content when no workspace path exists.", "无路径小图的 base64 内容。")
    _set_prop_description(OCR_TOOL_SCHEMA, "tier", "Internal OCR effort tier: fast, balanced, or accurate.", "内部 OCR 档位。")
    _set_prop_description(OCR_TOOL_SCHEMA, "allow_upgrade", "Allow automatic escalation up to `max_tier` based on quality signals.", "允许按质量自动升档。")
    _set_prop_description(OCR_TOOL_SCHEMA, "max_tier", "Maximum OCR tier allowed for this call.", "允许使用的最高档位。")
    _set_prop_description(OCR_TOOL_SCHEMA, "return_raw", "Return raw previews when folded abnormal spans matter.", "需要时返回原始预览。")
    _set_prop_description(OCR_TOOL_SCHEMA, "save_to", "Workspace-relative `.txt` or `.md` path for saving long OCR text.", "长 OCR 文本保存路径。")

    _set_tool_description(
        TTS_TOOL_SCHEMA,
        (
            "Generate an audio file from text when the task explicitly needs an audio artifact or a final voice file. "
            "Concept, principle, log, or scheduling questions about TTS should be answered directly without using this tool. "
            "For ordinary conversational voice replies, keep the final text concise and let the output layer handle voice "
            "delivery. Persona voice is system-managed outside model parameters."
        ),
        "按文本生成音频文件；普通语音回复由输出层处理，声音配置由系统管理。",
    )
    _set_prop_description(TTS_TOOL_SCHEMA, "text", "Text to synthesize.", "要合成的文本。")
    _set_prop_description(TTS_TOOL_SCHEMA, "language", "Optional language name such as Chinese, English, or Japanese.", "可选语言名称。")
    _set_prop_description(TTS_TOOL_SCHEMA, "output_filename", "Optional output filename inside the workspace.", "可选输出文件名。")
    _set_prop_description(TTS_TOOL_SCHEMA, "speed", "Speech speed factor; 1.0 is normal.", "语速倍率。")
    _set_prop_description(TTS_TOOL_SCHEMA, "push", "Legacy compatibility flag; audio delivery is handled outside this tool.", "历史兼容参数。")

    _set_tool_description(
        MAIN_WORKSPACE_TOOL_SCHEMA,
        (
            "Main-process workspace tool for file management only. It can create directories, write documentation files, "
            "append short text sections, run file-management commands, and locate files. Source-code implementation, "
            "substantive generation, long documents, command-heavy analysis, and tests are helper-owned in the normal workflow; "
            "this tool keeps the coordinator lightweight. Large write/append calls are hard resource-boundary failures: "
            "no file is written, no partial content is saved, and the tool result reports path, size, and recovery facts. "
            "The tool loop may replace the attempted long content argument with an omission marker after the failed call "
            "so the main context does not retain the long body. In environment project work, "
            "this tool works in the chat workspace, not the real project directory. Use env tools for real project files: "
            "env_fetch/edit staged copy/env_diff/env_apply_replace for existing "
            "files, and env_apply_create for confirmed-new files. `_env/...` paths are staged copies and staging "
            "evidence, not a project creation namespace or project directory to populate with workspace.write. "
            "Project file creation uses env_apply_create rather than workspace.write. "
            "workspace.mkdir creates chat-workspace folders only; for project directories, create project files through "
            "env_apply_create, which creates needed parent folders, or use env_run only when an actual empty project "
            "directory is itself the requested artifact."
        ),
        "主进程轻量文件管理工具；真实项目文件和目录走 env 工具，workspace.mkdir 只建聊天工作区目录。",
    )
    _set_prop_description(
        MAIN_WORKSPACE_TOOL_SCHEMA,
        "action",
        "Required action: `mkdir`, `write`, `append`, `run`, or `locate`. In environment project work, `mkdir` creates chat-workspace folders only.",
        "必填操作类型；项目模式下 mkdir 不是项目目录操作。",
    )
    _set_prop_description(MAIN_WORKSPACE_TOOL_SCHEMA, "path", "Workspace-relative path.", "工作区相对路径。")
    _set_prop_description(
        MAIN_WORKSPACE_TOOL_SCHEMA,
        "content",
        (
            "Short documentation text for `write` or `append`. Large content is rejected rather than truncated or "
            "partially written; use helper-owned artifacts, smaller logical sections, or environment apply tools when "
            "the current evidence says durable project state is needed."
        ),
        "短文档写入内容；超大内容会拒绝且不截断落盘。"
    )
    _set_prop_description(MAIN_WORKSPACE_TOOL_SCHEMA, "command", "File-management command for `run`.", "文件管理命令。")
    _set_prop_description(MAIN_WORKSPACE_TOOL_SCHEMA, "timeout_sec", "Maximum runtime in seconds.", "命令超时时间。")
    _set_prop_description(MAIN_WORKSPACE_TOOL_SCHEMA, "pattern", "Filename or glob pattern for `locate`.", "文件名或 glob 匹配。")

def apply_english_schema_descriptions(schema_globals: dict) -> None:
    """Apply English-first model-visible descriptions to tool schema objects."""
    globals().update(schema_globals)
    _apply_english_schema_descriptions()
    _set_prop_description(
        WORKSPACE_TOOL_SCHEMA,
        "command",
        "Shell command for `run`. Match the active platform. On Windows without Git Bash, prefer dedicated file tools or Python probes for file discovery, and use Windows-compatible commands. Unix-only `ls`, `find`, `/dev/null`, heredocs, or inline `export` syntax require a confirmed Unix shell.",
        "运行命令需匹配当前平台；Windows 下优先专用文件工具或 Python 探针，避免 Unix 专属命令。",
    )
    _set_prop_description(
        MAIN_WORKSPACE_TOOL_SCHEMA,
        "command",
        "File-management command for `run`. Match the active platform. On Windows without Git Bash, prefer `locate`, `read_file`, `search_*`, or helper-delegated probes. Unix-only `ls`, `find`, `/dev/null`, heredocs, or inline `export` syntax require a confirmed Unix shell.",
        "文件管理命令需匹配当前平台；Windows 下优先专用文件工具或委派 helper 探针。",
    )

    _set_tool_description(
        MAIN_READ_FILE_SCHEMA,
        (
            "Main-process text spot-check reader. It is designed for small files, narrow main-owned checks, or a narrow "
            "evidence location already identified by helper/search/code_index/inspect. Broad source reading, long logs, "
            "generated reports, source-material extraction, and full-file analysis are helper-owned in the normal workflow, "
            "with the main process synthesizing from concise evidence. Large unbounded reads return "
            "file-size and targeting facts instead of filling the main context."
        ),
        "主进程只做小文件或定点核查；大文件和全量分析由 helper 读取后回报精简证据。",
    )
    _set_prop_description(MAIN_READ_FILE_SCHEMA, "path", "Workspace-relative text file path.", "工作区相对文本路径。")
    _set_prop_description(MAIN_READ_FILE_SCHEMA, "start_line", "Starting line number for a narrow evidence check after the location is known.", "已知位置后的窄范围核查起始行。")
    _set_prop_description(MAIN_READ_FILE_SCHEMA, "end_line", "Inclusive ending line number for the same narrow evidence check.", "窄范围核查的结束行。")
    _set_prop_description(MAIN_READ_FILE_SCHEMA, "max_chars", "Maximum returned characters; main-process defaults and caps are intentionally small.", "主进程返回字符上限更小。")
