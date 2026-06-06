"""Tool JSON schema literals for model-visible tools.

Schemas cover python, memory expansion, workspace, office, delegate, read/edit,
todo, OCR, TTS, and related tools. Keep model-facing descriptions English-first
with concise Chinese summaries. Registry re-exports these constants for
compatibility.

工具 schema 集中定义；模型可见描述采用英文主体加中文概括。
"""


# ── 工具 schemas ────────────────────────────────────────────
PYTHON_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "python",
        "description": (
            "Run isolated Python for pure calculation, small in-memory transforms, and quick reasoning checks.\n"
            "This tool runs in a temporary sandbox separate from workspace files. `open()`, Path.read_text/write_text, "
            "os.listdir, os.walk, CSV/PNG/DOCX reads, and saved artifacts operate only inside that sandbox. "
            "When a task mentions a file path, project file, source tree, CSV, image, "
            "PDF, Office document, generated artifact, or third-party library, switch tools: use read_file/search "
            "for reads, workspace(action='write') for scripts or text artifacts, and workspace(action='run', "
            "command='python script.py') for file IO, pandas/numpy/PIL/openpyxl, or project commands.\n"
            "Allowed imports include standard in-memory libraries such as math, statistics, datetime, json, re, "
            "collections, itertools, functools, decimal, fractions, random, hashlib, base64, time, operator, "
            "bisect, heapq, html, unicodedata, binascii, hmac, copy, dataclasses, enum, typing, uuid, string, "
            "and textwrap. The last expression is returned as `result`; print output is returned as `stdout`. "
            "Limits: CPU 10s, memory 256MB, no file IO, no network.\n\n"
            "python 工具只做隔离内存计算；涉及工作区文件、项目路径、第三方库或产物保存时改用 read_file 或 workspace.write/run。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute.\n\n要执行的 Python 代码。",
                },
            },
            "required": ["code"],
        },
    },
}


EXPAND_WARM_SCHEMA = {
    "type": "function",
    "function": {
        "name": "expand_warm",
        "description": (
            "Expand warm-memory entries by ID from the system index. Returns headline, summary, internal_hint, "
            "entities, and tendencies for each entry. Expand only entries that are relevant to this response; "
            "batch multiple IDs when useful.\n\n"
            "按 ID 展开相关温记忆条目。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Warm-memory IDs such as w_xxx.\n\n温记忆 ID 列表。",
                },
            },
            "required": ["ids"],
        },
    },
}


EXPAND_COLD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "expand_cold",
        "description": (
            "Expand cold-memory graph nodes by ID and traversal depth. Returns node content and neighbor headlines. "
            "Use depth=1 for direct neighbors and depth=2 only when deeper context is needed.\n\n"
            "按 ID 和深度展开冷记忆图节点。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Cold-node IDs such as c_xxx.\n\n冷节点 ID 列表。",
                },
                "depth": {
                    "type": "integer",
                    "description": "Neighbor traversal depth: 1 or 2.\n\n邻居遍历深度。",
                    "default": 1,
                },
            },
            "required": ["ids"],
        },
    },
}


EXPAND_KB_SCHEMA = {
    "type": "function",
    "function": {
        "name": "expand_kb",
        "description": (
            "Expand knowledge-base nodes from condensed shared history. Usage mirrors expand_cold, but the scope is KB.\n\n"
            "展开知识库节点；用法同冷记忆展开。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "depth": {"type": "integer", "default": 1},
            },
            "required": ["ids"],
        },
    },
}


MARK_AVOID_SCHEMA = {
    "type": "function",
    "function": {
        "name": "mark_avoid_mention",
        "description": (
            "Record that the user explicitly prefers reduced proactive mention of a topic. "
            "Background maintenance marks semantically related cold-memory or KB nodes for reduced proactive mention; "
            "the nodes remain stored and available for direct user questions. Use this when the user clearly asks for "
            "a topic to stay out of proactive replies. Also list the topic in this round's `plan.avoid` field.\n\n"
            "记录用户明确希望减少主动提及的主题，不删除记忆；本轮 plan.avoid 也列出该话题。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Natural-language topic descriptions for reduced proactive mention.\n\n希望减少主动提及的话题描述。",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional user-provided reason or context.\n\n用户请求原因或上下文。",
                },
            },
            "required": ["topics"],
        },
    },
}


WORKSPACE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace",
        "description": (
            "Lightweight persistent workspace operations: create directories, write small text/verification files, run bounded commands, "
            "and locate files. `action` is required; missing action returns `unknown action: ''`.\n\n"
            "Main-process boundary: the main process coordinates, searches, verifies, and applies tiny patches. Substantive implementation, "
            "documents, charts, algorithms, and generated artifacts should be delegated to helpers. Main-process `.py` writes are for small "
            "verification/probing scripts only.\n\n"
            "Actions: mkdir creates a workspace subdirectory; write creates or rewrites small text/JSON/Markdown or tiny verify scripts; "
            "run executes bounded commands and returns stdout/stderr/returncode; locate finds files by name or glob.\n\n"
            "Workspace files remain available for later search/read. Writes outside the workspace are blocked; risky system commands are blocked. "
            "Use processes tools for background process control.\n\n"
            "workspace 用于轻量文件和命令；主进程只编排/验证，实质实现交给 helper。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["mkdir", "write", "run", "locate"],
                    "description": "Required action: mkdir, write, run, or locate. Missing action returns unknown action.\n\naction 必填。",
                },
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path. Required for mkdir/write; omit the field for run when no path is needed.\n工作区相对路径；不需要时省略字段。",
                },
                "content": {
                    "type": "string",
                    "description": "File content. Required for action=write.\n\nwrite 时的文件内容。",
                },
                "command": {
                    "type": "string",
                    "description": "Shell command for action=run, such as 'python calc.py'.\n\nrun 时的命令。",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": (
                        "Expected maximum runtime for action=run, 1..300 seconds. Default is intentionally short (1s), "
                        "so pass this for compile, test, conversion, or benchmark commands. Timed-out subprocesses are killed.\n\n"
                        "run 建议显式传超时时间。"
                    ),
                    "minimum": 1,
                    "maximum": 300,
                },
                "pattern": {
                    "type": "string",
                    "description": "Filename or glob pattern for action=locate. Plain strings are treated as fuzzy filename matches.\n\nlocate 文件名或 glob。",
                },
            },
            "required": ["action"],
        },
    },
}

OCR_WORKSPACE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace",
        "description": (
            "Read-helper text evidence writer. It writes internal `.txt` source evidence, quality notes, and line-range suggestions. "
            "The `.txt` output is source material for the main process or downstream helpers, not a polished user-facing report. "
            "Use the dedicated reading/recognition tools or request_resource for clearer evidence.\n\n"
            "read helper workspace 只写内部证据 txt；正文报告交给主进程或 edit helper。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["write"],
                    "description": "Fixed to write; read helpers can only write internal evidence.\n\n固定为 write。",
                },
                "path": {
                    "type": "string",
                    "description": "Workspace-relative .txt path, such as ocr_questions_7_10.txt.\n\n只能写 .txt 相对路径。",
                },
                "content": {
                    "type": "string",
                    "description": "Confirmed text, uncertain text, quality notes, and suggested line ranges for the main process.\n\n内部证据内容与质量备注。",
                },
            },
            "required": ["action", "path", "content"],
        },
    },
}


TTS_WORKSPACE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace",
        "description": (
            "TTS helper scoped text-material writer. Use only for short internal notes, transcript fragments, "
            "or synthesis manifests that help the main thread understand the generated audio. Audio generation itself "
            "uses the dedicated TTS tool; this writer only saves text notes.\n\n"
            "tts helper 专用文本写入工具，仅写内部文本材料。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["write"],
                    "description": "Fixed value: `write`.\n\n固定为写入文本材料。",
                },
                "path": {
                    "type": "string",
                    "description": "Relative `.txt` path for an internal TTS note or manifest.\n\n内部 TTS 文本材料相对路径。",
                },
                "content": {
                    "type": "string",
                    "description": "Short transcript, manifest, or quality note for the main thread.\n\n给主线程看的短文本材料。",
                },
            },
            "required": ["action", "path", "content"],
        },
    },
}


EDIT_WORKSPACE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace",
        "description": (
            "Edit-helper scoped artifact writer and locator. Use it to create folders, write small final text artifacts, "
            "or locate already available files. Computation, scripts, tests, and broad extraction belong to code/read "
            "helpers.\n\n"
            "edit helper 专用产物写入/定位工具，仅做产物文件操作。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["mkdir", "write", "locate"],
                    "description": "Allowed action: mkdir, write, or locate.\n\n允许创建目录、写文本产物或定位已有文件。",
                },
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path for mkdir/write. Use artifact paths, not helper temp paths.\n\n工作区相对路径。",
                },
                "content": {
                    "type": "string",
                    "description": "Text content for write.\n\n写入的文本内容。",
                },
                "pattern": {
                    "type": "string",
                    "description": "Filename or glob pattern for locate.\n\n文件名或 glob 匹配。",
                },
            },
            "required": ["action"],
        },
    },
}

SUMMARY_WORKSPACE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace",
        "description": (
            "Inventory-helper scoped inspection workspace. Use it for project inventory commands, lightweight "
            "statistics, locating staged files, and writing temporary inspection scripts or notes under `_scratch/`. "
            "It is not for project edits, final deliverables, Office/media artifacts, or `_env/` writes.\n\n"
            "inventory helper 专用检查工作区：用于目录盘点、统计、定位文件，以及在 `_scratch/` 写临时分析脚本/笔记；不修改项目、不写交付物。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["run", "locate", "write", "mkdir"],
                    "description": "Allowed action: run, locate, write, or mkdir.\n\n允许运行检查、定位文件、写临时文件或创建临时目录。",
                },
                "command": {
                    "type": "string",
                    "description": "Read-only inspection command for statistics, listing, or parsing staged files.\n\n只读检查命令。",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "Maximum runtime in seconds for `run`.\n\n命令超时时间。",
                },
                "pattern": {
                    "type": "string",
                    "description": "Filename or glob pattern for locate.\n\n文件名或 glob 匹配。",
                },
                "path": {
                    "type": "string",
                    "description": "Workspace-relative temporary path for write/mkdir. Use `_scratch/...` or `scratch/...`.\n\n临时脚本或笔记路径。",
                },
                "content": {
                    "type": "string",
                    "description": "Text content for temporary write.\n\n临时文件文本内容。",
                },
            },
            "required": ["action"],
        },
    },
}


VERIFY_WORKSPACE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace",
        "description": (
            "Verify-helper scoped validation runner. Use only to run bounded checks or locate files that support a "
            "verification verdict. Writing, implementation, and repair belong to "
            "the producing helper or main process.\n\n"
            "verify helper 专用验证工具：只运行检查或定位文件，不写入产物。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["run", "locate"],
                    "description": "Allowed action: run or locate.\n\n仅允许运行验证命令或定位文件。",
                },
                "command": {
                    "type": "string",
                    "description": "Bounded validation command that does not intentionally modify the workspace.\n\n用于验证的有界命令。",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "Maximum runtime in seconds for `run`.\n\n命令超时时间。",
                },
                "pattern": {
                    "type": "string",
                    "description": "Filename or glob pattern for locate.\n\n文件名或 glob 匹配。",
                },
            },
            "required": ["action"],
        },
    },
}


SEARCH_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_files",
        "description": (
            "Search the shared file index by keyword. Prefer the visible Shared Files index first because it already contains "
            "the complete list and summaries; use this tool only to filter many files or locate historical generated artifacts. "
            "Returns filename, description, and workspace path.\n\n"
            "按关键词过滤共享文件索引；多数情况先看共享文件列表。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keyword matched against file descriptions and names.\n\n搜索关键词。",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum returned entries. Default 10, max 50.\n\n返回条目上限。",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
}


