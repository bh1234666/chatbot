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
            "batch multiple IDs when useful. IDs are opaque handles from the current memory index; semantic guesses "
            "such as topic names are not evidence IDs and return empty results.\n\n"
            "按当前记忆索引里的真实 ID 展开温记忆；主题词猜测不是 ID。"
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
            "Use depth=1 for direct neighbors and depth=2 only when deeper context is needed. IDs are opaque handles "
            "from the current memory index; semantic guesses such as topic names are not evidence IDs and return empty results.\n\n"
            "按当前记忆索引里的真实 ID 和深度展开冷记忆；主题词猜测不是 ID。"
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
            "Expand knowledge-base nodes from condensed shared history. Usage mirrors expand_cold, but the scope is KB. "
            "Use concrete KB IDs from the current index; semantic guesses such as topic names are not evidence IDs.\n\n"
            "用当前索引里的真实 KB ID 展开知识库节点；主题词猜测不是 ID。"
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
            "and locate files. `action` is required; a missing action returns `workspace_action_required` without executing the tool.\n\n"
            "Main-process boundary: the main process coordinates, searches, records narrow facts, and applies tiny patches. Substantive implementation, "
            "documents, charts, algorithms, and generated artifacts should be delegated to helpers. Main-process `.py` writes are for small "
            "verification/probing scripts only.\n\n"
            "Actions: mkdir creates a workspace subdirectory; write creates or rewrites small text/JSON/Markdown or tiny verify scripts; "
            "run executes bounded commands and returns stdout/stderr/returncode; locate finds files by name or glob.\n\n"
            "Workspace files remain available for later search/read. Writes outside the workspace are blocked; risky system commands are blocked. "
            "Use processes tools for background process control.\n\n"
            "workspace 用于轻量文件和命令；主进程只编排、记录窄事实，实质实现和自检交给 helper。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["mkdir", "write", "run", "locate"],
                    "description": "Required action: mkdir, write, run, or locate. Missing action returns workspace_action_required without execution.\n\naction 必填；缺失时不会执行。",
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
            "locate already available files, or run narrow existing verifier/check commands for the artifact. "
            "Computation, new scripts, test development, builds, services, and broad extraction belong to code/read "
            "helpers.\n\n"
            "edit helper 专用产物写入/定位工具；可运行窄验收命令，但不做计算、构建或新脚本开发。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["mkdir", "write", "locate", "run"],
                    "description": "Allowed action: mkdir, write, locate, or run. Use run only for existing verifier/check commands that validate the current artifact.\n\n允许创建目录、写文本产物、定位文件或运行已有验收命令。",
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
                "command": {
                    "type": "string",
                    "description": "Existing verifier/check command for run. Do not use for new computation, build, services, or broad extraction.\n\n已有验收/检查命令。",
                },
                "timeout_sec": {
                    "type": "number",
                    "description": "Optional timeout in seconds for run.\n\n可选运行超时秒数。",
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
            "Search historical files and the shared file index by keyword. Prefer the visible Shared Files window first "
            "when the candidate is already shown there, but use this tool to locate older files, files outside that "
            "window, same-name uploads from different users, or historical generated artifacts. Returned entries include "
            "filename, uploader/time metadata, workspace path, and the `content` summary; fetch a ready file by its node ID "
            "before reading concrete file content.\n\n"
            "按关键词检索历史/共享文件；返回文件名、上传者/时间、路径和 content 摘要，需读取正文时再按节点 ID 提取。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search keyword matched against filenames, headlines, and indexed content summaries. Use concrete "
                        "filename words, uploader/topic words, or terms from the user reference.\n\n"
                        "搜索文件名、标题和已索引内容摘要。"
                    ),
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
            "Preflight a workspace file to identify type, direct-read safety, and available reader/conversion facts. "
            "Use it as the first step for unfamiliar files, especially Office, PDF, image, archive, media, or binary formats. "
            "Text files can usually go to read_file after inspection; for structured files, compare the returned workflow facts with the active task before choosing the next reader.\n\n"
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
            "In environment project mode, real project paths are read with env_read/env_search/env_list_tree/env_run; "
            "`_env/...` is only a fetched staged copy and may be absent until env_fetch succeeds. "
            "Use this for source, logs, markdown, JSON, CSV, XML, and other text files. Structured or binary formats return "
            "a typed refusal with file-category and recovery facts; compatibility fields such as suggested_tools describe candidate readers, not an automatic decision.\n"
            "\n"
            "## Reading Workflow\n"
            "- Plain text source and logs: read the whole file when size permits, or page with start_line/end_line.\n"
            "- Office files: inspect first, then use office(action='read'/'write'/'append'/'replace_block', ...) for body, tables, or edits.\n"
            "- PDFs: inspect first, then use the suggested text extraction or OCR path.\n"
            "- Images and visual files: inspect dimensions/format first, then use OCR or visual extraction.\n"
            "- After binary_or_structured_file_not_readable_as_text, compare suggested_tools/next_call_fact with the active task and choose the next reader from evidence.\n"
            "- When output is truncated, continue from next_start_line.\n"
            "\n"
            "Returns total_lines, shown_range, line-numbered content, and truncated status.\n\n"
            "read_file 只读工作区纯文本；项目路径用 env_*，`_env/...` 仅表示已 fetch 的暂存副本。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative text file path. In project mode, use env_read for project paths; use `_env/...` only after env_fetch.\n\n工作区相对文本路径；项目路径用 env_read，_env 需先 fetch。",
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