FETCH_GROUP_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fetch_indexed_file",
        "description": (
            "Fetch a ready indexed file into the current workspace so read_file or inspect_file can read it. "
            "Call it only for ready entries from the Shared Files index; pending entries are not fetchable and failed "
            "entries need a new source. `kb_node_id` is the node ID shown in square brackets in the index. "
            "The returned `path` is the authoritative workspace-relative path; use it directly and do not guess names "
            "when conflicts add suffixes.\n\n"
            "把已就绪的索引文件取到当前工作区；以返回 path 为准。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kb_node_id": {
                    "type": "string",
                    "description": "Target node ID from the Shared Files index, such as `c_01J...`.\n索引文件节点 ID。",
                },
            },
            "required": ["kb_node_id"],
        },
    },
}


INSPECT_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "inspect_file",
        "description": (
            "Preflight a workspace file to identify type, direct-read safety, and the recommended reader/conversion workflow. "
            "Use it as the first step for unfamiliar files, especially Office, PDF, image, archive, media, or binary formats. "
            "Text files can usually go to read_file after inspection; structured files should follow recommended_workflow.\n\n"
            "先预检陌生文件，确定类型和读取路径。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path, such as 'report.docx', 'data.xlsx', or 'config.json'.\n\n工作区相对路径。",
                },
            },
            "required": ["path"],
        },
    },
}


READ_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read UTF-8 plain text from workspace-relative files and return line-numbered content for later targeted edits. "
            "Use this for source, logs, markdown, JSON, CSV, XML, and other text files. Structured or binary formats return "
            "a typed refusal with suggested_tools so the workflow can switch to the correct reader.\n"
            "\n"
            "## Reading Workflow\n"
            "- Plain text source and logs: read the whole file when size permits, or page with start_line/end_line.\n"
            "- Office files: inspect first, then use office read/write actions for body, tables, or edits.\n"
            "- PDFs: inspect first, then use the suggested text extraction or OCR path.\n"
            "- Images and visual files: inspect dimensions/format first, then use OCR or visual extraction.\n"
            "- After binary_or_structured_file_not_readable_as_text, follow suggested_tools on the next step.\n"
            "- When output is truncated, continue from next_start_line.\n"
            "\n"
            "Returns total_lines, shown_range, line-numbered content, and truncated status.\n\n"
            "read_file 只读纯文本；结构化/二进制文件先 inspect，再按建议切换 office、PDF 或 OCR 工具；大文本按行分页。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative text file path, such as 'sort.c'.\n\n工作区相对文本路径。",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Start line, 1-indexed. Default is 1.\n起始行号，默认 1。",
                    "default": 1,
                },
                "end_line": {
                    "type": "integer",
                    "description": "Inclusive end line, 1-indexed. Use -1 to read to EOF.\n结束行号，-1 表示读到末尾。",
                    "default": -1,
                },
                "max_chars": {
                    "type": "integer",
                    "description": (
                        "Maximum returned characters. Use larger values only when the next step needs that text in context.\n"
                        "输出字符上限；只有后续确实需要时才调大。"
                    ),
                    "default": 500000,
                },
            },
            "required": ["path"],
        },
    },
}


EDIT_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": (
            "Replace one exact text span in a text file. Prefer this for focused edits because the tool sends only the "
            "changed span rather than a whole file body.\n"
            "\n"
            "## Matching Contract\n"
            "- old_str must appear exactly expected_count times; default expected_count is 1.\n"
            "- old_str should include enough surrounding context and at least 5 characters.\n"
            "- On mismatch, the file is unchanged and the error explains how to widen context or set expected_count.\n"
            "- Use read_file first when uniqueness or surrounding structure is uncertain.\n"
            "- If repeated focused edits fail on the same logic area, step back: read the relevant file/function, rebuild the data flow, and use a broader helper-owned rewrite or replacement strategy.\n"
            "- Use new_str='' for deletion with a precise old_str.\n\n"
            "edit_file 用于精确文本替换；先确认唯一上下文，失败不改文件，连续局部失败时应重新理解结构并换更宽的修复策略。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path.\n\n文件相对路径。"},
                "old_str": {
                    "type": "string",
                    "description": "Exact text to replace, including indentation and newlines.\n\n要替换的精确文本。",
                },
                "new_str": {
                    "type": "string",
                    "description": "Replacement text. Empty string deletes the old text.\n\n新文本；空字符串表示删除。",
                },
                "expected_count": {
                    "type": "integer",
                    "description": "Expected number of old_str matches. Default 1.\n\nold_str 期望出现次数。",
                    "default": 1,
                },
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
}


INSERT_IN_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "insert_in_file",
        "description": (
            "Insert new text after a specific line in a text file. Use it for additive changes; use edit_file for replacements.\n"
            "\n"
            "Line placement:\n"
            "- 0 inserts before the first line.\n"
            "- -1 inserts at the end of the file.\n"
            "- A positive N inserts after line N.\n\n"
            "insert_in_file 用于新增文本；替换已有内容使用 edit_file。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path.\n\n文件相对路径。"},
                "after_line": {
                    "type": "integer",
                    "description": "Insert after this line: 0 before first line, -1 at EOF, N after line N.\n\n插入位置行号。",
                },
                "content_to_insert": {
                    "type": "string",
                    "description": "Content to insert; may be multiline.\n\n要插入的内容。",
                },
            },
            "required": ["path", "after_line", "content_to_insert"],
        },
    },
}


# ── 2026-05-02 part20:multi_edit(Claude Code 风格 MultiEdit)──
MULTI_EDIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "multi_edit",
        "description": (
            "Atomic multi-replacement edit for one text file. All edits in the call apply together or the file rolls back unchanged. "
            "Use it for plain text source, configuration, Markdown, and CSV. Structured or binary formats use their dedicated tools.\n"
            "\n"
            "## Best Use\n"
            "- Rename or update a symbol across declarations and uses.\n"
            "- Change a data-structure field across all read/write points.\n"
            "- Apply coordinated fixes across several functions in the same file.\n"
            "- Replace a whole function with a delete-and-insert pair.\n"
            "- Add parameters across declarations, definitions, and call sites.\n"
            "\n"
            "## Advantages\n"
            "- Fewer tool round trips than repeated edit_file.\n"
            "- One coherent diff for related changes.\n"
            "- Atomic rollback on mismatch.\n"
            "- Sequential application lets later edits match the result of earlier edits.\n"
            "\n"
            "## 调用格式\n"
            "```json\n"
            "{\"path\": \"huffman.c\", \"edits\": [\n"
            "  {\"old_str\": \"void foo(int a) {\", \"new_str\": \"void foo(int a, int b) {\"},\n"
            "  {\"old_str\": \"foo(x);\", \"new_str\": \"foo(x, 0);\", \"expected_count\": 3},\n"
            "  {\"old_str\": \"void foo(int);\", \"new_str\": \"void foo(int, int);\"}\n"
            "]}\n"
            "```\n"
            "\n"
            "## 限制\n"
            "- 单次最多 50 个 edit(超过拆多次调用)\n"
            "- 每个 edit 同 edit_file 规则:old_str ≥ 5 字符、配对校验、expected_count 默认 1"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path.\n\n文件相对路径。"},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_str": {"type": "string", "description": "Exact text to replace, same contract as edit_file.\n\n要替换的精确文本。"},
                            "new_str": {"type": "string", "description": "Replacement text; empty string deletes.\n\n新文本；空字符串表示删除。"},
                            "expected_count": {
                                "type": "integer",
                                "description": "Expected number of old_str matches. Default 1.\n\nold_str 期望出现次数。",
                                "default": 1,
                            },
                        },
                        "required": ["old_str", "new_str"],
                    },
                    "description": "Ordered edits applied atomically.\n\n按顺序原子应用的编辑列表。",
                },
            },
            "required": ["path", "edits"],
        },
    },
}


SEARCH_IN_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_in_file",
        "description": (
            "Search inside plain text files and return matching line numbers with previews. "
            "Use this for source code, Markdown, TXT, CSV/TSV, JSON/YAML, and logs; it is the "
            "portable replacement for `findstr` or `grep` and supports streaming scans up to 50MB. "
            "For Office/PDF/image/binary containers, first use `inspect_file`; use `office(action='read')` "
            "for DOCX/PPTX/XLSX body content and OCR/extraction tools for PDF or images. If this tool "
            "returns `binary_or_structured_file_not_readable_as_text`, switch to the suggested structured "
            "reader instead of retrying the same search. Use search results to confirm existence, locate "
            "line ranges for `read_file`, and establish edit uniqueness.\n\n"
            "文本搜索工具只用于纯文本/源码/日志/CSV/JSON；Office/PDF/图片先 inspect，再用 office/OCR 等结构化读取。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative text file path.\n\n文件相对路径。"},
                "pattern": {
                    "type": "string",
                    "description": "Search pattern. Interpreted as plain text unless is_regex=true.\n\n搜索模式。",
                },
                "is_regex": {
                    "type": "boolean",
                    "description": "When true, pattern is Python regular expression. Default false.\n\n是否按正则解释。",
                    "default": False,
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum matches to return. Default 50, max 200.\n\n返回匹配上限。",
                    "default": 50,
                },
            },
            "required": ["path", "pattern"],
        },
    },
}


# ── 2026-05-02 part14:code_index 工具 ──
# 解决 trace 74b1295b 教训:helper read 同一 .c 文件 60 次试图建立 mental model,
# 应该用一次"鸟瞰索引"工具代替反复 read。1000 行代码 → 30 行紧凑索引。
CODE_INDEX_SCHEMA = {
    "type": "function",
    "function": {
        "name": "code_index",
        "description": (
            "Create a compact structural index for one source file: functions, classes, structs, includes/imports, and line numbers. "
            "Use this as the first step for unfamiliar source files instead of reading the whole file. "
            "It is especially useful before focused read_file ranges, read_function calls, and bug triage across large files.\n\n"
            "Supported source families include C/C++, Python, JS/TS, Go, and Rust. The `summary` field is the compact table to read first:\n"
            "```\n"
            "rdh.c (762 lines, c)\n"
            "  includes: <stdint.h>, <stdlib.h>, <string.h>\n"
            "  L 45 struct   min_heap_t\n"
            "  L 80 fn       heap_push\n"
            "  L120 fn       huff_build_tree\n"
            "  L530 fn       huff_encode\n"
            "  L640 fn       huff_decode\n"
            "  L720 fn       rdh_decompress\n"
            "```\n"
            "Read concrete code only after the index identifies the relevant range.\n\n"
            "code_index 用于源码鸟瞰；先拿结构索引，再按行号精读。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative source file path, such as 'rdh.c'.\n\n工作区相对源码路径。",
                },
                "include_includes": {
                    "type": "boolean",
                    "description": "Whether to include #include/import lists. Default true.\n\n是否包含 include/import。",
                    "default": True,
                },
                "name_filter": {
                    "type": "string",
                    "description": (
                        "Optional symbol-name filter. Supports glob-like prefixes such as 'huff_*' or regex. "
                        "Use on large files to keep the summary compact.\n\n"
                        "可选符号名过滤器。"
                    ),
                },
                "kinds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional symbol-kind filter. Values include fn, struct, typedef, enum, define, class, def, import, include. "
                        "For example ['fn'] lists only functions.\n\n"
                        "可选符号类型过滤器。"
                                ),
                            },
                        },
            "required": ["path"],
        },
    },
}