MAIN_READ_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Main-process text spot-check reader. It is designed for small files, narrow main-owned checks, or a narrow "
            "evidence location already identified by helper/search/code_index/inspect. Broad source reading, long logs, "
            "generated reports, source-material extraction, and full-file analysis are helper-owned in the normal workflow, "
            "with the main process synthesizing from concise evidence. Large unbounded reads return "
            "file-size and targeting facts instead of filling the main context.\n\n"
            "主进程只做小文件或定点核查；大文件和全量分析由 helper 读取后回报精简证据。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative text file path.\n\n工作区相对文本路径。",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Start line, 1-indexed, for a narrow evidence check after the location is known.\n\n已知位置后的窄范围核查起始行。",
                    "default": 1,
                },
                "end_line": {
                    "type": "integer",
                    "description": "Inclusive end line, 1-indexed, for the same narrow evidence check.\n\n窄范围核查的结束行。",
                    "default": -1,
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum returned characters. Main-process defaults and caps are intentionally small.\n\n返回字符上限；主进程默认和上限更小。",
                    "default": 24000,
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
            "- old_str is literal file text, not the JSON-escaped representation of a tool result; backslashes before quotes belong only when the file itself contains them.\n"
            "- On mismatch, the file is unchanged and the error explains how to widen context or set expected_count.\n"
            "- Use read_file first when uniqueness or surrounding structure is uncertain.\n"
            "- If repeated focused edits fail on the same logic area, step back: read the relevant file/function, rebuild the data flow, and use a broader helper-owned rewrite or replacement strategy.\n"
            "- Use new_str='' for deletion with a precise old_str.\n\n"
            "edit_file 用于精确文本替换；old_str 填文件原文而非 JSON 转义形式；先确认唯一上下文，失败不改文件。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path. In environment project helpers, edit the staged `_env/...` copy, not the bare project-relative path.\n\n工作区相对路径；项目 helper 编辑已暂存的 `_env/...` 副本。"},
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
                "path": {"type": "string", "description": "Workspace-relative file path. In environment project helpers, insert into the staged `_env/...` copy, not the bare project-relative path.\n\n工作区相对路径；项目 helper 使用已暂存的 `_env/...` 副本。"},
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
            "- 每个 edit 同 edit_file 规则:old_str ≥ 5 字符、配对校验、expected_count 默认 1；old_str 是文件原文，不是 JSON 转义文本。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative target for the atomic edit batch. Environment project helpers should use the fetched staged `_env/...` file for coordinated replacements, not a bare project-relative path.\n\n原子批量替换的目标文件；项目 helper 使用已暂存的 `_env/...` 副本，不用裸项目路径。"},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_str": {"type": "string", "description": "Exact literal file text to replace, same contract as edit_file; do not copy JSON escape backslashes unless they exist in the file.\n\n要替换的文件原文；不要复制 JSON 转义反斜杠。"},
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
            "Search inside workspace plain text files and return matching line numbers with previews. "
            "In environment project mode, search real project paths with env_search; search `_env/...` only when that staged copy already exists. "
            "Use this for source code, Markdown, TXT, CSV/TSV, JSON/YAML, and logs; it is the "
            "portable replacement for `findstr` or `grep` and supports streaming scans up to 50MB. "
            "For Office/PDF/image/binary containers, first use `inspect_file`; use `office(action='read')` "
            "for DOCX/PPTX/XLSX body content and OCR/extraction tools for PDF or images. If this tool "
            "returns `binary_or_structured_file_not_readable_as_text`, switch to the suggested structured "
            "reader instead of retrying the same search. Use search results to confirm existence, locate "
            "line ranges for `read_file`, and establish edit uniqueness.\n\n"
            "工作区文本搜索；项目路径用 env_search，_env 仅用于已存在暂存副本。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative text file path. In project mode, use env_search for project paths; use `_env/...` only after env_fetch.\n\n工作区文本路径；项目路径用 env_search，_env 需先 fetch。"},
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
            "Create a compact structural index for one workspace source file: functions, classes, structs, includes/imports, and line numbers. "
            "In environment project mode, use env_read/env_search for project paths or env_fetch before indexing `_env/...`. "
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
            "code_index 用于工作区源码鸟瞰；项目文件先 env_read/env_search 或 env_fetch。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative source file path. In project mode, use env_* for project paths or env_fetch first.\n\n工作区相对源码路径；项目路径先用 env_* 或 fetch。",
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
            "it records what has been verified, what is partial, and what is still blocked. This ledger is internal "
            "structured state; it is not a user-visible note file, memory file, or handoff artifact. When a user explicitly "
            "asks to store facts as memory, notes, or later handoff, record each compact fact here as evidence or a contract "
            "in addition to any requested or naturally needed visible note/handoff file; neither channel replaces the other."
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