# ── 2026-05-02 part16:read_function 工具 ──
READ_FUNCTION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_function",
        "description": (
            "Read one complete source function body with automatic boundary detection and optional caller/callee references.\n"
            "\n"
            "## Best Use\n"
            "- Use after code_index or read_file has narrowed the target to a specific source function.\n"
            "- Use for focused refactor review, caller/callee checks, or detailed reading of one known function.\n"
            "- For root-cause debugging that may cross several functions, first read enough surrounding file context, then use read_function for the narrowed function.\n"
            "- For HTML, Markdown, Office, logs, or embedded scripts, search for the target and read a line range with read_file.\n"
            "\n"
            "## Returns\n"
            "- body: line-numbered complete function body.\n"
            "- lines: [start, end] function boundary.\n"
            "- called_by: same-file callers when available.\n"
            "- calls: symbols called by this function.\n"
            "- match_count and available_functions when a precise match is unavailable.\n\n"
            "read_function 用于已定位的源码函数精读和调用关系；跨函数调试先读上下文，再精读目标函数。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative source file path, such as 'rdh.c'.\n\n工作区相对源码路径。",
                },
                "function_name": {
                    "type": "string",
                    "description": (
                        "Exact case-sensitive function name. The parameter key is `function_name`.\n"
                        "精确函数名，字段名固定为 function_name。"
                    ),
                },
                "include_xref": {
                    "type": "boolean",
                    "description": (
                        "Whether to include caller/callee references. Default true; disable only for very large files when references are unnecessary.\n"
                        "是否包含调用关系，默认开启。"
                    ),
                    "default": True,
                },
                "xref_scope": {
                    "type": "string",
                    "enum": ["file", "workspace"],
                    "description": (
                        "Cross-reference scope. Use 'file' for fast same-file callers; use 'workspace' when the caller map must span the project. "
                        "Workspace scope is slower but avoids a separate cross-file search.\n\n"
                        "调用关系范围：file 或 workspace。"
                    ),
                    "default": "file",
                },
            },
            "required": ["path", "function_name"],
        },
    },
}


# ── 2026-05-02 part16:search_across_files 工具 ──
SEARCH_ACROSS_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_across_files",
        "description": (
            "Search text across workspace files in one call and return matching lines by file. "
            "Use this for cross-file source/config/log searches instead of shell grep/findstr. "
            "Choose concrete tokens that appear in files, use 1-3 meaningful alternatives, and narrow with file_glob when possible. "
            "Returns results, files_scanned, files_with_matches, total_matches, and truncation status.\n\n"
            "跨文件文本搜索工具；用具体代码词和 file_glob 控制噪音。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Search text or regex pattern.\n\n搜索文本或正则。",
                },
                "file_glob": {
                    "type": "string",
                    "description": (
                        "Filename glob. Default '*' scans all files. Examples: '*.c', '*.py', 'src/*.h'.\n\n"
                        "文件名通配符。"
                    ),
                    "default": "*",
                },
                "is_regex": {
                    "type": "boolean",
                    "description": "Whether pattern is regex. Default false means plain text.\n\n是否按正则解释。",
                    "default": False,
                },
                "max_results_per_file": {
                    "type": "integer",
                    "description": "Maximum matching lines per file. Default 5.\n\n单文件返回行数上限。",
                    "default": 5,
                },
                "max_files": {
                    "type": "integer",
                    "description": "Maximum files to scan. Default 30.\n\n扫描文件数量上限。",
                    "default": 30,
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
}


AGENT_STATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "agent_state",
        "description": (
            "Maintain the structured task ledger for complex work: task contracts, evidence records, artifact manifests, "
            "resource waits, and status snapshots. Use it when a task has acceptance criteria, produced files, helper "
            "resource dependencies, or facts that must survive long toolchains. This tool does not replace verification; "
            "it records what has been verified, what is partial, and what is still blocked."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "status",
                        "upsert_contract",
                        "add_evidence",
                        "register_artifact",
                        "add_resource_task",
                        "update_resource_request",
                    ],
                    "description": "Ledger operation to perform.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task/helper identifier related to this ledger entry.",
                },
                "goal": {
                    "type": "string",
                    "description": "Task goal for `upsert_contract`.",
                },
                "acceptance": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Acceptance points for `upsert_contract`.",
                },
                "evidence_required": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Evidence needed before final delivery.",
                },
                "deliverables": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Expected user-facing deliverables.",
                },
                "risks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Known risks or uncertainty sources.",
                },
                "current_stage": {
                    "type": "string",
                    "description": "Current stage such as map, read, modify, verify, or report.",
                },
                "source": {
                    "type": "string",
                    "description": "Evidence source such as command, helper, inspect_file, manual_check, or artifact_verify.",
                },
                "status": {
                    "type": "string",
                    "enum": ["verified", "partial", "failed", "stale", "contradicted", "ready", "failed_artifact", "intermediate"],
                    "description": "Evidence or artifact status.",
                },
                "summary": {
                    "type": "string",
                    "description": "Short factual evidence summary.",
                },
                "kind": {
                    "type": "string",
                    "description": "Evidence/helper/artifact kind.",
                },
                "data": {
                    "type": "object",
                    "description": "Optional compact structured evidence data.",
                },
                "path": {
                    "type": "string",
                    "description": "Artifact path for `register_artifact`.",
                },
                "artifact_type": {
                    "type": "string",
                    "description": "Artifact type such as report, code, audio, docx, chart, image, or data.",
                },
                "created_by": {
                    "type": "string",
                    "description": "Creator identifier for an artifact.",
                },
                "verified_by": {
                    "type": "string",
                    "description": "Verifier identifier for an artifact.",
                },
                "evidence_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Evidence IDs supporting an artifact.",
                },
                "metadata": {
                    "type": "object",
                    "description": "Optional compact artifact metadata.",
                },
                "request_id": {
                    "type": "string",
                    "description": "Resource request ID for `add_resource_task`.",
                },
                "resource_task_id": {
                    "type": "string",
                    "description": "Resource-producing helper task ID linked to a resource request.",
                },
                "reason": {
                    "type": "string",
                    "description": "Decision reason for refusing or closing a resource request.",
                },
                "satisfied_by": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Concrete resource paths that satisfy a resource request.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}


DELEGATE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate",
        "description": (
            "Run and manage helper tasks in isolated workspaces. The main process owns goals, dependency order, monitoring, "
            "acceptance, and final synthesis; helpers do the substantial work.\n"
            "\n"
            "## Kinds and modes\n"
            "`kind` describes task nature. `mode` describes difficulty/resource strength.\n"
            "- `code`: implementation, debugging, compiling, benchmarks, data analysis, algorithms, HTML/scripts, project statistics that need commands or scripts, and executable file-preparation steps before reading.\n"
            "- `read`: source-material reading and evidence extraction from text files, prepared archive contents, images, PDFs, Office files, screenshots, forms, and scanned or visual content; writes internal `.txt` evidence.\n"
            "- `edit`: user-facing document and text assembly for docx/pptx/xlsx/markdown/json/yaml/txt; it should not gather broad source evidence or implement program source code.\n"
            "- `draw`: generate final PNG/chart/diagram files from data or clear specs.\n"
            "- `tts`: audio generation resource helper.\n"
            "- `verify`: adversarial checking and evidence reports for existing artifacts.\n"
            "- Use `mode='easy'` for ordinary work and `mode='hard'` for high difficulty, recovery, or stronger reasoning.\n"
            "\n按任务本质选 kind，按难度和重试强度选 mode。\n"
            "\n"
            "## When to delegate\n"
            "Delegate implementation, drawing, Office output work, TTS, reading/extraction, verification, substantial file work, and independent exploration. "
            "Split multiple algorithms, implementations, parameters, seeds, modules, files, or artifacts into parallel helpers. Keep strict dependencies serial.\n"
            "For broad multi-part work, first create a compact shared framework contract: goal, interfaces or schema, outline, evidence map, acceptance checks, ownership boundaries, and merge order. "
            "The framework must include an exact output matrix for every downstream consumer: helper task_id, kind, mode, input_files, expected_outputs, acceptance checks, and the final merge/apply target. "
            "The first framework task defines slots, dependencies, ownership, and acceptance; later helpers produce implementation bodies, long scripts, experiments, evidence-backed claims, citations, tables with final values, charts, and final assembly. "
            "`_helpers_shared/...` files are handoff evidence for later helpers; final user-facing artifact or project files must be assembled into explicit non-shared workspace files or `_env/...` staged project paths before acceptance. "
            "Then delegate bounded slices such as modules, chapters, data shards, source ranges, algorithms, experiments, or verification targets. "
            "Produce long code, documents, reports, extracted evidence, and analysis in inspectable segments instead of one monolithic write.\n"
            "For long file lists or many similar helpers, write the list/contract to a workspace manifest and keep delegate prompts compact. "
            "Use `framework` and `input_files` for shared context, and split very bulky fan-out into small batches so tool-call JSON stays valid.\n"
            "\n主进程负责编排和验收，重工作交给 helper；大型任务先建紧凑框架契约，只定义槽位、归属和验收，再分片并行和合并验收。\n"
            "\n"
            "## Benchmark and comparison discipline\n"
            "For comparisons, first define a shared benchmark spec: CSV schema, algorithm labels, timing and memory method, repetitions, seed, data distribution, and scale limits. "
            "Estimate runtime by complexity before large runs, calibrate with small probes, and use smaller scales or extrapolation notes for slow algorithms. "
            "Before summarizing, check that units, labels, and order-of-magnitude behavior match the evidence.\n"
            "\n横向比较先统一规格和运行预算，再汇总证据。\n"
            "\n"
            "## Continuation and branching\n"
            "Use the same `task_id` with `resume=true` for interrupted work, missing outputs, or repair of the same task. "
            "Change task_id only for a genuinely different subtask or after the old helper completed and its workspace is no longer needed. "
            "Use `fork_from` to branch several variants from one completed helper workspace.\n"
            "\n同一任务续作复用 task_id；多变体探索用 fork_from。\n"
            "\n"
            "## Monitoring and acceptance\n"
            "Set `wait_window_sec` for long tasks. Use still_running heartbeats, recent_tools, last_thought, outputs_check, file_map, verify_verdict, and repair_recommendation to decide whether to wait, resume, collect, verify, or cooperatively interrupt. "
            "If results are incomplete and the user did not interrupt, continue or escalate before finalizing. If recovery is not useful, report only verified partial results and the exact remaining gap. "
            "In environment tasks, `main_available_files` and `copy_stats.env_copied_files` mean `_env/...` changes are already available in the main workspace.\n"
            "\n按心跳、产物完整性和验证证据决策；未完成但可恢复时先续作或升级。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["spawn", "spawn_async", "poll", "collect", "wait_any", "kill", "status"],
                    "description": (
                        "Delegate action.\n"
                        "- spawn: start helpers and return when wait_window expires or all helpers finish.\n"
                        "- spawn_async: start helpers and return proc_ids immediately while helpers continue in the background.\n"
                        "- poll: quick heartbeat/status check for task_ids.\n"
                        "- collect: wait for final results for task_ids; completed helpers return immediately.\n"
                        "- wait_any: wait until any listed task finishes, useful after fan-out.\n"
                        "- kill: cooperative interruption for one helper by task_id.\n"
                        "- status: dashboard for all active and completed helpers in this trace; task_ids are optional.\n"
                        "Preferred asynchronous flow: spawn_async, continue other coordination, then poll/status, wait_any, or collect. "
                        "Use wait_any or collect for waiting; helper tasks should perform useful work rather than act as timers.\n\n"
                        "delegate action 控制 helper 生命周期；异步流程用 spawn_async 后轮询/收集，等待用 wait_any 或 collect。"
                    ),
                },
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "(action=poll/collect/wait_any 必须)要查询/收集的 task_id 列表。"
                        "顺序无关。同名 task_id 重复会被去重。"
                    ),
                },
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "description": (
                        "task 列表。每个 task 字段:task_id, prompt, kind, resume 等。\n"
                        "**注意**:`wait_window_sec` 不是 task 字段,是 delegate 调用顶层字段(同 `tasks` 同级)。"
                        "如果你把它放在 task 里,系统会自动 hoist 到顶层(取最大值)并 warn,但下次请放对位置。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": (
                                    "短标识,如 'n_100' / 'mergesort' / 'variant_1'。"
                                    "**用同一个 task_id 配 resume=true 可让 helper 续作上次中断的工作**"
                                    "(工作区被保留)。"
                                    "建议用语义化前缀(buddy/slab/...)而非 b1/b2/b3 — log 中能一眼看清谁在干啥。"
                                ),
                            },
                            "prompt": {
                                "type": "string",
                                "description": (
                                    "Focused helper prompt. State goal, required inputs, expected outputs, acceptance checks, "
                                    "and recovery/resource conditions. If the task belongs to a framework-first workflow, "
                                    "include the shared framework contract, the exact slice boundary, segment output names, local checks, "
                                    "and how to report conflicts or gaps. If resume=true, include the prior progress summary "
                                    "and the remaining work for the same task.\n"
                                    "\n"
                                    "Helper prompts should be compact, specific, and verifiable. Pass the current goal, necessary "
                                    "inputs, output paths, constraints, and 3-8 checkable acceptance points. For reversible, "
                                    "serialization, compression, or conversion work, include round-trip and byte/hash comparison "
                                    "standards when relevant. Keep broad history, persona text, unrelated helper reports, and "
                                    "tool manuals out of the helper prompt.\n"
                                    "\nhelper prompt 应聚焦、可验收；大型任务写明共享契约、分片边界、分段产物和本地检查；框架任务只写结构契约，续作复用同一任务并说明剩余工作。"
                                ),
                            },
                            "framework": {
                                "type": ["string", "object"],
                                "description": (
                                    "Shared framework contract for this helper batch or slice. Provide this for broad or multi-part work after the main process has established the framework. "
                                    "It should state goal, interfaces or schema, outline, evidence/source map, ownership boundary, validation checks, expected segment outputs, and merge order. "
                                    "For comparable algorithms, chapters, documents, or experiments, include an exact output matrix: each downstream helper task_id, kind, mode, input_files, expected_outputs, and final merge/apply target. "
                                    "Keep this field structural and portable; put evidence, final values, citations, conclusions, implementation details, and long section text in producer outputs instead. "
                                    "`_helpers_shared/...` entries are internal handoff files; the final user-facing artifact must be a non-shared workspace file or an `_env/...` project path before acceptance. "
                                    "Each slice helper should receive the same relevant framework plus its own narrow prompt; a dedicated framework/spec helper may create the contract first.\n\n"
                                    "共享框架契约字段；大型任务先建立只含结构、槽位、归属和验收的框架，再随分片 helper 一起传入。"
                                ),
                            },
                            "resume": {
                                "type": "boolean",
                                "description": (
                                    "Default false. When true, the helper keeps the previous workspace and continues from preserved files "
                                    "instead of recopying from the main workspace. Use it for interrupted or incomplete work with the same task boundary. "
                                    "Before resuming, inspect still_running status. If the existing helper is finalizing or about to produce a report, prefer waiting or collecting; "
                                    "if the direction must change, cooperatively interrupt first and then resume with the new focused prompt. "
                                    "The runtime protects against concurrent streams by finalizing or aborting the prior live stream before the resumed stream starts.\n\n"
                                    "resume=true 用于同一任务续作；先看心跳，接近收尾则等待，需换方向则先协作中断再续作。"
                                ),
                                "default": False,
                            },
                            "fork_from": {
                                "type": "string",
                                "description": (
                                    "可选。设为另一个已结束 helper 的 task_id 时,新 helper 启动前会"
                                    "**复制源 helper 的工作区**作为起点(自动启用 resume=true)。"
                                    "用法:看完 helper A 的报告后,想基于它的产物做 N 个并行变体——"
                                    "spawn 多个 task 都 fork_from='A',各自做不同变体。"
                                    "源工作区 >500MB 会被拒绝。"
                                ),
                            },
                            "kind": {
                                "type": "string",
                                # 真相源: app.llm.tools.delegate.VALID_HELPER_KINDS
                                "enum": ["code", "edit", "verify", "draw", "tts", "read", "project_map", "file_summary", "impact_review", "inventory"],
                                "default": "code",
                                "description": (
                                    "Choose the helper base kind from the work product, not from difficulty. "
                                    "`mode` controls resource strength; `kind` controls the tool family and deliverable boundary.\n\n"
                                    "Base kinds:\n"
                                    "- `code`: source implementation, debugging, build/test/benchmark commands, reusable scripts, data computation, algorithmic analysis, generated CSV/JSON evidence, executable file-preparation steps before reading, and project scaffold/shared-contract files that must be written or smoke-tested.\n"
                                    "- `read`: source-material reading and evidence extraction from text files, prepared archive contents, images, PDFs, Office files, screenshots, forms, and scanned or visual content. It writes internal `.txt` evidence for the main thread.\n"
                                    "- `edit`: polished document or structured-file assembly such as .docx/.pptx/.xlsx/Markdown/JSON/YAML/TXT. It consumes verified evidence and existing images; it does not gather broad source evidence, implement source code, or create charts.\n"
                                    "- `verify`: read-only adversarial review of code, data, images, documents, or helper artifacts, with evidence and acceptance status.\n"
                                    "- `draw`: image/chart production from data or a precise visual specification. Use `verify` for judging existing images and `code` for reusable charting applications.\n"
                                    "- `tts`: audio synthesis resources; report the produced audio path without changing voice policy.\n"
                                    "- `inventory`: environment project first-pass inventory: directory shape, file types, README/entry/config/test hints, lightweight statistics, and unread source-material groups.\n"
                                    "Planning rules:\n"
                                    "- Keep the user's full goal covered: every requested deliverable, evidence source, and acceptance check must be owned by a helper or by the main thread.\n"
                                    "- Fill each task as a helper request envelope: `kind`, `mode`, `framework`, `input_files`, focused `prompt`, `expected_outputs`, and `acceptance_checks`. Use concrete `_env/...` paths for project files.\n"
                                    "- Split executable preparation from reading: a code helper may unpack or convert files, while read helpers summarize source material and write internal evidence.\n"
                                    "- For many Office/PDF/image/text source files, fan out focused `read` helpers by source group or batch before downstream code/edit work. Script or library wording stays read when scripts are only the reading method.\n"
                                    "- For mixed work, build a pipeline instead of forcing one helper to cross tool families: computation/benchmarks in `code`, charts in `draw`, final document assembly in `edit`, acceptance review in `verify`.\n"
                                    "- If a helper is interrupted while making useful progress, resume the same `task_id` with the same base kind; upgrade to `mode='hard'` only when the narrow task shows repeated errors, weak reasoning, or missing capability.\n"
                                    "- Treat failed or cancelled helper output as evidence to inspect or repair. Present it as completed only after a matching helper or verification step confirms it.\n"
                                    "- Reading Office/PDF/image/text source material belongs to `read`; final document assembly belongs to `edit`; programming and command-running work belongs to `code`; difficulty upgrades preserve the base kind.\n\n"
                                    "按产物性质选 kind，按难度选 mode。大量材料先并行 read 提证据，再由 code/edit 消费；失败结果先修复或验证再采纳。"
                                ),
                            },
                            "mode": {
                                "type": "string",
                                "enum": ["easy", "hard"],
                                "default": "easy",
                                "description": (
                                    "Difficulty/resource mode for the same base kind.\n"
                                    "- `easy`: default path for ordinary bounded work.\n"
                                    "- `hard`: stronger reasoning/model budget for a specific difficult task, a useful retry, or an easy/hard race on a substantial project task.\n\n"
                                    "Use `hard` after the task boundary is already narrow. Permissions, broad-work splitting, and helper kind stay governed by the base task contract. When continuing useful work, prefer the same `task_id` with `resume=true`; change the task id only when the old workspace is stale or the work boundary truly changed.\n\n"
                                    "mode 只表示资源强度。先把任务边界拆清楚，再对具体难点或续作升 hard。"
                                ),
                            },
                            "expected_outputs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "List the concrete files the helper must deliver, up to 20 paths. Use the same path namespace the helper will edit. In project mode, use full staged paths such as `_env/src/pkg/file.py` for project files, not just basenames, so ownership, copyback, and output checks match the actual target. Helper completion reports include outputs_complete=true/false; use that evidence before deciding whether to resume.\n\n"
                                    "列出 helper 必须交付的文件；项目模式使用完整 `_env/...` 路径，便于归属、回写和验收一致。"
                                ),
                                "default": [],
                            },
                            "input_files": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Concrete files, staged paths, source ranges, or artifacts transferred or expected to be readable by this helper. "
                                    "Use `_env/...` paths in environment mode and include only files relevant to this helper's slice.\n\n"
                                    "传给 helper 或要求其读取的具体文件、路径、范围或产物。"
                                ),
                                "default": [],
                            },
                            "acceptance_checks": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Focused checks the helper should run or report against: compile/test commands, smoke tests, schema checks, coverage points, "
                                    "document sections, OCR completeness, or exact evidence requirements.\n\n"
                                    "该 helper 需要执行或汇报的聚焦验收项。"
                                ),
                                "default": [],
                            },
                        },
                        "required": ["task_id", "prompt"],
                    },
                    "description": "并行任务列表(action=spawn 时必填,1-16 个)",
                },
                "task_id": {
                    "type": "string",
                    "description": (
                        "Target helper task_id for action=kill. A kill request is cooperative; after killed_proc_id "
                        "or already_killed=true, continue from the resulting report or status rather than repeating the same interruption.\n\n"
                        "kill 的目标 task_id；协作中断已受理后，依据报告或状态继续。"
                    ),
                },
                "reason": {
                    "type": "string",
                    "enum": [
                        "self_report_cant_do",
                        "self_report_done",
                        "sibling_completed_first",
                        "content_deemed_useless",
                        "api_stall_emergency",
                    ],
                    "description": (
                        "Required for action=kill. Choose the evidence-backed reason:\n"
                        "- self_report_cant_do: helper reports infeasible work.\n"
                        "- self_report_done: helper reports completion while final ok result is not returned yet.\n"
                        "- sibling_completed_first: a sibling or paired helper completed the same task first.\n"
                        "- content_deemed_useless: the task is cancelled, superseded, or no longer useful.\n"
                        "- api_stall_emergency: heartbeat shows API stall for 60s+ without chunks. Healthy iter/tool progress is not an API stall.\n\n"
                        "kill reason 需匹配证据；API stall 只用于无 chunk 的卡死，不用于健康心跳。"
                    ),
                },
                "force": {
                    "type": "boolean",
                    "description": (
                        "**已弃用**(2026-05-02)。helper 不再支持硬杀,无论传什么都按"
                        "协作中断处理(响应里 force_downgraded=true 告知)。保留作向后兼容。"
                    ),
                    "deprecated": True,
                },
                "wait_window_sec": {
                    "type": "number",
                    "default": 90,
                    "description": (
                        "Optional wait window for spawn. Default is 90 seconds. Use 30-1800 seconds for a bounded wake-up, "
                        "then inspect still_running heartbeats, recent_tools, last_thought, and age to decide whether to wait, "
                        "resume the same task_id, cooperatively interrupt, or proceed from verified completed results. "
                        "Use longer windows for compile-heavy, benchmark, or multi-artifact tasks; use shorter windows when early fan-in is useful. "
                        "A nonpositive value waits for all helpers for legacy compatibility.\n\n"
                        "等待窗口用于定期介入；醒来后根据心跳决定等待、续作、中断或整合已验证结果。"
                    ),
                },
                "min_results_to_return": {
                    "type": "integer",
                    "description": (
                        "Optional early fan-in threshold for spawn. Return as soon as N helpers finish, or when wait_window_sec expires. "
                        "Use it when fast completed slices can be integrated while slower helpers continue. Valid range is 1..len(tasks)-1; "
                        "values at or above task count are ignored.\n\n"
                        "达到 N 个 helper 完成即可提前返回，便于边整合边等待慢任务。"
                    ),
                    "minimum": 1,
                },
                "force_blanket_resume": {
                    "type": "boolean",
                    "description": (
                        "(action=spawn 时可选,2026-05-04 加)全部 alive helper 一起 resume "
                        "时需设为 true 以确认非误操作。系统会检测≥3 个全 alive resume 并拦截,"
                        "要求先看心跳再精挑。如确认所有 helper 都应 resume,传 true 绕过。"
                    ),
                },
                "auto_final": {
                    "type": "boolean",
                    "description": (
                        "Legacy compatibility switch for historical paired hard-helper behavior. For current workflows, prefer explicit "
                        "same-task resume=true with the original base kind and mode='hard' when a narrow retry needs stronger resources.\n\n"
                        "旧 paired hard 兼容开关；当前流程优先显式同 task_id 续作并按需升 hard。"
                    ),
                },
                "helper_think": {
                    "type": "boolean",
                    "description": (
                        "(action=spawn 时可选,2026-05-08 加)设为 true 时本次 delegate 的所有 "
                        "coding/verify helper 启用思考(reasoning=low)。用于:helper 编译通过但 "
                        "运行 SEGFAULT/逻辑错误,需要模型深度推理才能定位的 bug。有成本,只在 "
                        "确认 helper 无思考搞不定时才开。"
                    ),
                },
            },
        },
    },
}