TASK_PLAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "task_plan",
        "description": (
            "Maintain the active task plan snapshot for the main process. Use it after reading memory, files, "
            "toolchain cache, or agent_state when those facts clarify or change the active task beyond the current turn text. "
            "It updates the current thread plan and mirrors compact facts into the structured task ledger; it does "
            "not verify artifacts or replace final JSON. Updates retain prior acceptance/evidence facts for the "
            "same task unless the model states task-change evidence in the plan."
            "\n\n维护当前主线任务快照；读取记忆、文件或续作证据后可更新；同任务旧验收事实会保留。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "update"],
                    "description": "Use status to read the current snapshot; update to revise the active task plan.\n\n读取或更新当前任务快照。",
                },
                "goal": {
                    "type": "string",
                    "description": "Resolved active task goal. It may be based on current turn plus prior memory/tool evidence.\n\n解析后的当前主线任务目标。",
                },
                "key_points": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Compact facts, acceptance notes, or evidence points for the active task.\n\n当前任务事实或验收要点。",
                },
                "deliverables": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Expected current-task user-facing deliverables, if known. Omit uncertain historical files.\n\n预期本任务交付物。",
                },
                "acceptance": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Checkable completion criteria for the active task. Include revised criteria as facts; omitted prior criteria for the same task remain visible in agent_state for comparison.\n\n可检查验收标准；同任务未重复的旧验收仍保留供对照。",
                },
                "evidence_required": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Evidence needed before finalizing this active task. This records final evidence needs, not who must collect them. "
                        "For coding/debugging, code helpers can satisfy source reading, failure diagnosis, edits, and tests through input_files and acceptance_checks; the main thread can keep final diff/apply and acceptance evidence compact.\n\n"
                        "最终交付前所需证据；记录证据需求，不表示必须由主进程收集。"
                    ),
                },
                "risks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Known ambiguity or stale-context risks.\n\n已知歧义或旧上下文风险。",
                },
                "current_stage": {
                    "type": "string",
                    "description": "Current stage such as resolving_task, reading_memory, executing, verifying, or finalizing.\n\n当前阶段。",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief factual reason for the update, such as memory/file evidence that changed the active task.\n\n更新原因。",
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
            "Run and manage helper tasks in isolated workspaces. The main process owns goals, dependency order, monitoring, acceptance, and final synthesis; helpers do substantial bounded work.\n"
            "Pick `kind` by product, `mode` by difficulty. Split independent work into parallel tasks in one call; keep strict dependencies serial. Shared structure goes in `framework`; readable paths go in `input_files`. Same task_id with resume=true continues interrupted work; fork_from clones a finished workspace. The first framework helper owns only this structural contract (slots, ownership, acceptance, exact output matrix); evidence-backed analysis, research claims, citations, final numeric values, conclusions, implementation bodies, and final assembly belong to producer helpers. `_helpers_shared/...` files are handoff evidence, not user-facing artifacts; final deliverables are clean non-shared workspace files (project-visible targets use staged `_env/<project-relative-path>`).\n"
            "Run-state facts (heartbeats, outputs_check, file_map, main_available_files, quality_warnings, repair hints) arrive in tool results; decide wait/resume/collect/kill from those facts. Clean helper results are producer-owned quality evidence, so include all requested deliverables in expected_outputs and consume the compact helper report rather than rereading produced artifacts.\n"
            "\u6309\u4ea7\u7269\u9009 kind\u3001\u6309\u96be\u5ea6\u9009 mode\uff1b\u72ec\u7acb\u4efb\u52a1\u5408\u4e00\u6b21\u8c03\u7528\u5e76\u884c\u6d3e\u53d1\uff1b\u5171\u4eab\u6846\u67b6\u53ea\u653e\u7ed3\u6784\u548c\u9a8c\u6536\uff0c\u5b9e\u8d28\u5185\u5bb9\u7531\u5206\u7247\u4ea7\u7269\u627f\u8f7d\uff1b\u8fd0\u884c\u4e8b\u5b9e\u89c1\u5de5\u5177\u7ed3\u679c\u3002"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["spawn", "spawn_async", "poll", "collect", "wait_any", "kill", "status"],
                    "description": (
                        "spawn waits up to wait_window_sec; spawn_async returns immediately for overlapped coordination; "
                        "poll = non-blocking heartbeat; collect/wait_any wait on listed task_ids; kill = cooperative interrupt; "
                        "status = dashboard.\n\u5f02\u6b65\u6d41\u7a0b spawn_async \u540e poll/wait_any/collect\uff1b\u7b49\u5f85\u7528 wait_any \u6216 collect\u3002"
                    ),
                },
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Target task_ids for poll/collect/wait_any.",
                },
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "description": (
                        "Task list for spawn/spawn_async (parallel, 1-16). `wait_window_sec` is a top-level field, not a task field."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "Short semantic id; same id + resume=true continues prior workspace.",
                            },
                            "prompt": {
                                "type": "string",
                                "description": (
                                    "Focused request: goal, inputs, owned output paths, constraints, and 3-8 checkable acceptance points. "
                                    "Pass file bodies via input_files, not pasted text; keep history/persona/manuals out; do not preselect implementation routes. "
                                    "Structured source fields stay raw (values plus notes); ambiguous cost/unit/count semantics are for the helper to resolve from evidence.\n"
                                    "\u805a\u7126\u76ee\u6807\u4e0e\u9a8c\u6536\uff1b\u6b63\u6587\u8d70 input_files\uff1b\u6b67\u4e49\u5b57\u6bb5\u4fdd\u7559\u539f\u503c\u7531 helper \u5224\u65ad\u3002"
                                ),
                            },
                            "framework": {
                                "type": ["string", "object"],
                                "description": (
                                    "Shared structural contract for multi-part work: goal, interfaces/schema, outline, evidence map, ownership, validation checks, exact output matrix, merge order. Structure only; content belongs to producer helpers.\n\u5171\u4eab\u6846\u67b6\u53ea\u653e\u7ed3\u6784\u3001\u69fd\u4f4d\u3001\u5f52\u5c5e\u3001\u9a8c\u6536\u548c\u5408\u5e76\u987a\u5e8f\u3002"
                                ),
                            },
                            "dispatch_reason": {
                                "type": "string",
                                "description": "Facts justifying this boundary/kind/mode/split, mainly after a guard block. Not an override.",
                            },
                            "resume": {
                                "type": "boolean",
                                "description": "Keep prior workspace and continue the same task boundary. Check still_running first; interrupt before redirecting.",
                                "default": False,
                            },
                            "fork_from": {
                                "type": "string",
                                "description": "Clone a finished helper workspace as the starting point (implies resume).",
                            },
                            "kind": {
                                "type": "string",
                                # 真相源: app.llm.tools.delegate.VALID_HELPER_KINDS
                                "enum": ["code", "edit", "verify", "draw", "tts", "read", "project_map", "file_summary", "impact_review", "inventory"],
                                "default": "code",
                                "description": (
                                    "Product family: code=implementation/commands/benchmarks/data computation, including browser-automation evidence that requires running Playwright/Puppeteer/Selenium/Chromium-style commands; read=source-material reading/classification/triage/extraction (internal .txt evidence); "
                                    "edit=final document/text assembly (may read small explicit input_files); draw=charts/images from data; tts=speech/narration/persona voice/TTS-file artifacts; non-speech audio stays code; verify=read-only review; "
                                    "inventory/project_map/file_summary/impact_review=project analysis. Broad/visual/uncertain extraction goes read-first.\n"
                                    "\u6309\u4ea7\u7269\u548c\u80fd\u529b\u9009 kind\uff1b\u9700\u8fd0\u884c\u6d4f\u89c8\u5668\u81ea\u52a8\u5316\u547d\u4ee4\u7684\u8bc1\u636e\u7528 code\uff0c\u5e7f\u6cdb\u6750\u6599\u5148 read\u3002"
                                ),
                            },
                            "mode": {
                                "type": "string",
                                "enum": ["easy", "hard"],
                                "default": "easy",
                                "description": "easy=default; hard=stronger reasoning for a narrow difficult task or evidence-backed retry.",
                            },
                            "expected_outputs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Files the helper must deliver or may modify (ownership, copyback, outputs_check). Project-visible targets use full staged `_env/<path>`; read-helper evidence uses helper-local .txt names.\n"
                                    "\u9879\u76ee\u53ef\u89c1\u4ea7\u7269\u7528\u5b8c\u6574 `_env/...` \u8def\u5f84\uff0cread \u8bc1\u636e\u7528\u5185\u90e8 txt \u540d\u3002"
                                ),
                                "default": [],
                            },
                            "input_files": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Readable inputs: concrete files, staged paths, or project-relative paths (auto-staged at helper startup). Use instead of pasting bodies into prompt.\n"
                                    "\u53ef\u8bfb\u8f93\u5165\uff1b\u9879\u76ee\u76f8\u5bf9\u8def\u5f84\u4f1a\u81ea\u52a8\u6682\u5b58\u3002"
                                ),
                                "default": [],
                            },
                            "acceptance_checks": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Checks the helper runs or reports against: commands, coverage points, sections, evidence requirements.",
                                "default": [],
                            },
                        },
                        "required": ["task_id", "prompt"],
                    },
                },
                "task_id": {
                    "type": "string",
                    "description": "Target helper for action=kill (cooperative).",
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
                        "Required for kill; must match evidence. api_stall_emergency only for 60s+ without chunks; healthy iter/tool progress is not a stall."
                    ),
                },
                "force": {
                    "type": "boolean",
                    "description": "Deprecated; kills are always cooperative.",
                    "deprecated": True,
                },
                "wait_window_sec": {
                    "type": "number",
                    "default": 90,
                    "description": (
                        "Wait window for spawn/collect/wait_any (30-1800s; nonpositive waits for all). Wake up, read heartbeats, then wait/resume/interrupt/proceed."
                    ),
                },
                "min_results_to_return": {
                    "type": "integer",
                    "description": "Early fan-in: return once N helpers finish (1..len(tasks)-1).",
                    "minimum": 1,
                },
                "force_blanket_resume": {
                    "type": "boolean",
                    "description": "Confirm a >=3 all-alive blanket resume is intentional.",
                },
                "helper_think": {
                    "type": "boolean",
                    "description": "Enable reasoning for coding/verify helpers in this call (costly; for deep bugs only).",
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
            "Office document read/write/edit. Auto-detects DOCX/PPTX/XLSX behavior from the file extension. On ok=false, read the error and change the next call rather than retrying the same arguments.\n"
            "\n"
            "## Actions\n"
            "- read, extract_images, ocr_images: extract document text, structure, media metadata, image files, or embedded-image OCR. Page large DOCX bodies with start_block/end_block/max_blocks; save large OCR output with save_to and read it by ranges.\n"
            "- write, append: create or extend a document. DOCX uses blocks, PPTX uses slides, XLSX uses sheets.\n"
            "- replace_section, replace_block, replace_blocks, delete_block, insert_block: targeted DOCX edits. Use block indexes from office(action='read'). Targeted block/slide edits require an exact non-negative `block_index`/index; use replace_blocks for multiple known block edits.\n"
            "- fill_empty_headings: DOCX-only batch fill for existing empty heading paragraphs, optionally inserting body blocks after each heading.\n"
            "- replace_slide, insert_slide, delete_slide: targeted PPTX edits.\n"
            "- update_cells: targeted XLSX edits.\n"
            "- insert_image: append one image to DOCX.\n"
            "- verify_numbers, verify_rigor: DOCX/PPTX numeric and rigor checks against CSV/source data; pass `csv_paths` when checking data claims. DOCX structural acceptance uses `read`; `verify_integrity` is XLSX-only and is not a DOCX validity check.\n"
            "\n"
            "## Formats\n"
            "Valid DOCX block types are `heading`, `paragraph`, `list`, `table`, `image`, `equation`, and `page_break`; plain prose is `paragraph`, subtitles are also `paragraph`, bullets are `list`, and Table rows must contain at least one non-empty cell in a 2D array such as rows:[[\"A\",\"B\"],[\"C\",\"D\"]]. Image paths must already exist in the workspace. If document text references a figure/chart, embed the corresponding image block or markdown image and then verify structure/figure consistency.\n"
            "PPTX layouts include title, section, title_content, two_column, image, table, and blank. XLSX sheets use rows, optional header/freeze_header/column_widths, and formulas as strings such as '=SUM(A1:A10)'.\n"
            "\n"
            "## Large And Specialized Work\n"
            "Initial large write/append calls allow generous block/text limits; if the tool returns arg_size_warning, use the reported current limit for the next call. A successful DOCX read returns headings plus paragraph/block, table, and image counts; repeat reads are useful when an artifact changed or a named block range/detail is still unchecked. For formulas, data-rigor verification parameters, and detailed Office recipes, load `read_skill` with `name=\"office-recipes\"` when those details affect the next action. Office containers are not plain text; do not use search_in_file on DOCX/PPTX/XLSX paths.\n"
            "\n"
            "Office 工具处理 DOCX/PPTX/XLSX 容器；schema 保留动作、格式和验收边界，长文档分段、嵌图、公式和数据严谨性细节按需读取 `office-recipes`。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "read", "write", "append",
                        "replace_section", "replace_block",
                        "fill_empty_headings",
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
                    "description": "(.docx write/append/replace_section/replace_block/insert_block or fill_empty_headings.after_blocks) Structured content blocks. Use canonical DOCX block types: heading, paragraph, list, table, image, equation, page_break. Subtitles are paragraph blocks. Table rows are non-empty 2D arrays, not row objects. Initial DOCX write/append limits are generous (currently up to 96 blocks and 30,000 text characters before adaptive shrink); if an arg_size_warning appears, use the reported active limits. Batch coherent sections when JSON remains reliable, and split only when size, quoting, or verification facts make that safer.\n\n中文摘要：DOCX 内容块；subtitle 用 paragraph；表格 rows 用二维数组；初始上限较大，若出现 arg_size_warning 则按返回的当前限制调整。",
                    "items": {"type": "object"},
                },
                "headings": {
                    "type": "array",
                    "description": "(.docx fill_empty_headings) Heading entries to apply to existing empty DOCX heading paragraphs in document order: [{text, level?, after_blocks?}, ...]. The model decides titles and body content; the tool only writes them efficiently.\n\n中文摘要：按文档顺序批量填充空 heading，可在每个标题后插入正文块。",
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
                    "description": "(verify_numbers / verify_rigor) 核对文档内数字所依据的 CSV/源数据文件路径列表；DOCX 数据校验需要提供，结构验收用 read",
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
            "Write the current short task checklist. Use `todo_write` for planning state only: "
            "brief steps, ownership, and status. It is not a content-writing surface for long prose, "
            "Python scripts, document bodies, tables, or patches; use `workspace`, `office`, or edit tools "
            "for those artifacts. 中文概要：todo_write 只记录短计划状态，不承载长正文、脚本或文档内容。\n"
            "\n"
            "## Use When\n"
            "- The task has 3+ visible steps.\n"
            "- Debugging needs several hypotheses or files.\n"
            "- The user gave a checklist.\n"
            "\n"
            "## Do Not Use For\n"
            "- Single-step tasks or chat.\n"
            "- Drafting long document sections, code, scripts, tables, or final answers.\n"
            "\n"
            "## Rules\n"
            "- Keep each todo brief and concrete, normally 5-30 words.\n"
            "- status: `pending`(待办) / `in_progress`(正在做) / `completed`(完成)\n"
            "- At most one todo may be `in_progress`; even with parallel helpers, only the coordinating step is `in_progress`.\n"
            "- Update when a step completes, becomes blocked, or the plan materially changes.\n"
            "\n"
            "## Example\n"
            "```\n"
            "todo_write(todos=[\n"
            "  {\"id\": \"1\", \"content\": \"Read huffman.c and trace writes\", \"status\": \"in_progress\"},\n"
            "  {\"id\": \"2\", \"content\": \"Patch missing assignment\", \"status\": \"pending\"},\n"
            "  {\"id\": \"3\", \"content\": \"Run compile and focused tests\", \"status\": \"pending\"},\n"
            "])\n"
            "```\n"
            "Returns a visual checklist."
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
                                "description": "Brief concrete task description, not long prose/code/document content.",
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
            "tool failure facts, and relevant skills when they can resolve the uncertainty. A current request to save, "
            "create, edit, or jot down an artifact is already task authorization for that artifact; use the appropriate "
            "file/project tool unless content or destination facts are missing. A good question names the ambiguity, gives two to "
            "four executable options when useful, and explains the practical consequence briefly. After the call, the "
            "main process will provide the answer or best available decision in a later turn.\n\n"
            "用于缺少关键决策或外部信息时提问；用户已要求保存/创建/记录时即有该产物授权，缺内容或目标事实才问。"
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
            "检查工作区内文件的结构化元数据；用于缺失结构事实、矛盾、警告或用户明确要求的窄范围核查。\n"
            "\n"
            "## 何时用\n"
            "- helper 报告缺少必要结构事实，或存在矛盾、警告、用户显式核查要求\n"
            "- 你需要一个很窄的内部结构事实(段落数 / 表格数 / 图片数 / 幻灯片数 / 行数 / 尺寸 / 时长)\n"
            "- 用 read_file 看不到二进制内部,只能拿到 base64;用 inspect_file 能拿到结构元数据\n"
            "- 干净 helper 报告已经给出足够结构事实时，不要仅为复验而调用\n"
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
            "inspect_file 用于窄范围结构事实；warnings 表示产物结构异常，需修复、重派或说明缺口后再交付。"
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
            "Offline TTS speech generation for user-facing narration, spoken artifacts, or final voice files. "
            "For conceptual TTS questions, logs, scheduling, or general discussion of ordinary conversational voice replies, answer directly instead of generating audio.\n"
            "\n"
            "## Boundary\n"
            "- Voice-reply requests set the final reply form; either the normal final voice-output layer or a `kind=tts` helper/tool route may synthesize it when the active plan selects that route.\n"
            "- Speech, narration, persona voice, and TTS-file requests use this tool to generate a spoken artifact and return paths for deliverables.\n"
            "- Non-speech audio such as white noise, tones, beeps, music/signal synthesis, waveform processing, or audio analysis is code/signal work, not TTS.\n"
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
            "Generation returns path facts and candidate fields; the final JSON decides how to use them. "
            "If the generated speech file is this round's final voice reply, copy `voice_reply_file_candidate` into final JSON `voice_reply_file` and do not list it as a normal deliverable. "
            "If the audio is a file attachment or standalone artifact, copy `deliverable_candidate` into `plan.deliverables`.\n"
            "\n"
            "Use this tool for explicit speech/voice-file generation, long-text narration into a file, or spoken audio attachments for documents and reports.\n\n"
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
                    "description": "历史兼容字段；不改变合成过程。最终是否作为语音回复或普通附件由返回的候选字段和最终 ResponsePlan 决定。默认 false。",
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
            "- append: append a short section to a lightweight text file when main-process authorship is still the chosen route.\n"
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
                    "enum": ["mkdir", "write", "append", "run", "locate"],
                    "description": "Required action: mkdir, write/append lightweight docs, run file-management commands, or locate files.\n必填动作字段。",
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