OFFICE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "office",
        "description": (
            "Office document read/write/edit. Auto-detects format from file extension.\n"
            "\n"
            "## Error responses\n"
            "On failure returns: {\"ok\": false, \"error\": \"<what went wrong>\"}\n"
            "READ the error message — it tells you exactly what to fix. Do NOT retry the same broken call.\n"
            "\n"
            "## Workflow for large documents (P150 自适应粒度)\n"
            "本工具采用自适应粒度: 初始允许单次 write/append 最多 96 个 blocks,\n"
            "单 block.text 最多 5000 字, 单次调用总文本最多 30000 字。\n"
            "若 LLM tool-call JSON 解析失败,系统会自动把上限折半(12 封底),\n"
            "并在下次调用的 arg_size_warning 中告知新上限。\n"
            "\n"
            "推荐做法:\n"
            "1. **一次写完整章节**(含 heading + paragraphs + image + table),减少 LLM round-trip。\n"
            "   论文/报告类文档常见: 一次 write 整篇骨架 + 一次 append 每个长章节。\n"
            "2. 仅在响应中看到 arg_size_warning.adaptive_shrinks_so_far > 0 时才拆小。\n"
            "3. **避免无谓的 replace_section**: 写之前想清楚结构;若同一 heading 反复 replace\n"
            "   3 次以上,说明心智模型有偏差,应该 read 整个文档一次,然后整段 append 重写。\n"
            "4. 文档已有主体、表格和图片后,只做一次最终 read/inspect 验收；没有明确事实错误就交付,不要为润色反复 append/read/replace。\n"
            "\n"
            "## Actions (17 total)\n"
            "### Read & Inspect\n"
            "- read: Extract text, tables, headings, image metadata from document. For large .docx files, use start_block/end_block/max_blocks to page the body without loading every paragraph at once.\n"
            "- extract_images: Export embedded images as .png/.jpg files to workspace\n"
            "- ocr_images: OCR embedded images independently from document text. Use image_offset/max_images to batch large documents, and save_to to write OCR text into a .txt/.md file for later read_file paging.\n"
            "  Use this when read showed image_count>0. Defaults: max_images=30, max_size_mb=50.\n"
            "### Create & Append\n"
            "- write: Create new document (overwrites if exists). Requires blocks/slides/sheets.\n"
            "- append: Add content to end of existing document\n"
            "### Incremental Edit — .docx (use block_index from read output)\n"
            "- replace_section: Replace content under a heading. Params: heading_text, blocks, keep_heading?\n"
            "- replace_block: Replace block at 0-based index. Params: index, blocks\n"
            "- delete_block: Delete N blocks from index. Params: index, count?\n"
            "- insert_block: Insert blocks before index. Params: index, blocks\n"
            "### Incremental Edit — .pptx\n"
            "- replace_slide: Replace slide at index. Params: index, slide (or slides)\n"
            "- insert_slide: Insert slides before index. Params: index, slides\n"
            "- delete_slide: Delete N slides from index. Params: index, count?\n"
            "### Incremental Edit — .xlsx\n"
            "- update_cells: Update specific cells. Params: updates[{sheet, ref, value}, ...]\n"
            "### Image\n"
            "- insert_image: Append single image to .docx. Params: image_path, width_inches?, caption?\n"
            "### Data Rigor Verification (P161/P162, see separate section below)\n"
            "- verify_numbers (.docx, .pptx): absolute number cross-check vs CSV\n"
            "- verify_rigor (.docx): 9-in-1 comprehensive rigor checker\n"
            "- verify_integrity (.xlsx): cross-sheet consistency + formula cached_value sanity\n"
            "\n"
            "## .docx block types\n"
            "{type:'heading', level:1-6, text:'...'}\n"
            "{type:'paragraph', text:'...', bold?, italic?, align?:'left|center|right|justify', font_size_pt?}\n"
            "{type:'list', items:[...], ordered?:true|false}\n"
            "{type:'table', rows:[[...]], header?:true, style?:'Light Grid Accent 1'}\n"
            "{type:'image', path:'...', width_inches?:6.0, caption?:'...'}\n"
            "{type:'equation', latex:'...', display?:false}  ← Word 原生公式 (see Formula section)\n"
            "{type:'page_break'}\n"
            "\n"
            "## .pptx slide layouts\n"
            "title:        {title, subtitle?}\n"
            "section:      {title, subtitle?}  ← use subtitle, NOT body\n"
            "title_content:{title, body:['bullet1','bullet2',...]}\n"
            "two_column:   {title, left:[...], right:[...]}\n"
            "image:        {title?, image:{path, width_inches?, caption?}}  ← top-level image, NOT in body\n"
            "table:        {title?, table:{rows:[[...]], header?}}\n"
            "blank:        {title?, text:'...'} or {title?, body:[{type:'text'|'bullets'|'image'|'table',...}]}\n"
            "\n"
            "## .xlsx sheet format\n"
            "{name:'Sheet1', rows:[[...]], header?:true, freeze_header?:true, column_widths?:[...]}\n"
            "Cells accept formula strings like '=SUM(A1:A10)'.\n"
            "append to existing sheet name → error unless replace_if_exists:true.\n"
            "\n"
            "## Rules\n"
            "- All image paths must already exist in workspace before use\n"
            "- Use office read/append/replace/update actions for .docx/.pptx/.xlsx instead of read_file or multi_edit.\n"
            "- Text and pictures can be read separately: use office(read, start_block/end_block/max_blocks) for body text, and office(ocr_images, image_offset/max_images/save_to) for image text. Then use read_file ranges on the saved OCR text.\n"
            "- **CRITICAL — Image embedding**: When writing a .docx/.pptx that references charts,\n"
            "  you MUST use type:'image' blocks to embed every chart PNG. Do NOT just generate\n"
            "  standalone .png files — the user expects a complete document with embedded visuals.\n"
            "  After generating chart*.png with python/matplotlib, immediately call office with\n"
            "  {type:'image', path:'chart_xxx.png'} blocks.\n"
            "- **Figure–text consistency**: If your paragraph text says '图 N 展示了 X', the very\n"
            "  next blocks must include {type:'image', path:...} for that figure. After writing,\n"
            "  call office(action='read') to verify every '图 N' reference has matching image.\n"
            "  (read output's figure_consistency field flags mismatches automatically.)\n"
            "- **Auto image embedding (.docx)**: markdown image syntax ![alt](path.png) in paragraph\n"
            "  text auto-converts to embedded image.\n"
            "- .docx/.pptx → pass blocks/slides; .xlsx → pass sheets\n"
            "- Use office tool instead of writing python-docx/pptx/openpyxl scripts — faster & more reliable\n"
            "\n"
            "## Formula support (.docx) — P151 OMML extended\n"
            "**方式 1 — inline $...$ / display $$...$$ 在 paragraph 文本中**: \n"
            "  简单公式自动转 Unicode (10^4 → 10⁴), 复杂公式自动降级为 PNG。\n"
            "**方式 2 — 显式 equation block (Word 原生公式, 可编辑可缩放, 推荐)**:\n"
            "  {type:'equation', latex:'10^4', display:false}  ← inline 大小\n"
            "  {type:'equation', latex:'\\\\sum_{i=1}^n x_i', display:true}  ← 居中放大\n"
            "  优先尝试 OMML, 不支持的公式自动降级 PNG。\n"
            "\n"
            "### Supported (OMML 原生, 可编辑)\n"
            "  ✓ ^/_ 上下标 (任意嵌套)\n"
            "  ✓ \\\\frac, \\\\dfrac, \\\\tfrac (支持嵌套, P151 修复)\n"
            "  ✓ \\\\sqrt{x}, \\\\sqrt[n]{x} (支持嵌套)\n"
            "  ✓ \\\\sum, \\\\int, \\\\iint, \\\\iiint, \\\\oint, \\\\prod, \\\\coprod, \\\\bigcup, \\\\bigcap\n"
            "     (带可选 _{}^{} 上下限, P151 新加)\n"
            "  ✓ \\\\lim, \\\\binom (P151 新加)\n"
            "  ✓ 希腊字母 \\\\alpha..\\\\omega \\\\Alpha..\\\\Omega\n"
            "  ✓ 运算符 \\\\pm \\\\times \\\\cdot \\\\div \\\\leq \\\\geq \\\\neq \\\\approx \\\\equiv \\\\to \\\\infty\n"
            "  ✓ 关系/集合 \\\\in \\\\notin \\\\subset \\\\supset \\\\cup \\\\cap \\\\emptyset\n"
            "  ✓ 逻辑 \\\\forall \\\\exists \\\\Rightarrow \\\\Leftrightarrow \\\\land \\\\lor \\\\neg\n"
            "  ✓ 函数名 \\\\sin \\\\cos \\\\tan \\\\log \\\\ln \\\\exp \\\\max \\\\min \\\\sup \\\\inf (转直立体文本)\n"
            "  ✓ 间距 \\\\, \\\\; \\\\quad \\\\qquad   |   \\\\left/\\\\right 自动去除\n"
            "### Fallback to PNG (matplotlib mathtext, 位图不可编辑)\n"
            "  \\\\stackrel, \\\\overline, \\\\underline 多数情况下能渲染\n"
            "### NOT supported (P160 preflight 会明确报错)\n"
            "  ✗ \\\\begin{align/cases/pmatrix/matrix/...} 环境 — 拆成多个 inline equation block\n"
            "  ✗ \\\\text{中文} — 中文应在普通 paragraph 里, 不要塞进公式\n"
            "  ✗ 自定义 \\\\newcommand / \\\\def\n"
            "Examples:\n"
            "  paragraph: 'The quadratic formula is $x = \\\\frac{-b \\\\pm \\\\sqrt{b^2-4ac}}{2a}$.'\n"
            "  equation block: {type:'equation', latex:'\\\\sum_{i=1}^n \\\\frac{1}{i^2} = \\\\frac{\\\\pi^2}{6}', display:true}\n"
            "\n"
            "## Data Rigor Verification (P161/P162) — for data-driven documents\n"
            "**触发条件**: 任务产物是数据驱动文档 (benchmark / 实验论文 / 财务分析 / 对比研究等)\n"
            "且工作区有 .csv / .xlsx 数据源。**强制**在交付前调一次 verify_rigor 验全绿。\n"
            "\n"
            "### verify_numbers (.docx & .pptx)\n"
            "Absolute number cross-check. Params:\n"
            "- csv_paths: list[str], 必填\n"
            "- tolerance: float = 0.05 (5% 相对误差)\n"
            "Returns: mismatches[] with paragraph_idx / slide_idx + best_csv_match.\n"
            "Use case: 快速排查论文里凭印象写的绝对数字与 CSV 不符。\n"
            "\n"
            "### verify_rigor (.docx) — 9-in-1 comprehensive checker\n"
            "All params of verify_numbers, plus:\n"
            "- pivot_col (e.g. 'algorithm'), pivot_values (e.g. ['ASMT','RBTREE','AVL','BPTREE','SKIPLIST'])\n"
            "- pivot_aliases (dict): {'BPTREE': ['B+树','B+ 树']} — 中英文/缩写互通\n"
            "- comparison_assertions (RECOMMENDED, strongest ratio verification):\n"
            "    [{context_keyword:'比红黑树快', csv_filter:{operation:'batch_del_small',\n"
            "      N:'1000000', distribution:'random'}, baseline_pivot:'RBTREE',\n"
            "      target_pivot:'ASMT', claimed_ratio:9.2, value_col:'time_ms', tolerance:0.1}, ...]\n"
            "    工具检查: (a) context_keyword 在 docx 存在, (b) CSV 锁定 baseline/target 行,\n"
            "    (c) 算 baseline/target ratio 对比 claimed_ratio。捕获 '47 倍' 这种编造。\n"
            "- internal_facts (regex-based 跨段一致性):\n"
            "    [{name:'batch_del_large vs RBTREE',\n"
            "      pattern:r'batch[_]?del[_]?large[^比。]{0,80}比红黑树.{0,15}?快\\s*约?\\s*(\\d+(?:\\.\\d+)?)\\s*倍',\n"
            "      expected_value:2.24, tolerance:0.05}, ...]\n"
            "    工具按 regex 抓数字与期望值对比。捕获 abstract/body/conclusion 同事实点数字不一致。\n"
            "    用 [^比。] 边界字符类防止跨语句匹配。\n"
            "- expect_reproducibility_metadata (bool, default False): set True for benchmark papers.\n"
            "    检查论文是否含 random_seed / repeat_count / environment / input_distribution。\n"
            "- scaling_n_col / scaling_value_col / scaling_group_cols / scaling_super_linear_threshold:\n"
            "    跨 N 复杂度观察 (O(N log N) 期望 vs 实测)。\n"
            "\n"
            "9 sub-checks (all 0 = ✅ ready to ship):\n"
            "  L1 number_check   绝对数字 vs CSV\n"
            "  L2 ratio_check    比率论断 (X 倍) vs CSV pairwise\n"
            "  L3 cherry_pick    对比段漏了某 pivot 值\n"
            "  L4 unit_consistency  单位混用 (ms/s/μs/ns)\n"
            "  L5 assertion_check 结构化断言违反 ← 最强\n"
            "  L6 methodology_check 方法学声明 (3 次/中位数/std) vs CSV 列\n"
            "  L7 scaling_check  跨 N 复杂度观察 (observational)\n"
            "  L8 internal_consistency  跨段 regex 一致性\n"
            "  L9 reproducibility 复现元数据\n"
            "\n"
            "### verify_integrity (.xlsx)\n"
            "Cross-sheet consistency + formula cached_value sanity. Params:\n"
            "- key_columns (e.g. ['region']), value_columns (e.g. ['revenue']) — 同 key 的 value\n"
            "  在不同 sheet 出现时必须一致。捕获 财务模型 link 漏更新类错误。\n"
            "- check_formulas (default True): 验证 formula 单元格的 cached value 非空,\n"
            "  避免 openpyxl 写完没经 Excel 重算保存导致下游读 0。\n"
            "\n"
            "### Iteration\n"
            "verify_* 报错 → replace_section/replace_block 修 → 再调一次 → 直到全绿。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "read", "write", "append",
                        "replace_section", "replace_block",
                        "delete_block", "insert_block",
                        "replace_slide", "insert_slide", "delete_slide",
                        "update_cells",
                        "extract_images", "ocr_images", "insert_image",
                        "verify_numbers",
                        "verify_rigor",
                        "verify_integrity",
                    ],
                },
                "path": {
                    "type": "string",
                    "description": "工作区相对路径,后缀决定格式:.docx / .pptx / .xlsx (.xlsm 也支持)",
                },
                "title": {
                    "type": "string",
                    "description": "(.docx write) 可选封面标题(level 0 大标题)",
                },
                "blocks": {
                    "type": "array",
                    "description": "(.docx write/append/replace_section/replace_block/insert_block) 内容块数组。复杂内容请分小批追加:每次 append 建议 2-8 个 blocks;表格、大段含引号文本、公式说明不要一次塞入巨大 JSON。",
                    "items": {"type": "object"},
                },
                "slides": {
                    "type": "array",
                    "description": "(.pptx write/append/replace_slide/insert_slide) 幻灯片数组",
                    "items": {"type": "object"},
                },
                "slide": {
                    "type": "object",
                    "description": "(.pptx replace_slide) 单页字典(等价于 slides=[slide])",
                },
                "sheets": {
                    "type": "array",
                    "description": "(.xlsx write/append) 工作表数组",
                    "items": {"type": "object"},
                },
                "updates": {
                    "type": "array",
                    "description": "(.xlsx update_cells) 单元格更新列表 [{sheet, ref, value}, ...]",
                    "items": {"type": "object"},
                },
                "index": {
                    "type": "integer",
                    "description": "(replace_block/delete_block/insert_block/replace_slide/insert_slide/delete_slide) 0-based 块/页序号",
                },
                "count": {
                    "type": "integer",
                    "description": "(delete_block/delete_slide) 连续删除多少个,默认 1",
                },
                "heading_text": {
                    "type": "string",
                    "description": "(.docx replace_section) 要替换的章节标题文字(strip 后精确匹配)",
                },
                "keep_heading": {
                    "type": "boolean",
                    "description": "(.docx replace_section) 是否保留原 heading,默认 true",
                },
                "create_sheet_if_missing": {
                    "type": "boolean",
                    "description": "(.xlsx update_cells) sheet 不存在时是否新建,默认 false",
                },
                "sheet_name": {
                    "type": "string",
                    "description": "(.xlsx read) 只读指定 sheet,缺省读全部",
                },
                "out_dir": {
                    "type": "string",
                    "description": "(extract_images) 输出子目录,默认 '<stem>_images/'",
                },
                "start_block": {
                    "type": "integer",
                    "description": "(.docx read) 0-based 起始正文块索引；用于大 Word 文档分段读取。",
                },
                "end_block": {
                    "type": "integer",
                    "description": "(.docx read) 0-based 结束正文块索引(含)；省略表示读到文档末尾或 max_blocks。",
                },
                "max_blocks": {
                    "type": "integer",
                    "description": "(.docx read) 最多返回多少个正文块；用于分段读取大 Word 文档。",
                },
                "max_images": {
                    "type": "integer",
                    "description": "(ocr_images) 本批最多 OCR 几张图, 默认 30. 大文档可配合 image_offset 分批处理。",
                },
                "image_offset": {
                    "type": "integer",
                    "description": "(ocr_images) 0-based 起始图片偏移；当返回 has_more_images/next_image_offset 时继续下一批。",
                },
                "max_size_mb": {
                    "type": "number",
                    "description": "(ocr_images) 单图最大 MB, 默认 50.0 (基本不限). 真正巨型图 (>50MB) 才跳过.",
                },
                "image_path": {
                    "type": "string",
                    "description": "(.docx insert_image) 工作区相对路径的图片",
                },
                "width_inches": {
                    "type": "number",
                    "description": "(insert_image / image block) 宽度英寸,docx 默认 6.0,pptx 默认 8.0",
                },
                "caption": {
                    "type": "string",
                    "description": "(insert_image / image block) 图片下方说明文字(斜体小号)",
                },
                "csv_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "(verify_numbers / verify_rigor) 核对文档内数字所依据的 CSV/源数据文件路径列表",
                },
                "pivot_col": {
                    "type": "string",
                    "description": "(verify_rigor) 透视/分组列名,用于跨表(sheet)一致性与公式健全性检查",
                },
                "source_path": {
                    "type": "string",
                    "description": "(verify_against_source) 对照源文件路径(如 OCR 文本),检查 docx 内容与源的 token 覆盖率、防 OCR 幻觉编造",
                },
                "source_text": {
                    "type": "string",
                    "description": "(verify_against_source) 直接给出源文本(替代 source_path 从文件读)",
                },
                "number_pattern": {
                    "type": "string",
                    "description": "(verify_numbers / verify_rigor) 提取数字的正则;默认匹配常见数字格式",
                },
                "tolerance": {
                    "type": "number",
                    "description": "(verify_numbers / verify_rigor) 数值比对容差,默认 0.05",
                },
                "threshold": {
                    "type": "number",
                    "description": "(verify_rigor) 主判定阈值,默认 0.30",
                },
                "warn_threshold": {
                    "type": "number",
                    "description": "(verify_rigor) 告警阈值,默认 0.60",
                },
                "pivot_aliases": {
                    "type": "object",
                    "description": "(verify_rigor) 透视列别名映射,处理表头/CSV 列名不一致",
                },
                "pivot_values": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "(verify_rigor) 限定只核对的透视值子集",
                },
                "comparison_assertions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "(verify_rigor) 需校验的对比断言列表",
                },
                "internal_facts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "(verify_rigor) 文档内部应自洽的事实/数字列表",
                },
                "scaling_group_cols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "(verify_rigor 复杂度检查) 按哪些列分组,如 ['algorithm','operation']",
                },
                "scaling_n_col": {
                    "type": "string",
                    "description": "(verify_rigor) 规模列名,默认 'N'",
                },
                "scaling_value_col": {
                    "type": "string",
                    "description": "(verify_rigor) 耗时/数值列名,默认 'time_ms'",
                },
                "scaling_super_linear_threshold": {
                    "type": "number",
                    "description": "(verify_rigor) 超线性增长判定阈值,默认 1.5",
                },
                "expect_reproducibility_metadata": {
                    "type": "boolean",
                    "description": "(verify_rigor) 是否要求文档含可复现性元数据(种子/环境等)",
                },
                "edits": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "(replace_blocks 批量编辑) 多处编辑指令列表",
                },
                "save_to": {
                    "type": "string",
                    "description": "(ocr_images/verify_* 等) 把长 OCR 文本或校验报告另存到工作区相对路径，再用 read_file 分段读取。",
                },
                "max_workers": {
                    "type": "integer",
                    "description": "(ocr_images) 并行 OCR 最大并发数,默认 4",
                },
                "per_image_timeout": {
                    "type": "integer",
                    "description": "(ocr_images) 单张图片 OCR 超时秒数,默认 60",
                },
            },
            "required": ["action", "path"],
        },
    },
}


# ─── 2026-05-02 part21:TodoWrite/TodoRead 工具(参考 Claude Code) ─────
# 核心机制:让模型把"任务分解"外化到工作区。复杂任务先列 todo,过程中
# mark complete。这样模型不会:漏事 / 死磕单点 / 忘记原计划。
# Claude Code 实测最有效的"thinking out loud"机制。
TODO_WRITE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "todo_write",
        "description": (
            "管理任务清单。**复杂任务的第一个工具调用应该是 todo_write,把任务拆成 todos**。\n"
            "之后每完成一项就再次 todo_write 把那项 mark completed。这是把思维外化,"
            "避免漏事/死磕单点/忘记原计划。\n"
            "\n"
            "## 何时用(强烈建议)\n"
            "- 任务有 3+ 个明显步骤(读代码 → 找根因 → 改 → 测试 → 写报告)\n"
            "- 调试 bug 需要尝试多个假设(假设 A 测一遍、假设 B 测一遍)\n"
            "- 多文件协作(改 huffman.c 同时也要改 main.c)\n"
            "- 用户给的任务清单(实现 A + 实现 B + 写文档)\n"
            "\n"
            "## 何时**不**用\n"
            "- 单步任务(修一个 typo / 答一个问题)\n"
            "- 闲聊\n"
            "\n"
            "## 规则\n"
            "- 每个 todo 用 5-30 字描述具体做什么(不要含糊)\n"
            "- status: `pending`(待办) / `in_progress`(正在做) / `completed`(完成)\n"
            "- **同时只能有 1 个 in_progress** —— 即使你并行派多个 helper,也只能把当前主控步骤标为 in_progress,其他并行分支保持 pending\n"
            "- 完成立刻 mark complete,不要等任务全部完成才一次更新\n"
            "- 中途计划有变 → 直接 todo_write 重写整个 list\n"
            "\n"
            "## 例子\n"
            "```\n"
            "todo_write(todos=[\n"
            "  {\"id\": \"1\", \"content\": \"read_file huffman.c 看完整代码\", \"status\": \"in_progress\"},\n"
            "  {\"id\": \"2\", \"content\": \"找 codes[i].code 所有写入路径\", \"status\": \"pending\"},\n"
            "  {\"id\": \"3\", \"content\": \"补漏的赋值 + 编译测试\", \"status\": \"pending\"},\n"
            "  {\"id\": \"4\", \"content\": \"写报告说明根因\", \"status\": \"pending\"},\n"
            "])\n"
            "```\n"
            "返回带 ☐/▶/✓ 三态可视化展示。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "完整 todo list(每次 write 都是覆盖式)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "唯一 id,可用 '1' '2' '3' 简单数字",
                            },
                            "content": {
                                "type": "string",
                                "description": "todo 内容(5-30 字,具体可执行)",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "状态。同时只能 1 个 in_progress",
                            },
                        },
                        "required": ["id", "content", "status"],
                    },
                },
            },
            "required": ["todos"],
        },
    },
}


TODO_READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "todo_read",
        "description": (
            "读取当前 todo list。每隔几个工具调用看一眼当前 todos 状态,"
            "确认还在原计划上、不是跑偏了。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}


# ─── 2026-05-03 Bug E:commit_to_main 工具(双层工作区)─────────────────
# 主线程 / helper 工作在 .temp/ 临时工作区,主工作区是干净的核心成果区。
# 正常情况下 plan.deliverables 列出的文件会在对话结束时**自动提升**到主区,
# 但有些场景需要手动 commit:
#   - 多步任务:阶段性成果想立刻沉淀,不等整个对话结束
#   - 长链路 helper:某个明显是核心成果的文件想提前固化
#   - 主动归档:模型判断"这份是 deliverable",显式声明
# 不要用于:中间 .json / 调试 .c / benchmark scratch ——这些留 .temp 不污染主区
COMMIT_TO_MAIN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "commit_to_main",
        "description": (
            "Promote files from the temporary workspace to the main workspace as core conversation outputs.\n"
            "\n"
            "## Promotion Standard\n"
            "- Use for verified deliverables, user-requested final files, or milestone artifacts that should persist before the conversation ends.\n"
            "- Keep scratch data, debug probes, benchmark internals, and temporary runners in the temporary workspace.\n"
            "- Verify files before promotion. For compression, codec, serialization, or round-trip code, include an actual round-trip or hash/byte comparison in the evidence before promotion.\n"
            "- When deliverable status is uncertain, list the file in the plan and let normal final promotion handle it.\n"
            "- Paths are relative to the current workspace root. The response lists promoted and skipped files.\n\n"
            "commit_to_main 只提升已验证的核心成果；临时证据留在临时区，序列化/压缩类产物需有往返验证。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要提升的文件路径列表(相对工作区根目录)",
                },
            },
            "required": ["paths"],
        },
    },
}


# ─── v2 架构: fetch_to_temp — 按需从永久区/历史快照复制到 temp ──
FETCH_TO_TEMP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fetch_to_temp",
        "description": (
            "将文件从永久工作区或上一轮历史快照复制到当前临时工作区(.temp/)。\n"
            "三层隔离模型的核心原语：不能直接访问永久区和 .prev/，必须通过此函数复制。\n"
            "\n"
            "何时调:\n"
            "- 需要看上一轮的 helper 输出或中间文件(source='prev')\n"
            "- 需要把永久区的核心成果拉到 temp 继续加工(source='main')\n"
            "- helper 需要获取主区或历史区的参考文件\n"
            "\n"
            "调用: fetch_to_temp(source='main'|'prev', paths=['file1.c', 'dir/'])\n"
            "返回 copied/skipped 列表。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["main", "prev"],
                    "description": "来源: 'main'(永久工作区) 或 'prev'(上一轮 .prev/ 历史快照)",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要复制的文件/目录相对路径列表",
                },
            },
            "required": ["source", "paths"],
        },
    },
}


# ─── 2026-05-03 新工具 recall_thread:防上下文淹没 ─────────────────────
# 长 tool 链路里(20+ iter)模型常常迷失原始目标 —— tool result 把"用户最初
# 问的什么"挤出注意力窗口。这个工具让模型显式拿回:
#   1. 用户原始 message(单条还原,不是搜历史)
#   2. plan(这次 round2 的目标 + key_points + deliverables)
#   3. 当前 todos 状态(从 todo_read 自动整合)
#   4. 已经做了什么的简短摘要(从最近 N 次 tool call 提取)
# 类似 Claude Code 的"check in" 模式 — 主动跳出 tool 循环,回到目标层。
RECALL_THREAD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "recall_thread",
        "description": (
            "Recall the current task contract and progress when a long toolchain may be losing focus. "
            "Use this for multi-step work after several tool calls, after repeated failures, before a milestone handoff, "
            "or before final synthesis when the original request, acceptance points, todo state, or deliverables are no longer fresh in context. "
            "It returns the original current user request, current plan snapshot, todo state, and a direction check. "
            "This is task alignment, not semantic memory search; use expand/search tools for older conversation memory.\n\n"
            "长链路任务用于回看本轮原始请求、计划、todo 和交付契约；不是历史记忆检索。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}


# ─── 2026-05-03 新工具 progress_note:helper 主动汇报中间状态 ───────────
# helper 跑 long-running 任务时,主线程通过 delegate/processes 监控状态。
# progress_note 让 helper 在关键节点写一段简短进度,主线程可通过
# processes(action=peek) 立即拿到最新 progress(无需等 helper 完成)。
CONTINUE_TOOLCHAIN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "continue_toolchain",
        "description": (
            "Pull the previous round2 toolchain cache into the current toolchain as prior evidence. "
            "Use this once near the start of a multi-stage task when the user asks to continue, "
            "deepen, repair, verify, or build on work from earlier rounds and the current prompt "
            "does not contain enough concrete tool evidence. The returned text is a compact prefix "
            "of earlier tool calls, helper results, files, errors, and verification facts. "
            "After this call the old cache is cleared, and this tool becomes unavailable for the "
            "rest of the current round to prevent self-continuing into updated cache. "
            "Use it for continuing an execution chain; for simple chat, fresh one-shot tasks, or general memory search, use "
            "recall/search tools for semantic memory and use this only for continuing an execution "
            "chain."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why previous toolchain evidence is needed for this round.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum chars to return, default 60000, capped by the service.",
                    "default": 60000,
                },
            },
            "required": ["reason"],
        },
    },
}


PROGRESS_NOTE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "progress_note",
        "description": (
            "Helper-only progress heartbeat for the main process. Use it during long helper work when a milestone "
            "is completed, a non-fatal obstacle changes the approach, or the task has been running for several "
            "minutes without a final report. Keep the note factual, concrete, and short; describe current evidence, "
            "current action, and the next useful step rather than mood or filler.\n\n"
            "helper 专用进度心跳；长任务里用一句事实说明当前进展、阻塞和下一步。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Short factual progress note.\n\n一句话事实进度。",
                },
            },
            "required": ["text"],
        },
    },
}


REQUEST_RESOURCE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "request_resource",
        "description": (
            "Helper-only resource wait. Call this when progress is blocked by a missing external artifact, evidence "
            "source, or specialized resource. The helper freezes with a preserved "
            "workspace and reports the missing resource, useful partial evidence, expected outputs, and the concrete "
            "condition for resuming. The main process decides "
            "whether an existing resource is enough, whether to spawn a resource helper, or whether to refuse/close "
            "the request. Keep the missing-resource state in the request fields, not in user-facing deliverable text.\n\n"
            "helper 专用资源等待；缺资源时冻结并说明所需资源、已有证据和唤醒条件，由主线程协调。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["code", "edit", "draw", "verify", "tts", "read", "project_map", "file_summary", "impact_review", "inventory"],
                    "description": "Resource helper kind requested from the main process.\n\n需要的资源 helper 类型。",
                },
                "reason": {
                    "type": "string",
                    "description": "What resource is missing and where progress is blocked.\n\n缺少资源及阻塞点。",
                },
                "needed_outputs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Desired files or evidence from the resource helper.\n\n希望资源 helper 产出的文件或证据。",
                    "default": [],
                },
                "resume_instruction": {
                    "type": "string",
                    "description": "Concrete instruction to use when this helper is resumed after resource resolution.\n\n资源处理后的续作指令。",
                },
            },
            "required": ["kind", "reason"],
        },
    },
}


# ── 2026-05-04 Claude Code 移植:ask_user_question ─────────────────
# helper 遇到歧义时可主动向主线程/用户提问,而非盲猜后失败。
# Claude Code AskUserQuestionTool 的等价移植。
ASK_USER_QUESTION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_user_question",
        "description": (
            "Ask the main process or user a concise clarifying question when progress depends on a missing decision, "
            "external input, or genuinely ambiguous choice. First use available context, file/search evidence, and "
            "relevant skills when they can resolve the uncertainty. A good question names the ambiguity, gives two to "
            "four executable options when useful, and explains the practical consequence briefly. After the call, the "
            "main process will provide the answer or best available decision in a later turn.\n\n"
            "用于缺少关键决策或外部信息时提问；先查已有证据，问题要具体并给可执行选项。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Complete concise question to ask.\n\n一句完整、具体的问题。",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional short answer choices; put the recommended practical option first.\n\n可选答案，推荐项放前面。",
                },
                "context": {
                    "type": "string",
                    "description": "Brief reason why this decision is needed.\n\n为什么需要这个决策。",
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
}


# ── 2026-05-09 Patch 22: inspect_file 工具 ────────────────────
#
# 主线程禁止运行 .py 脚本,但 helper 经常产出 docx/pptx/xlsx/png 这类二进制文件。
# 没有 inspect_file 时主线程只能盲信 helper.report,无法核实"4 张图实际有几张"。
# 这个工具纯 ZIP / header 解析,不执行任何用户代码,给主线程结构化元数据。
#
INSPECT_FILE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "inspect_file",
        "description": (
            "检查工作区内文件的结构化元数据,**主线程验证 helper 二进制产物的关键工具**。\n"
            "\n"
            "## 何时用\n"
            "- helper 报告 ok=true + 产出了 docx/pptx/xlsx/png/pdf 等二进制文件\n"
            "- 你需要确认产物的内部结构(段落数 / 表格数 / 图片数 / 幻灯片数 / 行数 / 尺寸 / 时长)\n"
            "- 用 read_file 看不到二进制内部,只能拿到 base64;用 inspect_file 能拿到结构元数据\n"
            "- 验收前最后一步:把 helper 报告说的'4 张图'与 inspect_file 给的 image_count 对照\n"
            "\n"
            "## 何时不用\n"
            "- 文本文件(.c/.h/.py/.md/.txt 等):用 read_file 直接读内容\n"
            "- 检查文件存在/大小:workspace(action='list') 或 workspace.locate 已足够\n"
            "\n"
            "## 支持类型\n"
            "- **docx**: paragraph_count, table_count, image_count, image_files[], text_chars, text_preview\n"
            "- **pptx**: slide_count, image_count, slide_titles[]\n"
            "- **xlsx**: sheet_count, sheet_names[], rows_per_sheet[]\n"
            "- **pdf**: version, page_count\n"
            "- **png/jpg/gif/bmp/webp**: format, width, height\n"
            "- **wav**: channels, sample_rate, bits_per_sample, duration_seconds\n"
            "- **text 类**(.md/.json/.c/.py 等): line_count, char_count, preview_first_lines[]\n"
            "- **其他二进制**: 文件头 16 字节 hex(给你识别陌生格式用)\n"
            "\n"
            "## warnings\n"
            "When the result contains warnings, treat them as acceptance evidence about abnormal structure "
            "such as empty files, missing paragraphs, missing images, or very small media. Repair, reassign, "
            "or report the verified gap before presenting the artifact as a deliverable.\n"
            "\n"
            "inspect_file 用于验收结构；warnings 表示产物结构异常，需修复、重派或说明缺口后再交付。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "工作区内的相对路径,如 'paper.docx' 或 'helper_xxx_chart.png'。",
                },
            },
            "required": ["path"],
        },
    },
}


# ── OCR 工具(离线图片文字识别) ───────────────────────────────
OCR_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ocr",
        "description": (
            "Offline visual/document text reading for images, PDFs, and Office containers. Use it when the current task needs text evidence from a concrete visual or document file. "
            "Use read_file, search, or code_index for source code, Markdown, CSV, JSON, logs, and other plain text. "
            "For conceptual OCR/tooling questions, answer or troubleshoot directly from context. "
            "tier is an internal evidence-effort setting; decide upgrades from user purpose, result quality, risk, and next_tier. "
            "User-facing replies should present confirmed content and uncertainty without internal engine or tier labels.\n"
            "\n"
            "## Input\n"
            "- Prefer image_path for workspace images, PDFs, and Office files.\n"
            "- Use image_base64 only for small images without a file path.\n"
            "- PDFs are rendered page by page. Office files can be OCRed as containers; for embedded-image OCR, prefer office(action='ocr_images').\n"
            "- For large images, long images, long PDFs, or Office image text, set save_to and page the saved text with read_file line ranges.\n"
            "\n"
            "## Evidence Effort\n"
            "- fast is the default quick evidence path; balanced and accurate are slower and stronger.\n"
            "- allow_upgrade=true upgrades by quality signal up to max_tier.\n"
            "- Rough reading, formal quotation, numbers/tables/formulas, readability judgment, user challenge, and deliverable evidence require different confidence. Stop when the evidence supports the purpose; upgrade when evidence remains insufficient and next_tier exists.\n"
            "- balanced/accurate reuse the single hot MinerU service and wait serially.\n"
            "\n"
            "## Result Semantics\n"
            "- text is recognized text; it is empty when no text is recognized.\n"
            "- next_tier means stronger recognition is available; no_stronger_tier means the configured ceiling was reached.\n"
            "- quality_flags and folded_spans indicate repeated abnormal text was folded by the OCR layer.\n"
            "- OCR text is literal recognized text rather than full visual understanding. Inference should stay grounded in visible text and uncertainty.\n"
            "- math_quality_warning means formulas, symbols, or matrices may be incomplete; use context, upgrade when useful, or mark OCR uncertainty.\n\n"
            "OCR 用于具体图片/PDF/Office 的视觉文字证据；大文件保存到文本后分段读；结果按字面证据和不确定性使用，内部档位不面向用户。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "[首选] 工作区内的图片/PDF/Office 文档路径, 如 'screenshot.png' / 'paper.pdf' / 'doc.docx'. PDF/docx/pptx/xlsx 会内部 OCR. 所有大小都安全。与 image_base64 二选一。",
                },
                "image_base64": {
                    "type": "string",
                    "description": "图片的 base64 编码(可含 data URI 前缀)。≥16KB 会自动走临时文件兜底,但仍建议把图片写到工作区后用 image_path。",
                },
                "tier": {
                    "type": "string",
                    "enum": ["fast", "balanced", "accurate"],
                    "description": "Internal visual evidence effort. Default fast; upgrades depend on purpose, quality, and task consequence.\n内部视觉证据强度。",
                },
                "allow_upgrade": {
                    "type": "boolean",
                    "description": "默认 false。设 true 时按质量信号自动逐级升档到 max_tier；仍只复用单例 MinerU 热服务并串行等待。",
                },
                "max_tier": {
                    "type": "string",
                    "enum": ["fast", "balanced", "accurate"],
                    "description": "允许使用的最高 OCR 档位；低于返回的 next_tier 时会返回 no_stronger_tier。默认 accurate。",
                },
                "return_raw": {
                    "type": "boolean",
                    "description": "默认 false。若 OCR 层折叠了重复异常,默认只返回 raw_text_path；设 true 才返回 raw_text_preview。",
                },
                "save_to": {
                    "type": "string",
                    "description": "把 OCR 文本保存到工作区相对 .txt/.md 路径，并只返回预览与行数，便于大图/长图分段读取。",
                },
            },
            "required": [],
        },
    },
}


# ── TTS 工具(离线语音合成 + 声音推送) ─────────────────────────
TTS_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tts",
        "description": (
            "Offline TTS audio generation for user-facing audio artifacts or final voice files. "
            "For conceptual TTS questions, logs, scheduling, or ordinary voice-reply preference, answer directly or let the normal final voice-output layer handle it.\n"
            "\n"
            "## Boundary\n"
            "- Voice-reply requests set the final reply form; keep the final text short and spoken, then let the post-processing voice layer handle it.\n"
            "- Audio-file requests use this tool to generate an audio artifact and return paths for deliverables.\n"
            "- Persona voice identity and voice policy are stable runtime settings. When the user asks to change voice identity inside the conversation, explain the boundary and continue with valid audio generation if possible.\n"
            "\n"
            "## Text Capabilities\n"
            "text may include non-linguistic tokens: [laughter], [sigh], [confirmation-en], [question-en], "
            "[question-ah], [question-oh], [question-ei], [question-yi], [surprise-ah], [surprise-oh], "
            "[surprise-wa], [surprise-yo], [dissatisfaction-hnn]。\n"
            "Pronunciation can be adjusted with pinyin+tone digits for Chinese or uppercase CMU phonemes for English. "
            "Normalize numbers into spoken form when useful.\n"
            "\n"
            "## Delivery\n"
            "push=true is a legacy parameter; generation returns a file path rather than pushing automatically. "
            "If the audio is this round's final voice reply, set final JSON `voice_reply_file` to the wav path. "
            "If the audio is a file attachment or standalone artifact, list it in `plan.deliverables`.\n"
            "\n"
            "Use this tool for explicit audio-file generation, long-text narration into a file, or audio attachments for documents and reports.\n\n"
            "TTS 用于生成音频文件或最终语音文件；普通语音回复由后置语音层处理，角色声音策略保持运行时设置。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "要合成的文本。支持中文/英文/日文等。可直接插入非语言符号如 [laughter]/[sigh]；可用拼音声调或 CMU 音素修正发音。",
                },
                "language": {
                    "type": "string",
                    "description": "语言名称,如 'Chinese'/'English'/'Japanese'。默认自动检测——但混合中英文本建议显式指定避免拼音化中文。",
                },
                "output_filename": {
                    "type": "string",
                    "description": "可选。用户明确要求的输出文件名,如 hello.wav。只允许工作区内的 .wav/.mp3/.m4a/.ogg 文件名; 不填则系统自动命名。",
                },
                "speed": {
                    "type": "number",
                    "description": "语速因子。>1 更快,<1 更慢。默认 1.0。",
                },
                "push": {
                    "type": "boolean",
                    "description": "[未实现] 历史遗留参数。设 true 不会推送语音,仅返回提示信息。要让用户听到语音见 description 里的三种正确做法。默认 false。",
                },
            },
            "required": ["text"],
        },
    },
}


# ── 主线程工具集(v2 架构) ────────────────────────────────────
# 主线程只做编排 + 信息收集 + 工作区维护，不自己写代码/编辑文件/跑命令。
# bash / python / edit_file / multi_edit / insert_in_file / office 已移除 —
# 所有实现工作必须通过 delegate 派发给 helper。
# 主线程专用 workspace schema —— write 仅文档,run 仅文件管理命令
MAIN_WORKSPACE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace",
        "description": (
            "Main-process workspace file management. The main process coordinates, inspects, and manages lightweight files; substantial implementation, command-heavy analysis, source authoring, and tests belong to delegate helpers.\n"
            "\n"
            "`action` is required.\n"
            "\n"
            "## Write Boundary\n"
            "write is for .md, .json, .txt, and Makefile. Source files, scripts, tests, benchmark code, and substantial project files are produced through delegate helpers and then verified/applied by the main process.\n"
            "\n"
            "## actions\n"
            "- mkdir: create a workspace subdirectory.\n"
            "- write: write lightweight document/metadata files ending in .md/.json/.txt or named Makefile.\n"
            "- run: file-management commands such as dir/ls/copy/move/mkdir/type. Use delegate(kind='code') for gcc, python, tests, builds, and computation.\n"
            "- Use delegate file_map/main_available_files/copy_stats for helper outputs; resume or regenerate missing outputs instead of copying or moving files from `.temp/_delegate_*`.\n"
            "\n"
            "## run timeout\n"
            "Set timeout_sec explicitly from 1 to 300 seconds. The default is 1 second.\n\n"
            "主进程 workspace 只做轻量文件管理；源码、脚本、测试、构建和计算交给 code helper。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["mkdir", "write", "run", "locate"],
                    "description": "Required action: mkdir, write lightweight docs, run file-management commands, or locate files.\n必填动作字段。",
                },
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path. Required for mkdir/write; write targets are .md/.json/.txt or Makefile.\n工作区相对路径。",
                },
                "content": {
                    "type": "string",
                    "description": "File content for action=write.\n写入内容。",
                },
                "command": {
                    "type": "string",
                    "description": "File-management command for action=run. Use delegate(kind='code') for gcc, python, tests, builds, and computation.\n文件管理命令。",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "命令最长执行秒数(1~300)。dir/ls→5,copy→10。默认1。",
                    "minimum": 1,
                    "maximum": 300,
                },
                "pattern": {
                    "type": "string",
                    "description": "文件名匹配模式(action=locate 必须)。支持 glob(*.docx/chart*.png),不含通配符时自动模糊匹配(*substring*)。",
                },
            },
            "required": ["action"],
        },
    },
}


from app.llm.tools.tool_schema_descriptions import apply_english_schema_descriptions


apply_english_schema_descriptions(globals())

