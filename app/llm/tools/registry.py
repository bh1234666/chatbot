"""Round-2 tool registry and compatibility dispatch layer.

This module owns the main-thread tool list, permission metadata, runtime-mode
tool selection, and legacy import paths for handlers that now live in focused
modules. Implementation-heavy work should stay in dedicated tool modules while
this file keeps registration order and compatibility stable.

本模块持有主线程工具列表、权限元数据、运行模式工具选择与遗留导入路径;重逻辑实现应留在各专职工具模块,本文件只负责维持注册顺序与兼容性稳定。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import time
from contextvars import ContextVar
from typing import Any, Callable, Awaitable

from app.config import settings  # 2026-05-15: 把 OCR/TTS 并发上限挪到 settings, 默认仍为 2
from app.llm.tools.gpu_resources import (
    _GPU_SEMAPHORE,
    _OCR_SEMAPHORE,
    _TTS_SEMAPHORE,
    _async_semaphore,
    run_gpu_ocr,
    run_gpu_tts,
)

# 2026-05-09 Patch 15: OCR/TTS 子进程内存沉重；多个并发会 OOM 服务器。用 asyncio.Semaphore 限并发。
# 阈值 2(默认):典型部署是单机服务,2 个并发已能覆盖"主线程 + 一个 helper"的常见情况;
# 同时给其他 helper 排队等待,不挤爆。
# 2026-05-15: 阈值改从 settings 读, 通过 OCR_CONCURRENCY / TTS_CONCURRENCY 环境变量调整。
_current_voice_instruct: ContextVar[str] = ContextVar("_current_voice_instruct", default="")
_current_voice_ref_audio: ContextVar[str] = ContextVar("_current_voice_ref_audio", default="")
_current_persona_for_tts: ContextVar[str] = ContextVar("_current_persona_for_tts", default="")
_current_user_message_for_tts: ContextVar[str] = ContextVar("_current_user_message_for_tts", default="")


def set_current_tts_guard_context(persona: str, user_message: str):
    tok_persona = _current_persona_for_tts.set((persona or "").strip()[:800])
    tok_user = _current_user_message_for_tts.set((user_message or "").strip()[:500])
    return tok_persona, tok_user


def reset_current_tts_guard_context(tokens):
    try:
        tok_persona, tok_user = tokens
        _current_persona_for_tts.reset(tok_persona)
        _current_user_message_for_tts.reset(tok_user)
    except Exception:
        pass


def _copy_ocr_raw_text_to_workspace(workspace_dir: str, raw_text_path: str) -> dict[str, Any]:
    """Expose OCR raw text through a workspace-relative file.

    OCR internals may keep unfolded raw text under `output/ocr_raw`. Helpers
    cannot read that absolute path from their sandbox, so the tool result should
    provide a normal workspace path that can be paged with `read_file`.

    OCR 原始长文本转存到工作区相对路径，避免 helper 读取沙箱外绝对路径失败。
    """
    raw_path = str(raw_text_path or "").strip()
    if not raw_path:
        return {}
    try:
        with open(raw_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as exc:
        return {"raw_text_workspace_error": f"{type(exc).__name__}: {exc}"}
    try:
        digest = re.sub(r"[^0-9a-f]", "", hashlib.sha1(raw_path.encode("utf-8", "ignore")).hexdigest())[:10]
        rel_path = f"ocr_raw/raw_{digest}.txt"
        save_path = ws_tool._safe_resolve(workspace_dir, rel_path)
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        with open(save_path, "w", encoding="utf-8") as f:
            f.write(text)
    except (OSError, ValueError) as exc:
        return {"raw_text_workspace_error": f"{type(exc).__name__}: {exc}"}
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    return {
        "raw_text_workspace_path": rel_path,
        "raw_text_workspace_lines": lines,
        "raw_text_workspace_bytes": len(text.encode("utf-8")),
        "raw_text_hint": (
            f"Unfolded OCR raw text was saved to {rel_path}. Use read_file with start_line/end_line "
            "to inspect it in chunks; do not use the internal absolute raw_text_path.\n"
            "OCR 原始展开文本已转存到工作区相对路径，应分段读取。"
        ),
    }


async def tts_persona_guard(text: str, *, purpose: str = "tts") -> tuple[bool, str]:
    persona = _current_persona_for_tts.get("")
    user_message = _current_user_message_for_tts.get("")
    if not (text or "").strip():
        return True, "empty tts text"
    try:
        from app.llm.client import chat_json
        from app.llm import aux_prompts as _aux
        msgs = [
            {"role": "system", "content": _aux.TTS_PERSONA_GUARD_SYSTEM},
            {"role": "user", "content": _aux.TTS_PERSONA_GUARD_USER_TEMPLATE.format(
                persona=persona or "(none)",
                user_message=user_message or "(empty)",
                purpose=purpose,
                text=(text or "")[:500],
            )},
        ]
        raw = await chat_json(
            msgs,
            lite=True,
            reasoning="disabled",
            metrics_tag="json.tts_persona_guard",
        )
        return bool(raw.get("allow", True)), str(raw.get("reason", ""))[:200]
    except Exception as e:
        return True, f"guard_error: {e}"


def set_current_voice_instruct(instruct: str):
    return _current_voice_instruct.set((instruct or "").strip())


def reset_current_voice_instruct(token):
    try:
        _current_voice_instruct.reset(token)
    except Exception:
        pass


def current_voice_instruct(default: str = "") -> str:
    return _current_voice_instruct.get("") or default


def set_current_voice_ref_audio(path: str):
    return _current_voice_ref_audio.set((path or "").strip())


def reset_current_voice_ref_audio(token):
    try:
        _current_voice_ref_audio.reset(token)
    except Exception:
        pass


def current_voice_ref_audio() -> str:
    path = _current_voice_ref_audio.get("")
    return path if path and os.path.isfile(path) else ""

from app.llm.tools.python_exec import run_python
from app.llm.tools import workspace as ws_tool
# 注意：app.llm.tools.delegate 不在顶层导入——它本身需要 import
# PYTHON_TOOL_SCHEMA / WORKSPACE_TOOL_SCHEMA（定义在本文件下方），
# 在顶层互引会形成 partial-init 循环（Python 3.12 严格报错）。
# 改为在 _handle_delegate 函数内部 lazy import。
from app.memory import warm as warm_mem
from app.memory import cold as cold_mem
from app.memory import kb as kb_mem
from app.memory import group_files as gf_mem
from app.core import debug
from app.core.permissions import check_tool_permission, sync_tool_permissions_from_meta
from app.llm.tools.result_budget import apply_result_budget
from app.llm.tools.tool_meta import ToolMeta, schemas_for_main_thread, tool_meta, validate_aliases


log = logging.getLogger(__name__)


# ── 拒绝主线程实现型别名 ────────────────────────────────────────
# 主线程只负责规划、检索和调度。模型若幻觉出常见实现工具名,不要透明改写成
# workspace.run/write,否则会绕过“实现交给 helper”的边界。
_BLOCKED_MAIN_THREAD_ALIASES = {
    "write": "workspace(action=write)",
    "write_file": "workspace(action=write)",
    "run": "workspace(action=run)",
    "run_command": "workspace(action=run)",
    "shell": "workspace(action=run)",
    "bash": "bash",
    "python": "python",
    "edit_file": "edit_file",
    "multi_edit": "multi_edit",
    "insert_in_file": "insert_in_file",
    "office": "office",
    "progress_note": "helper-only(progress_note)",
    "request_resource": "helper-only(request_resource)",
}

# helper 允许把常见幻觉工具名透明映射到真实工具,避免无意义 unknown tool 重试。
# 注意这里只做 helper 侧兼容,主线程仍必须严格卡住实现型别名边界。
_HELPER_TOOL_ALIASES: dict[str, tuple[str, dict]] = {
    "write": ("workspace", {"action": "write"}),
    "write_file": ("workspace", {"action": "write"}),
    "write_workspace_file": ("workspace", {"action": "write"}),
    "write_github_file": ("workspace", {"action": "write"}),
    "create_file": ("workspace", {"action": "write"}),
    "workspace.write": ("workspace", {"action": "write"}),
    "run": ("workspace", {"action": "run"}),
    "run_command": ("workspace", {"action": "run"}),
    "workspace.run": ("workspace", {"action": "run"}),
    "shell": ("workspace", {"action": "run"}),
    "mkdir": ("workspace", {"action": "mkdir"}),
    "make_dir": ("workspace", {"action": "mkdir"}),
    "workspace.mkdir": ("workspace", {"action": "mkdir"}),
}

# 只保留低风险目录维护别名；实现型别名在 dispatch 顶层直接拒绝。
_TOOL_ALIASES: dict[str, tuple[str, dict]] = {
    "mkdir":      ("workspace", {"action": "mkdir"}),
    "make_dir":   ("workspace", {"action": "mkdir"}),
}


def _main_thread_resource_delegate_required(name: str) -> str:
    helper_kind = "tts" if name == "tts" else "read"
    action_hint = (
        "delegate(tasks=[{'task_id':'tts_resource','kind':'tts','prompt':'生成语音并报告 wav 路径'}])"
        if helper_kind == "tts"
        else "delegate(tasks=[{'task_id':'read_resource','kind':'read','prompt':'读取文件内容并写入可分段读取的证据文档'}])"
    )
    return json.dumps(
        {
            "ok": False,
            "error": "main_thread_resource_must_delegate",
            "resource_kind": helper_kind,
            "suggested_helper_kind": helper_kind,
            "main_thread_action": action_hint,
            "hint": (
                f"The main process should delegate a kind='{helper_kind}' helper for {name} "
                "so the shared helper guards and scheduling are used.\n"
                "主进程需要通过对应 helper 使用该资源。"
            ),
        },
        ensure_ascii=False,
    )


# 工具 JSON Schema 定义已抽离到 tool_schemas.py(2026-05-20 重构);re-export 兼容。
# 注:BASH_TOOL_SCHEMA 仍留在本文件(它在模块级调用 ws_tool.has_unix_shell())。
from app.llm.tools.tool_schemas import (  # noqa: E402,F401
    PYTHON_TOOL_SCHEMA,
    EXPAND_WARM_SCHEMA,
    EXPAND_COLD_SCHEMA,
    EXPAND_KB_SCHEMA,
    MARK_AVOID_SCHEMA,
    WORKSPACE_TOOL_SCHEMA,
    SEARCH_FILES_SCHEMA,
    FETCH_GROUP_FILE_SCHEMA,
    INSPECT_FILE_SCHEMA,
    READ_FILE_SCHEMA,
    EDIT_FILE_SCHEMA,
    INSERT_IN_FILE_SCHEMA,
    MULTI_EDIT_SCHEMA,
    SEARCH_IN_FILE_SCHEMA,
    CODE_INDEX_SCHEMA,
    READ_FUNCTION_SCHEMA,
    SEARCH_ACROSS_FILES_SCHEMA,
    AGENT_STATE_SCHEMA,
    TASK_PLAN_SCHEMA,
    DELEGATE_TOOL_SCHEMA,
    OFFICE_TOOL_SCHEMA,
    TODO_WRITE_SCHEMA,
    TODO_READ_SCHEMA,
    COMMIT_TO_MAIN_SCHEMA,
    FETCH_TO_TEMP_SCHEMA,
    RECALL_THREAD_SCHEMA,
    CONTINUE_TOOLCHAIN_SCHEMA,
    PROGRESS_NOTE_SCHEMA,
    REQUEST_RESOURCE_SCHEMA,
    ASK_USER_QUESTION_SCHEMA,
    INSPECT_FILE_TOOL_SCHEMA,
    OCR_TOOL_SCHEMA,
    TTS_TOOL_SCHEMA,
    MAIN_WORKSPACE_TOOL_SCHEMA,
)














# ── 局部读写工具(替代全文 read/write 的低效模式)──
# 每个工具独立挂载而不是塞到 workspace 子参数,因为子参数容易被模型忽略。


























# ─── 2026-05-02 part21:Bash 工具(参考 Claude Code 设计) ─────
# Bash 是 workspace.run 的简化别名,但 description 引导模型用 unix
# pipe/grep/sed/find/awk 直接组合,而不是被 search_in_file/code_index 等
# 受限工具切碎认知。
#
# 行为:dispatcher 直接路由到 _handle_workspace(action="run"),无需模型
# 自己组装 {action: "run", command: "...", timeout_sec: ...} 这种结构。
BASH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "Run shell commands for operations that genuinely need a shell: compilation, tests, git, pipelines, "
            "redirection, and platform checks. Use dedicated tools for file reading, searching, editing, and lightweight "
            "workspace writes when those tools fit the task.\n"
            "\n"
            "## Platform\n"
            + (
                # Linux/macOS 或 Windows + 检测到 git-bash:真 bash,unix 语法直接用
                "Native bash or compatible Unix shell is available. grep/sed/find/awk/head/tail/wc/xargs are available; "
                "Unix forms such as `2>/dev/null`, `| head -N`, `find -name`, and `grep -rn` can be used directly.\n"
                if ws_tool.has_unix_shell() else
                # Windows 没装 git-bash:cmd.exe,unix 语法不行,要 cmd 写法
                "Windows cmd.exe is the active shell because no git-bash was detected. Use Windows command forms: "
                "`findstr` instead of grep, `dir` instead of find, `2>nul` instead of `2>/dev/null`, and avoid "
                "`/dev/null` paths because cmd treats them as literal filenames.\n"
            )
            + "\n"
            "## Typical Shell Uses\n"
            + (
                # 真 bash 速查
                "- Cross-file search: `grep -rn 'codes\\[i\\]\\.code' . --include='*.c'` when search_across_files is not enough.\n"
                "- Large-file slice: `sed -n '440,460p' huffman.c`.\n"
                "- File discovery: `find . -name '*.c' -size +1k`.\n"
                "- Counts: `wc -l *.c` or `grep -c TODO *.c`.\n"
                "- Compile and run: `gcc -O2 -Wall *.c -o test && ./test`.\n"
                "- Filter large output: `./bench | grep -E 'time|ratio' | head -20`.\n"
                "- Batch compile: `for f in *.c; do gcc -c \"$f\" -o \"${f%.c}.o\"; done`.\n"
                if ws_tool.has_unix_shell() else
                # cmd 速查
                "- Cross-file search: `findstr /S /N /C:\"codes[i].code\" *.c`.\n"
                "- Large-file slices are better read with read_file ranges.\n"
                "- File discovery: `dir /s /b *.c`.\n"
                "- Line counts: `find /v /c \"\" *.c`.\n"
                "- Compile and run: `gcc -O2 -Wall *.c -o test.exe && test.exe`.\n"
                "- Filter large output: `bench.exe > out.txt 2>nul`, then `findstr /R \"time ratio\" out.txt`.\n"
                "- Suppress stderr with `xxx 2>nul`.\n"
                "- Search TODO lines: `findstr /S /N TODO *.c`.\n"
            )
            + "\n"
            "## Timeout And Waiting\n"
            "- Split very fast and very slow commands into separate batches so one long command does not delay unrelated quick evidence.\n"
            "- Timeout reference: search/list/read 5-10s, compile 15-60s, tests 30-120s, benchmark 120-1800s, ML training 600-3600s.\n"
            "- Choose timeout_sec for the expected natural runtime. Long-running validation should receive a long enough timeout.\n"
            "- For asynchronous or background work, rely on process notifications or a proper timeout instead of sleep polling.\n"
            "- When a command fails, diagnose the observed output and adjust the method; repeated sleep loops rarely add evidence.\n"
            "\n"
            "## Output Size\n"
            "stdout is truncated at 64KB. For large output, redirect to a file and then search the result:\n"
            + (
                "  `./bench > out.txt 2>&1` 然后 `grep RESULT out.txt | head -20`"
                if ws_tool.has_unix_shell() else
                "  `bench.exe > out.txt 2>&1` 然后 `findstr RESULT out.txt`"
            )
            + "\n\n"
            "shell 工具用于真正需要命令行的编译、测试、git、管道和重定向；按当前平台选择语法，合理设置超时，大输出先写文件再筛选。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run. Pipelines and command chaining are allowed when supported by the active shell.\n要执行的 shell 命令。",
                },
                "description": {
                    "type": "string",
                    "description": (
                        "One active-voice sentence describing what the command does. Prefer concrete operation labels such as "
                        "`List current files`, `Check git status`, `Find and delete .tmp files`, or `Compile all C files and run tests`. "
                        "Describe the action, not a vague risk category.\n"
                        "用一句主动语态说明命令做什么，描述具体动作而不是模糊类别。"
                    ),
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": (
                        "Timeout in seconds, default 90 and maximum 7200. Choose a value that matches the expected natural runtime: "
                        "search/list 5-10s, compile 30-90s, tests 60-180s, benchmark/performance 300-1800s, large data generation 600-3600s. "
                        "A timeout is first evidence about runtime budget; rerun with a larger timeout when the command plausibly needs it, "
                        "then investigate code or method problems if longer timeouts still fail.\n"
                        "命令超时秒数；先按任务类型给足时间，反复足量超时后再判断代码或方法问题。"
                    ),
                    "default": 90,
                },
            },
            "required": ["command"],
            "additionalProperties": False,  # strict: 拒绝幻觉参数
        },
    },
}

















MAIN_THREAD_TOOL_METAS: list[ToolMeta] = [
    tool_meta(MAIN_WORKSPACE_TOOL_SCHEMA, read_only=False, side_effect="workspace", requires_permission="generate_file"),
    tool_meta(COMMIT_TO_MAIN_SCHEMA, read_only=False, side_effect="workspace", requires_permission="generate_file"),
    tool_meta(TODO_WRITE_SCHEMA, read_only=False, side_effect="workspace", requires_permission="chat"),
    tool_meta(TODO_READ_SCHEMA, read_only=True, side_effect="none", requires_permission="chat"),
    tool_meta(RECALL_THREAD_SCHEMA, read_only=False, side_effect="workspace", requires_permission="chat"),
    tool_meta(READ_FILE_SCHEMA, read_only=True, side_effect="none", requires_permission="chat"),
    tool_meta(SEARCH_IN_FILE_SCHEMA, read_only=True, side_effect="none", requires_permission="chat"),
    tool_meta(CODE_INDEX_SCHEMA, read_only=True, side_effect="none", requires_permission="chat"),
    tool_meta(READ_FUNCTION_SCHEMA, read_only=True, side_effect="none", requires_permission="chat"),
    tool_meta(SEARCH_ACROSS_FILES_SCHEMA, read_only=True, side_effect="none", requires_permission="chat"),
    tool_meta(TASK_PLAN_SCHEMA, read_only=False, side_effect="memory", requires_permission="chat"),
    tool_meta(AGENT_STATE_SCHEMA, read_only=False, side_effect="memory", requires_permission="chat"),
    tool_meta(EXPAND_WARM_SCHEMA, read_only=True, side_effect="none", requires_permission="retrieve_memory"),
    tool_meta(EXPAND_COLD_SCHEMA, read_only=True, side_effect="none", requires_permission="retrieve_memory"),
    tool_meta(EXPAND_KB_SCHEMA, read_only=True, side_effect="none", requires_permission="retrieve_memory"),
    tool_meta(MARK_AVOID_SCHEMA, read_only=False, side_effect="memory", requires_permission="retrieve_memory"),
    tool_meta(SEARCH_FILES_SCHEMA, read_only=True, side_effect="none", requires_permission="read_group_file"),
    tool_meta(FETCH_GROUP_FILE_SCHEMA, read_only=False, side_effect="workspace", requires_permission="read_group_file"),
    tool_meta(FETCH_TO_TEMP_SCHEMA, read_only=False, side_effect="workspace", requires_permission="generate_file"),
    tool_meta(CONTINUE_TOOLCHAIN_SCHEMA, read_only=False, side_effect="memory", requires_permission="chat"),
    tool_meta(DELEGATE_TOOL_SCHEMA, read_only=False, side_effect="workspace", requires_permission="generate_file"),
    tool_meta(INSPECT_FILE_TOOL_SCHEMA, read_only=True, side_effect="none", requires_permission="chat"),
    tool_meta(OCR_TOOL_SCHEMA, read_only=False, side_effect="external", requires_permission="chat", main_thread_allowed=False),
    tool_meta(TTS_TOOL_SCHEMA, read_only=False, side_effect="external", requires_permission="chat", main_thread_allowed=False),
    tool_meta(REQUEST_RESOURCE_SCHEMA, read_only=False, side_effect="workspace", requires_permission="chat", main_thread_allowed=False),
    tool_meta(ASK_USER_QUESTION_SCHEMA, read_only=False, side_effect="external", requires_permission="chat"),
]

MAIN_THREAD_TOOLS = schemas_for_main_thread(MAIN_THREAD_TOOL_METAS)

ROUND2_TOOLS = MAIN_THREAD_TOOLS
sync_tool_permissions_from_meta(MAIN_THREAD_TOOL_METAS)
validate_aliases(_TOOL_ALIASES, _BLOCKED_MAIN_THREAD_ALIASES, MAIN_THREAD_TOOL_METAS)


# 主线程额外能用 processes 工具查看/终止当前 trace 的 helper/subprocess。
# 它只操作内部 ProcessRegistry，不执行任意命令，权限与 delegate/workspace 一致。
# 用 import 延迟加载防循环依赖。
def _add_process_tools_to_round2():
    """延迟注入 processes schema 到 ROUND2_TOOLS,避免循环 import。"""
    try:
        from app.llm.tools.tool_processes import PROCESSES_TOOL_SCHEMA
        if not any(meta.name == "processes" for meta in MAIN_THREAD_TOOL_METAS):
            MAIN_THREAD_TOOL_METAS.append(
                tool_meta(
                    PROCESSES_TOOL_SCHEMA,
                    read_only=False,
                    side_effect="external",
                    requires_permission="generate_file",
                )
            )
            sync_tool_permissions_from_meta(MAIN_THREAD_TOOL_METAS)
        if PROCESSES_TOOL_SCHEMA not in ROUND2_TOOLS:
            ROUND2_TOOLS.append(PROCESSES_TOOL_SCHEMA)
    except ImportError:
        pass


# ── 2026-05-02 part17:模块加载时自检工具名重复 ──
# 教训(trace d30b0823):READ_FUNCTION_SCHEMA / SEARCH_ACROSS_FILES_SCHEMA 误注册两次
# → OpenAI/DeepSeek 拒绝(同名工具)→ round2 在 0.468s 内崩 → fallback plan 装作回应。
# 损失极大但 import 时本可一行检测出来。
def _validate_round2_tools_unique() -> None:
    """检查 ROUND2_TOOLS 内工具 function name 唯一,有重复则 RuntimeError 在启动时崩。
    
    理由:重复 schema 是 import-time 错误,**不应**让生产系统跑起来再被 LLM API 拒绝。
    崩在 import 阶段比崩在 round2 强 10 倍 — 至少运维一眼就看到。
    """
    seen: dict[str, int] = {}
    for i, t in enumerate(ROUND2_TOOLS):
        try:
            name = t["function"]["name"]
        except (KeyError, TypeError):
            raise RuntimeError(
                f"ROUND2_TOOLS[{i}] 结构不合法:不是有效的 OpenAI tool schema"
            )
        if name in seen:
            raise RuntimeError(
                f"ROUND2_TOOLS 含重复工具 name='{name}' "
                f"(第 {seen[name]} 个 + 第 {i} 个),"
                f"OpenAI/DeepSeek API 会拒绝此工具列表。请在 ROUND2_TOOLS 里删除重复项。"
            )
        seen[name] = i


_validate_round2_tools_unique()


_add_process_tools_to_round2()


_ENVIRONMENT_TOOLS_CACHE: list[dict] | None = None
_ENVIRONMENT_TOOLS_SIGNATURE: tuple[str, ...] | None = None


def tools_for_runtime_mode(mode: str | None = None) -> list[dict]:
    """Return main-thread tool schemas for the active runtime mode.

    Chat mode deliberately returns the original ROUND2_TOOLS object so old callers
    keep identical names, order, and object identity. Environment mode reuses a
    stable additive list so prompt-cache tool schema shape stays consistent.
    """
    if mode is None:
        try:
            from app.core.runtime_mode import current_runtime_mode
            mode = current_runtime_mode()
        except Exception:
            mode = "chat"
    if (mode or "").strip().lower() != "environment":
        return ROUND2_TOOLS
    return _environment_tools_cached()


DELEGATE_INVENTORY_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate_inventory",
        "description": (
            "Environment-only shortcut for spawning one project inventory helper. Use it for unfamiliar or broad project context when a compact first-pass map of directories, file types, README/entry/config/test hints, exact lightweight statistics, and unread source-material notes would keep the main process focused. For deeper architecture use delegate with project_map; for selected files use file_summary; for change risk use impact_review.\n\n"
            "项目模式专用工程摸底 helper；用于首次了解目录、文件类型、入口、配置、测试和轻量统计。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Stable task id for the inventory helper, using a semantic name such as project_inventory.\n稳定的 inventory helper 任务 ID。",
                },
                "prompt": {
                    "type": "string",
                    "description": "What project orientation to produce and any focus areas. Keep dynamic task details here.\n项目摸底目标和重点区域。",
                },
                "input_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional staged project files or inventory manifests to inspect first.\n可选的已暂存项目文件或清单。",
                },
                "expected_outputs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional internal inventory report files expected from the helper.\n可选内部摸底报告产物。",
                },
                "acceptance_checks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional checks for inventory coverage or statistics.\n可选覆盖率或统计验收点。",
                },
            },
            "required": ["prompt"],
        },
    },
}


def _tool_name_signature(tools: list[dict]) -> tuple[str, ...]:
    """Return the stable tool-name order used to invalidate cached tool lists."""
    names: list[str] = []
    for tool in tools:
        function = tool.get("function", {}) if isinstance(tool, dict) else {}
        names.append(str(function.get("name", "")))
    return tuple(names)


def _environment_tools_cached() -> list[dict]:
    """Return the additive environment tool list with stable identity.

    The schemas themselves remain the source of truth. The cache only avoids
    rebuilding the same list object for every environment-mode request, which
    protects prefix-cache diagnostics and provider-side schema reuse.

    仅缓存工具列表对象；工具内容和顺序仍由注册表决定，用于稳定缓存前缀。
    """
    global _ENVIRONMENT_TOOLS_CACHE, _ENVIRONMENT_TOOLS_SIGNATURE
    from app.llm.tools.environment import ENVIRONMENT_TOOL_SCHEMAS

    tools = [*ROUND2_TOOLS, *ENVIRONMENT_TOOL_SCHEMAS, DELEGATE_INVENTORY_TOOL_SCHEMA]
    signature = _tool_name_signature(tools)
    if _ENVIRONMENT_TOOLS_CACHE is None or _ENVIRONMENT_TOOLS_SIGNATURE != signature:
        _ENVIRONMENT_TOOLS_CACHE = tools
        _ENVIRONMENT_TOOLS_SIGNATURE = signature
    return _ENVIRONMENT_TOOLS_CACHE


def tools_for_current_runtime() -> list[dict]:
    try:
        from app.core.runtime_mode import current_runtime_mode
        return tools_for_runtime_mode(current_runtime_mode())
    except Exception:
        return ROUND2_TOOLS


# ── 分发器 ──────────────────────────────────────────────────
async def dispatch(
    name: str,
    args: dict,
    *,
    archive_id: str,
    group_id: str,
    user_id: str,
    workspace_dir: str = "",
    permission_level: str | None = None,
    caller: str = "main",
) -> str:
    """
    执行工具并返回结果（字符串，作为 tool message content）。
    工具不暴露 archive_id/group_id/user_id/workspace_dir 给模型（args 不含），
    服务端从上下文注入，避免越权。

    2026-05-02 加: 工具别名兜底。模型(尤其 pro helper)即使在 prompt 明文警告下仍会
    hallucinate 出工具名 'write' / 'run' 等(这些名字在 OpenAI 训练数据里太常见)。
    在 dispatch 顶层做别名重写,把这类调用透明转到正确的工具上,避免每次都走 unknown
    tool 错误循环浪费 token。同时记录到 debug 让用户能 grep 出来观测频率。
    """
    # ── 别名兜底(Bug F): 模型 hallucinate 出来的常见错误工具名 ──
    aliased_from = None
    caller_kind = str(caller or "main").strip().lower()
    if caller_kind == "main" and name in {"ocr", "tts"}:
        debug.log(
            "tool.resource.delegate_required",
            f"{name} rejected in main thread; use resource helper",
            {"tool": name, "caller": caller_kind, "args": args},
        )
        return _main_thread_resource_delegate_required(name)

    try:
        from app.llm.tools.environment import environment_tool_names, handle_environment_tool
        if name in environment_tool_names():
            from app.core.runtime_mode import is_environment_mode
            if not is_environment_mode():
                return json.dumps(
                    {"ok": False, "error": f"tool '{name}' is only available in environment mode"},
                    ensure_ascii=False,
                )
            return await handle_environment_tool(name, workspace_dir, args or {})
    except Exception as e:
        if str(name).startswith("env_"):
            return json.dumps(
                {"ok": False, "error": f"environment tool dispatch failed: {type(e).__name__}: {e}"},
                ensure_ascii=False,
            )

    if name == "delegate_inventory":
        from app.core.runtime_mode import is_environment_mode
        if not is_environment_mode():
            return json.dumps(
                {"ok": False, "error": "tool 'delegate_inventory' is only available in environment mode"},
                ensure_ascii=False,
            )
        inv_args = args or {}
        task = {
            "task_id": str(inv_args.get("task_id") or "project_inventory").strip() or "project_inventory",
            "kind": "inventory",
            "mode": "easy",
            "prompt": str(inv_args.get("prompt") or "").strip(),
        }
        for key in ("input_files", "expected_outputs", "acceptance_checks"):
            if inv_args.get(key):
                task[key] = inv_args.get(key)
        if not task["prompt"]:
            return json.dumps(
                {"ok": False, "error": "delegate_inventory requires prompt"},
                ensure_ascii=False,
            )
        name = "delegate"
        args = {"tasks": [task], "auto_final": False}

    if caller_kind == "main" and name in _BLOCKED_MAIN_THREAD_ALIASES:
        target = _BLOCKED_MAIN_THREAD_ALIASES[name]
        debug.log(
            "tool.alias.blocked",
            f"{name} rejected instead of routing to {target}",
            {"original_name": name, "original_args": args, "target": target, "caller": caller_kind},
        )
        return json.dumps(
            {
                "ok": False,
                "error": (
                    f"tool '{name}' is not available in the main thread. "
                    "Use delegate for implementation work; the main thread should only plan, "
                    "inspect, retrieve, and coordinate. "
                    "Choose one documented main-thread tool or delegate once instead of retrying blocked aliases.\n\n"
                    "主进程只能编排、检查、检索和协调；实现类工作请派发 helper。"
                ),
                "hint": (
                    "Use delegate for implementation, or call only the tool names present in the current schema.\n\n"
                    "实现工作用 delegate；否则只调用当前 schema 中存在的工具。"
                ),
            },
            ensure_ascii=False,
        )

    if name in _TOOL_ALIASES or (caller_kind == "helper" and name in _HELPER_TOOL_ALIASES):
        alias_map = _HELPER_TOOL_ALIASES if (caller_kind == "helper" and name in _HELPER_TOOL_ALIASES) else _TOOL_ALIASES
        real_name, extra_args = alias_map[name]
        merged = {**extra_args, **(args or {})}  # 用户传的 args 优先,extra 兜底
        debug.log(
            "tool.alias",
            f"{name} → {real_name} (hallucinated tool name re-routed)",
            {"original_name": name, "original_args": args, "merged_args": merged, "caller": caller_kind},
        )
        aliased_from = name
        name = real_name
        args = merged

    allowed, required_perm, granted_perm = check_tool_permission(name, permission_level)
    if not allowed:
        debug.log(
            "tool.permission.denied",
            f"{name} requires {required_perm.name}, granted {granted_perm.name}",
            {"tool": name, "required": required_perm.name, "granted": granted_perm.name},
        )
        return json.dumps(
            {
                "ok": False,
                "error": f"permission denied for tool '{name}'",
                "required_permission": required_perm.name,
                "granted_permission": granted_perm.name,
            },
            ensure_ascii=False,
        )

    debug.log(f"tool.{name}.input", _tool_in_summary(name, args), args)
    try:
        if name == "python":
            result = await _handle_python(args)
        elif name == "workspace":
            result = await _handle_workspace(workspace_dir, args)
        elif name == "bash":
            # 2026-05-02 part21:bash 是 workspace.run 的简化别名
            result = await _handle_bash(workspace_dir, args)
        elif name == "commit_to_main":
            # 2026-05-03 Bug E:把 .temp 的文件提升到主区
            result = await _handle_commit_to_main(workspace_dir, args)
        elif name == "fetch_to_temp":
            # v2 三层隔离:从永久区或 .prev/ 复制文件到 .temp/
            result = await _handle_fetch_to_temp(workspace_dir, args)
        elif name == "recall_thread":
            # 2026-05-03:防上下文淹没 — 主线程的"checkpoint"
            result = await _handle_recall_thread(workspace_dir, args)
        elif name == "continue_toolchain":
            result = await _handle_continue_toolchain(archive_id, group_id, user_id, args)
        elif name == "progress_note":
            # 2026-05-03:helper 写中间状态(主线程通过 processes peek 看)
            result = await _handle_progress_note(workspace_dir, args)
        elif name == "request_resource":
            result = await _handle_request_resource(workspace_dir, args)
        elif name == "todo_write":
            # 2026-05-02 part21:任务规划外化(参考 Claude Code TodoWrite)
            result = await _handle_todo_write(workspace_dir, args)
        elif name == "todo_read":
            result = await _handle_todo_read(workspace_dir, args)
        elif name == "expand_warm":
            result = await _handle_expand_warm(archive_id, args)
        elif name == "expand_cold":
            result = await _handle_expand_cold(archive_id, user_id, args)
        elif name == "expand_kb":
            result = await _handle_expand_kb(archive_id, user_id, args)
        elif name == "mark_avoid_mention":
            result = await _handle_mark_avoid(archive_id, group_id, user_id, args)
        elif name == "search_files":
            result = await _handle_search_files(archive_id, group_id, workspace_dir, args)
        elif name in {"fetch_indexed_file", "fetch_group_file"}:
            result = await _handle_fetch_group_file(archive_id, group_id, workspace_dir, args)
        elif name == "read_file":
            result = await _handle_read_file(workspace_dir, args)
        elif name == "edit_file":
            result = await _handle_edit_file(workspace_dir, args)
        elif name == "multi_edit":
            result = await _handle_multi_edit(workspace_dir, args)
        elif name == "insert_in_file":
            result = await _handle_insert_in_file(workspace_dir, args)
        elif name == "search_in_file":
            result = await _handle_search_in_file(workspace_dir, args)
        elif name == "code_index":
            result = await _handle_code_index(workspace_dir, args)
        elif name == "read_function":
            result = await _handle_read_function(workspace_dir, args)
        elif name == "search_across_files":
            result = await _handle_search_across_files(workspace_dir, args)
        elif name == "task_plan":
            result = await _handle_task_plan(args)
        elif name == "agent_state":
            result = await _handle_agent_state(args)
        elif name == "delegate":
            result = await _handle_delegate(archive_id, group_id, user_id, workspace_dir, args)
        elif name == "office":
            from app.llm.tools.office import handle_office
            result = await handle_office(workspace_dir, args)
        elif name == "processes":
            from app.llm.tools.tool_processes import handle_processes
            result_dict = await handle_processes(args)
            result = json.dumps(result_dict, ensure_ascii=False)
        elif name == "spawn_helper":
            from app.llm.tools.delegate import handle_spawn_helper
            result = await handle_spawn_helper(
                args,
                archive_id=archive_id, group_id=group_id, user_id=user_id,
                helper_workspace=workspace_dir,
            )
        elif name == "wait_helper":
            from app.llm.tools.delegate import handle_wait_helper
            result = await handle_wait_helper(args)
        elif name == "ask_user_question":
            # 2026-05-04 Claude Code 移植:helper→主线程提问
            result = await _handle_ask_user_question(
                args, archive_id=archive_id, group_id=group_id, user_id=user_id,
            )
        elif name == "read_skill":
            # 2026-05-11 P1.1 Skills 系统轻量版: helper 按需加载详细指引
            # 参照 Claude Code bundledSkills.ts — system prompt 极简,详细教学按需 read
            from app.llm.tools.delegate import get_skill, list_skills
            _skill_name = str(args.get("name", "")).strip()
            if not _skill_name:
                result = json.dumps({
                    "ok": False, "error": "missing 'name' parameter",
                    "available_skills": list_skills(),
                }, ensure_ascii=False)
            else:
                _content = get_skill(_skill_name)
                if _content is None:
                    result = json.dumps({
                        "ok": False,
                        "error": f"unknown skill '{_skill_name}'",
                        "available_skills": list_skills(),
                        "hint": "Skill name must exactly match one of available_skills.\nskill 名称必须匹配 available_skills。",
                    }, ensure_ascii=False)
                else:
                    result = json.dumps({
                        "ok": True, "name": _skill_name,
                        "content": _content,
                    }, ensure_ascii=False)
        elif name == "ocr":
            result = await _handle_ocr(workspace_dir, args)
        elif name == "tts":
            result = await _handle_tts(workspace_dir, args, archive_id=archive_id)
        elif name == "inspect_file":
            # 2026-05-09 Patch 22: 主线程验证二进制产物
            result = await _handle_inspect_file(workspace_dir, args)
        else:
            result = json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)
        # ── Bug #29: 别名教育 — 注入 _alias_note 告知 LLM 正确的工具名 ──
        # 工具已通过别名重路由成功执行,但 LLM 不知道它用了错误的名字。
        # 在 result JSON 里加一条 _alias_note,教育 LLM 下次用正确的工具名。
        if aliased_from:
            try:
                _robj = json.loads(result)
                if isinstance(_robj, dict):
                    _robj["_alias_note"] = (
                        f"你调用了 '{aliased_from}',已被透明重路由到 '{name}'。"
                        f"下次请直接用 '{name}' 工具。"
                    )
                    result = json.dumps(_robj, ensure_ascii=False)
            except Exception:
                pass  # 非 JSON 或解析失败,静默跳过

        result = apply_result_budget(name, result)
        debug.log(f"tool.{name}.output", _tool_out_summary(name, result), _try_parse(result))
        return result
    except Exception as e:
        log.exception("tool dispatch failed: %s", name)
        debug.log(f"tool.{name}.error", f"{type(e).__name__}: {e}")
        return json.dumps(
            {"error": f"tool execution error: {type(e).__name__}: {e}"},
            ensure_ascii=False,
        )


# ─── 2026-05-02 part21:Bash / TodoWrite / TodoRead handlers ─────
async def _handle_bash(workspace_dir: str, args: dict) -> str:
    """bash 工具 — Claude Code 风格 shell 入口。

    2026-05-03 重写(Bug 1 修):不再走 _handle_workspace(action=run),直接调
    workspace.handle_run 并传 prefer_unix_shell=True。Windows 上若装了 git-bash
    会用真 bash.exe 跑(unix 语法直接生效);否则 fallback cmd 并在错误结果里加
    hint 让模型自切 cmd 写法。

    历史教训(trace 7e6629f228c84e78 sbt helper 02:06:55-02:07:00):
    schema/system prompt 都告诉模型"git-bash/MSYS2 可用",但实际走的是
    `cmd /c <cmd>` → unix 语法全部失败 → 同种错误 3 次 → stuck detector 32s 杀
    helper。修后 unix 命令真正可用,prompt 与实现一致。

    引导模型用 unix 工具组合(grep/sed/find/awk/pipe)替代受限的
    search_in_file/search_across_files/code_index 等专用工具。

    2026-05-15 P112: 加 bash 读类命令重复检测
    病因(实测压缩论文 trace): 329 bash 调用中只 198 唯一, 重复 131 次。
    包括 7× `type results.csv` (应该用 read_file), 6× `dir` (应该用 workspace.list),
    24× 同一 gcc 命令 (改 source 后重编合理, 但部分重复)。
    修法: 检测 type/cat/more/dir/ls/grep 等"读类"命令, 2 次重复后引导用专用工具。
    """
    args = args or {}
    command = str(args.get("command", "")).strip()
    if not command:
        return json.dumps({
            "ok": False,
            "error": "bash requires `command` parameter (the shell command to run)",
        })
    # 2026-05-11 B1 改默认值: 60 → 90s。原因:
    # log 实测 helper 默认传 timeout_sec=10 / 15 居多,benchmark 类全部不够。
    # 60s 默认值对 helper 倾向"我看默认就60s那大概就够"造成误导。
    # 改到 90s 是中间值,既给查询命令留余地(2-90s 都正常返回),
    # 又对 benchmark 类强行迫使 helper 显式传更长值(若它只想用默认)。
    timeout_sec = int(args.get("timeout_sec", 90) or 90)

    # 直接调 handle_run(不经 _handle_workspace)— 拿当前 dispatch 的 abort_event
    # 让 ctrl-c / 用户 abort 能中途打断长 bash 跑。
    from app.core.core_processes import current_abort_event
    abort_event = current_abort_event()
    result = await ws_tool.handle_run(
        workspace_dir,
        command,
        timeout_sec=timeout_sec,
        abort_event=abort_event,
        prefer_unix_shell=True,
    )

    # 2026-05-15 P112: bash 读类命令重复检测
    # 维护进程内 bash 命令计数 (per workspace_dir + command_fingerprint)
    # 重复 ≥ 2 次的读类命令注入提示, 引导用专用工具
    try:
        _bash_repeat_check(workspace_dir, command, result)
    except Exception:
        # 检测失败不影响主流程
        pass

    return json.dumps(result, ensure_ascii=False)


# 2026-05-15 P112: bash 命令跟踪 (进程内, 跨 helper)
_bash_command_tracker: dict[tuple[str, str], int] = {}
_BASH_REPEAT_WARN_THRESHOLD = 2  # 第 2 次重复就警告

# "读类"命令前缀 — 这些应该用专用工具替代, 重复就提示
_BASH_READ_LIKE_PATTERNS = [
    (r"^\s*type\s+\S+", "Use `read_file(path)` instead of `type X`; it provides read accounting and outline-based deduplication."),
    (r"^\s*cat\s+\S+", "Use `read_file(path)` instead of `cat X`; it provides read accounting and outline-based deduplication."),
    (r"^\s*more\s+\S+", "Use `read_file(path)` instead of `more X`."),
    (r"^\s*head\s+(-\w+\s+)?\S+", "Use `read_file(path, end_line=N)` instead of `head -N X`."),
    (r"^\s*tail\s+(-\w+\s+)?\S+", "Use `read_file(path, start_line=N)` instead of `tail X`."),
    (r"^\s*dir(\s|$)", "Use the workspace snapshot from context or `workspace(action='list')` instead of `dir`."),
    (r"^\s*ls(\s|$)", "Use the workspace snapshot from context or `workspace(action='list')` instead of `ls`."),
    (r"^\s*findstr\s+", "Use `search_in_file(path, pattern)` instead of `findstr`; use cross-file search only when needed."),
    (r"^\s*grep\s+", "Use `search_in_file(path, pattern)` or `search_across_files(pattern)` instead of repeated `grep`."),
]

import re as _re_p112

# 2026-05-15 P116: tracker GC 防止长期累积
# 长跑 chatbot 进程会反复处理任务, tracker 持续累积:
# - _bash_command_tracker: 不同命令 fingerprint
# - _search_tracker: 不同 search signature
# 单 entry 小但累积 50K+ 后可能 ~5MB. 主动 GC.
_TRACKER_GC_THRESHOLD = 5000  # 超过这个数就 GC
_TRACKER_GC_KEEP_RECENT = 1000  # GC 时保留最近 N 个


def _maybe_gc_tracker(tracker: dict) -> None:
    """如果 tracker 大于阈值, 删除最早的 entry, 保留最近 _TRACKER_GC_KEEP_RECENT."""
    if len(tracker) <= _TRACKER_GC_THRESHOLD:
        return
    # 简单 GC: 删除一半 (Python dict 保留插入顺序)
    keys_to_keep = list(tracker.keys())[-_TRACKER_GC_KEEP_RECENT:]
    items_to_keep = {k: tracker[k] for k in keys_to_keep}
    tracker.clear()
    tracker.update(items_to_keep)


def _bash_repeat_check(ws_dir: str, command: str, result: dict) -> None:
    """检测 bash 重复读类命令, 在 result 中注入提示。

    fingerprint = 命令前 60 字符 (允许变量略变如 timestamp)。

    2026-05-17 P157: 累计 ≥ 10 次硬警告 (类比 edit_thrashing_exceeded), 加 _hard_block。
    """
    if not command or not isinstance(result, dict):
        return
    fp = command[:60].strip()
    if not fp:
        return
    key = (ws_dir or "", fp)
    _bash_command_tracker[key] = _bash_command_tracker.get(key, 0) + 1
    count = _bash_command_tracker[key]
    # P116: 写入后做轻量 GC
    _maybe_gc_tracker(_bash_command_tracker)
    # P157: 累计 ≥ 10 次硬警告 — 强制 LLM 停止重复
    if count >= 10:
        result["_redundant_bash_hard_block"] = True
        result["_redundant_bash_warning"] = (
            f"This bash command has been repeated {count} times in this task. "
            "Further equivalent repetition is blocked at the result level because the prior output is already in context. "
            "Use that evidence, switch to a narrower tool, or change the plan before running another command.\n\n"
            "同一 bash 命令重复过多；复用已有结果，改用更窄工具或调整方案。"
        )
        result["_bash_repeat_count"] = count
        # 同时把 ok 改为 false-ish, 让 LLM 看见硬错误信号
        result["_repeat_block_action_required"] = (
            "If you need to re-check a file or directory, use read_file, inspect_file, or search_files with a narrower scope. "
            "If the command would rerun the same check, inspect the prior result instead.\n\n"
            "复查文件或目录时使用专用工具；相同检查优先看已有结果。"
        )
        return
    if count < _BASH_REPEAT_WARN_THRESHOLD:
        return
    # 检查是否是"读类"命令
    cmd_lower = command.strip().lower()
    for pattern, suggestion in _BASH_READ_LIKE_PATTERNS:
        if _re_p112.match(pattern, cmd_lower):
            result["_redundant_bash_warning"] = (
                f"This bash command has been repeated {count} times. {suggestion} "
                "Dedicated tools preserve accounting and deduplication better than repeated shell reads.\n\n"
                "bash 读取重复；改用专用读取或搜索工具以减少重复上下文。"
            )
            result["_bash_repeat_count"] = count
            return
    # 非读类命令但重复 ≥ 5 次也提示 (gcc 等)
    if count >= 5:
        result["_redundant_bash_warning"] = (
            f"This bash command has been repeated {count} times. "
            "For builds or tests, rerun only after relevant sources changed. "
            "For identical inputs and outputs, cite the prior result already in context.\n\n"
            "重复命令需先确认输入变化；相同结果直接复用。"
        )
        result["_bash_repeat_count"] = count





async def _handle_commit_to_main(workspace_dir: str, args: dict) -> str:
    from app.llm.tools.workspace_transfer_tools import handle_commit_to_main

    return await handle_commit_to_main(workspace_dir, args)


async def _handle_fetch_to_temp(workspace_dir: str, args: dict) -> str:
    from app.llm.tools.workspace_transfer_tools import handle_fetch_to_temp

    return await handle_fetch_to_temp(workspace_dir, args)


async def _handle_recall_thread(workspace_dir: str, args: dict) -> str:
    from app.llm.tools.workspace_transfer_tools import handle_recall_thread

    return await handle_recall_thread(workspace_dir, args)


async def _handle_continue_toolchain(
    archive_id: str,
    group_id: str,
    user_id: str,
    args: dict,
) -> str:
    from app.core import toolchain_cache

    result = toolchain_cache.continue_chain(
        archive_id=archive_id,
        group_id=group_id,
        user_id=user_id,
        trace_id=debug.current_trace_id() or "",
        reason=str((args or {}).get("reason") or ""),
        max_chars=(args or {}).get("max_chars"),
    )
    return json.dumps(result, ensure_ascii=False)


async def _handle_agent_state(args: dict) -> str:
    from app.llm.tools.agent_state_tool import handle_agent_state

    return await handle_agent_state(args)


async def _handle_task_plan(args: dict) -> str:
    from app.llm.tools.task_plan_tool import handle_task_plan

    return await handle_task_plan(args)


async def _handle_progress_note(workspace_dir: str, args: dict) -> str:
    """progress_note 工具 — helper 写一句状态摘要给主线程看。

    2026-05-03 引入。helper 是 long-running 任务时,主线程在 wait_helper 阶段
    只能等最终 result。中间想"心跳"目前唯一办法是写 .helper_summary.txt 然后
    被动等扫描。本工具调 update_helper_progress(note=...),让主线程 process
    registry 立即拿到最新 progress(通过 processes(action=peek) 显式查或在
    pause_state.active_helpers_detected 时一并展示)。
    """
    args = args or {}
    text = str(args.get("text") or "").strip()
    if not text:
        return json.dumps({
            "ok": False,
            "error": "progress_note requires `text` (a non-empty string)",
        }, ensure_ascii=False)
    if len(text) > 200:
        text = text[:200] + "...[截]"

    # 直接调 report_helper_progress(主线程调用是 no-op,helper 调有效)
    from app.core.core_processes import report_helper_progress, current_helper_proc_id
    pid = current_helper_proc_id()
    if not pid:
        return json.dumps({
            "ok": False,
            "error": (
                "progress_note is helper-only. The main process should surface status through progress events "
                "or its final response instead of calling this tool.\n"
                "progress_note 仅 helper 可用；主进程通过进度事件或最终回复展示状态。"
            ),
        }, ensure_ascii=False)
    ok = await report_helper_progress(note=text)
    return json.dumps({
        "ok": ok,
        "action": "progress_note",
        "proc_id": pid,
        "note": text,
        "info": "主线程在 processes(action='peek') 或 pause snapshot 时能看到这条 progress",
    }, ensure_ascii=False)


async def _handle_request_resource(workspace_dir: str, args: dict) -> str:
    """helper-only structured resource request.

    This is the single path that freezes a helper for missing external resources.
    Guard tools may recommend it, but should not set freeze fields themselves.
    """
    from app.core.core_processes import HELPER_OWNER_PREFIX, current_helper_kind, current_helper_proc_id, current_owner

    pid = current_helper_proc_id()
    owner = current_owner()
    helper_kind = current_helper_kind()
    if not pid and not str(owner or "").startswith(HELPER_OWNER_PREFIX) and not helper_kind:
        return json.dumps({
            "ok": False,
            "error": (
                "request_resource is helper-only. The main process should directly coordinate resources with delegate.\n"
                "request_resource 仅 helper 可用；主进程直接用 delegate 协调资源。"
            ),
        }, ensure_ascii=False)

    args = args or {}
    kind = str(args.get("kind", "")).strip().lower()
    valid = {
        "code", "edit", "draw", "verify", "tts", "read",
        "project_map", "file_summary", "impact_review", "inventory",
    }
    if kind == "ocr":
        kind = "read"
    if kind not in valid:
        return json.dumps({
            "ok": False,
            "error": f"request_resource.kind invalid: {kind!r}",
            "valid_kinds": sorted(valid),
        }, ensure_ascii=False)
    reason = str(args.get("reason", "")).strip()
    if not reason:
        return json.dumps({
            "ok": False,
            "error": "request_resource requires non-empty reason",
        }, ensure_ascii=False)
    needed = args.get("needed_outputs") or []
    if not isinstance(needed, list):
        needed = [str(needed)]
    needed = [str(x).strip() for x in needed if str(x).strip()][:20]
    resume_instruction = str(args.get("resume_instruction", "")).strip()
    return json.dumps({
        "ok": False,
        "action": "request_resource",
        "requires_main_resource": True,
        "resource_kind": kind,
        "suggested_helper_kind": kind,
        "blocked_reason": reason[:500],
        "needed_outputs": needed,
        "resume_instruction": resume_instruction[:1000],
        "wake_condition": {
            "kind": kind,
            "needed_outputs": needed,
            "resume_instruction": resume_instruction[:1000],
        },
        "main_thread_action": (
            "main process must decide: reuse an existing/sibling resource helper "
            f"that satisfies needed_outputs, spawn kind='{kind}' if missing, or "
            "resume/kill this helper if the resource is refused"
        ),
    }, ensure_ascii=False)


_todo_write_counters: dict[str, int] = {}  # ws_dir → call count
_TODO_WRITE_HARD_CAP = 25


async def _handle_todo_write(workspace_dir: str, args: dict) -> str:
    """TodoWrite handler — 参考 Claude Code 设计。

    2026-05-17 P158: 累计 ≥ 25 次硬拒绝。todo_write 是过程辅助工具,
    实测有 LLM 在一次任务里调 466+ 次, 全在更新 status 而不在干活 — 完全无效。
    """
    if not workspace_dir:
        return json.dumps({"ok": False, "error": "workspace not available"})
    # P158: 硬上限
    _todo_write_counters[workspace_dir] = _todo_write_counters.get(workspace_dir, 0) + 1
    cnt = _todo_write_counters[workspace_dir]
    _maybe_gc_tracker(_todo_write_counters)
    if cnt > _TODO_WRITE_HARD_CAP:
        return json.dumps({
            "ok": False,
            "error": "todo_write_hard_cap_exceeded",
            "call_count": cnt,
            "hard_cap": _TODO_WRITE_HARD_CAP,
            "hint": (
                f"todo_write has been called {cnt} times in this session, above the cap {_TODO_WRITE_HARD_CAP}. "
                "Treat todos as planning state, not execution. Move forward with evidence-producing tools, "
                "delegate, commit_to_main, or a final JSON plan as appropriate.\n"
                "todo_write 只是计划状态；超过上限后应转向实际执行、派发、提交或收束。"
            ),
        }, ensure_ascii=False)
    todos = (args or {}).get("todos", [])
    result = await ws_tool.handle_todo_write(workspace_dir, todos)
    return json.dumps(result, ensure_ascii=False)


async def _handle_todo_read(workspace_dir: str, args: dict) -> str:
    """TodoRead handler。"""
    if not workspace_dir:
        return json.dumps({"ok": False, "error": "workspace not available"})
    result = await ws_tool.handle_todo_read(workspace_dir)
    return json.dumps(result, ensure_ascii=False)


_PROJECT_SOURCE_WRITE_EXTS = {
    ".py", ".pyw",
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".cs", ".kt", ".kts", ".swift",
    ".php", ".rb", ".lua", ".r", ".jl",
    ".sh", ".bash", ".ps1", ".bat", ".cmd",
    ".sql", ".toml", ".yaml", ".yml", ".ini", ".cfg",
}


_MAIN_THREAD_FRAMEWORK_FILENAMES = (
    "framework", "contract", "outline", "spec", "schema", "scaffold",
    "interface", "interfaces", "plan", "blueprint", "protocol",
)


def _looks_like_main_project_path(norm_path: str) -> bool:
    if not norm_path or norm_path.startswith(("_scratch/", "scratch/", ".temp/", "ocr_raw/")):
        return False
    if norm_path.startswith(("_helpers_shared/", "_shared/")):
        return True
    suffix = os.path.splitext(norm_path)[1].lower()
    if suffix in _PROJECT_SOURCE_WRITE_EXTS:
        return True
    return "/" in norm_path


def _looks_like_framework_artifact_path(norm_path: str, content: str) -> bool:
    """Detect framework/contract artifacts even when written at workspace root."""
    lowered = (norm_path or "").lower()
    stem = os.path.splitext(os.path.basename(lowered))[0]
    suffix = os.path.splitext(lowered)[1].lower()
    if suffix not in {".txt", ".md", ".rst", ".json", ".yaml", ".yml", ".toml"}:
        return False
    if any(token in lowered for token in _MAIN_THREAD_FRAMEWORK_FILENAMES):
        return True
    text = (content if isinstance(content, str) else str(content)).lower()
    if len(text) < 1200:
        return False
    headings = (
        "framework", "contract", "acceptance", "expected_outputs", "input_files",
        "interface", "schema", "merge order", "ownership", "validation",
        "evidence map", "document outline", "shared goal",
    )
    return any(token in stem for token in _MAIN_THREAD_FRAMEWORK_FILENAMES) or sum(
        1 for token in headings if token in text
    ) >= 3


def _main_thread_long_text_artifact_kind(norm_path: str, content: str) -> str:
    """Classify long root text writes that should be produced by a helper."""
    suffix = os.path.splitext(norm_path.lower())[1]
    if suffix not in {".txt", ".md", ".rst", ".json", ".yaml", ".yml", ".toml"}:
        return ""
    text = content if isinstance(content, str) else str(content)
    line_count = text.count("\n") + (1 if text else 0)
    if len(text) < 1800 and line_count < 45:
        return ""
    if _looks_like_framework_artifact_path(norm_path, text):
        return "framework"
    return ""


def _helper_writable_artifact_path(norm_path: str) -> str:
    """Map main-thread readonly shared paths to helper-writable shared paths."""
    clean = (norm_path or "").replace("\\", "/").lstrip("./")
    if clean == "_shared":
        return "_helpers_shared"
    if clean.startswith("_shared/"):
        return "_helpers_shared/" + clean[len("_shared/"):]
    return clean


def _looks_like_main_thread_utility_script(norm_path: str, content: str) -> bool:
    """Allow compact coordinator utility scripts without weakening source guards."""
    suffix = os.path.splitext(norm_path)[1].lower()
    if suffix not in {".py", ".ps1", ".bat", ".cmd", ".sh"}:
        return False
    if "/" in norm_path or norm_path.startswith(("_shared/", "_helpers_shared/")):
        return False
    text = content if isinstance(content, str) else str(content)
    line_count = text.count("\n") + (1 if text else 0)
    if len(text) > 4000 or line_count > 120:
        return False
    lowered = text.lower()
    utility_markers = (
        "open(", "read_text", "write_text", "json.", "csv", "re.sub",
        "replace(", "splitlines(", "print(", "pathlib", "os.path",
    )
    implementation_markers = (
        "class ", "def insert", "def delete", "def search", "def main(",
        "pytest", "unittest", "flask", "fastapi", "uvicorn", "benchmark",
        "subprocess", "compile", "g++", "gcc", "npm ", "package.json",
    )
    return any(marker in lowered for marker in utility_markers) and not any(
        marker in lowered for marker in implementation_markers
    )


def _main_thread_project_write_warning(helper_kind: str, path: str, content: str) -> dict[str, Any] | None:
    """Return recoverable feedback when the main thread tries to author project artifacts."""
    if helper_kind:
        return None
    norm_path = (path or "").replace("\\", "/").lstrip("./")
    content_text = content if isinstance(content, str) else str(content)
    if not _looks_like_main_project_path(norm_path):
        artifact_kind = _main_thread_long_text_artifact_kind(norm_path, content_text)
        if not artifact_kind:
            return None
    if _looks_like_main_thread_utility_script(norm_path, content_text):
        return None
    suffix = os.path.splitext(norm_path)[1].lower()
    if norm_path.startswith("_env/"):
        line_count = content_text.count("\n") + (1 if content_text else 0)
        if len(content_text) <= 1200 and line_count <= 40:
            return None
    lowered = norm_path.lower()
    project_contract_markers = (
        "/_shared/",
        "/contract",
        "/contracts/",
        "/framework",
        "/scaffold",
        "/interface",
        "/interfaces/",
        "/base.",
        "contract.",
        "framework.",
        "scaffold.",
    )
    is_project_source = suffix in _PROJECT_SOURCE_WRITE_EXTS
    is_project_framework = any(marker in lowered for marker in project_contract_markers) or _looks_like_framework_artifact_path(norm_path, content_text)
    if not (is_project_source or is_project_framework):
        return None
    content_l = content_text.lower()
    document_contract_markers = (
        "paper", "report", "document", "chapter", "outline", "toc",
        "docx", "word", "论文", "报告", "文档", "章节", "大纲", "目录",
    )
    technical_contract_markers = (
        "source", "implementation", "interface", "api", "schema", "benchmark",
        "harness", "compile", "test", "module", "源码", "实现", "接口", "基准",
        "编译", "测试", "模块",
    )
    suggested_kind = "code"
    if any(marker in lowered or marker in content_l for marker in document_contract_markers) and not any(
        marker in lowered or marker in content_l for marker in technical_contract_markers
    ):
        suggested_kind = "edit"
    elif suffix in {".md", ".txt", ".rst"} and any(marker in content_l for marker in document_contract_markers):
        suggested_kind = "edit"
    framework_hint = (
        "Summarize the shared goal, document outline, evidence/source map, section ownership, validation checks, and merge order."
        if suggested_kind == "edit"
        else "Summarize the shared goal, interfaces, ownership boundaries, validation checks, and merge order."
    )
    helper_output_path = _helper_writable_artifact_path(norm_path)
    prompt_goal = (
        f"Create or update only {helper_output_path!r} as a helper-owned staged document/report framework contract."
        if suggested_kind == "edit"
        else (
            f"Create or update only {helper_output_path!r} as a helper-owned staged output according to the shared "
            "framework. Keep the scope to this artifact and report whether it is an internal handoff or should later "
            "be applied as a project file."
        )
    )
    return {
        "ok": False,
        "error": "main_thread_project_artifact_should_delegate",
        "error_kind": "main_thread_project_artifact_should_delegate",
        "blocked_reason": "main_thread_project_artifact_should_delegate",
        "recovery_action": "switch_tool",
        "retry_same_tool": False,
        "recommended_tools": ["delegate"],
        "blocked_path": norm_path,
        "content_chars": len(content_text),
        "content_lines": content_text.count("\n") + (1 if content_text else 0),
        "hint": (
            "The main thread coordinates project source, framework contracts, shared interfaces, "
            "scaffolds, benchmark harnesses, and implementation files by delegating artifact creation to helpers. Keep the "
            "current plan and delegate this artifact to a focused helper using a helper request envelope "
            "with `task_id`, `kind`, `mode`, `framework`, `input_files`, `prompt`, `expected_outputs`, "
            "and `acceptance_checks`. The main thread coordinates, inspects helper output, merges or "
            "apply accepted artifacts, and run final verification.\n"
            "主进程不直接写项目源码、共享接口、框架契约或脚手架；请用完整 helper envelope 派发给对应 helper，主进程负责协调、验收、合并和最终验证。"
        ),
        "suggested_next_action": {
            "tool": "delegate",
            "same_goal": True,
            "task_template": {
                "task_id": "focused_project_artifact",
                "kind": suggested_kind,
                "mode": "easy",
                "framework": framework_hint,
                "input_files": [],
                "prompt": prompt_goal,
                "expected_outputs": [helper_output_path],
                "acceptance_checks": ["read or inspect the smallest check that validates this artifact"],
            },
        },
    }

 
_PROJECT_RUN_SCRIPT_EXTS = {
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx",
    ".sh", ".bash", ".ps1", ".bat", ".cmd",
}

_PROJECT_RUN_DELEGATE_KEYWORDS = (
    "benchmark", "bench", "perf", "profile", "profiling",
    "stress", "loadtest", "load-test", "soak", "long_run",
)


def _main_thread_project_run_warning(
    helper_kind: str,
    command: str,
    timeout_sec: int | None,
) -> dict[str, Any] | None:
    """Return recoverable feedback when the main thread tries to run a production workload."""
    if helper_kind:
        return None
    command_text = str(command or "").strip()
    if not command_text:
        return None
    lowered = command_text.lower()
    if not any(keyword in lowered for keyword in _PROJECT_RUN_DELEGATE_KEYWORDS):
        return None
    if re.search(r"\b(?:pytest|py\.test|python\s+-m\s+pytest|python\s+-m\s+py_compile)\b", lowered):
        return None
    script_paths: list[str] = []
    for match in re.finditer(
        r"(?<![\w./\\-])([A-Za-z0-9_./\\-]+\.(?:pyw?|m?js|cjs|tsx?|sh|bash|ps1|bat|cmd))\b",
        command_text,
    ):
        rel = match.group(1).replace("\\", "/").lstrip("./")
        suffix = os.path.splitext(rel)[1].lower()
        if suffix in _PROJECT_RUN_SCRIPT_EXTS and _looks_like_main_project_path(rel):
            script_paths.append(rel)
    if not script_paths:
        return None
    return {
        "ok": False,
        "error": "main_thread_project_run_should_delegate",
        "error_kind": "main_thread_project_run_should_delegate",
        "recovery_action": "switch_tool",
        "retry_same_tool": False,
        "recommended_tools": ["delegate"],
        "blocked_command": command_text[:500],
        "blocked_scripts": list(dict.fromkeys(script_paths)),
        "timeout_sec": timeout_sec,
        "hint": (
            "The main process coordinates substantial benchmark, performance, stress, profiling, and long "
            "project workloads through code helpers. Delegate the workload with the current framework, "
            "input files, expected outputs, and acceptance checks. The main process collects the helper "
            "result, inspect produced CSV/JSON/stdout/artifacts, and run only bounded final verification.\n"
            "主进程不直接运行长 benchmark、性能、压力或 profiling 工作负载；请派发 code helper 执行，主进程负责收集、验收和最终小范围验证。"
        ),
        "suggested_next_action": {
            "tool": "delegate",
            "same_goal": True,
            "task_template": {
                "task_id": "run_project_workload",
                "kind": "code",
                "mode": "easy",
                "framework": "Use the current shared project contract, exact input paths, output schema, and acceptance checks.",
                "input_files": list(dict.fromkeys(script_paths)),
                "prompt": f"Run the bounded project workload command and report verified outputs: {command_text}",
                "expected_outputs": [],
                "acceptance_checks": ["capture command result, output files, and any failed cases"],
            },
        },
    }


_LONG_TEXT_ARTIFACT_EXTS = {
    ".md",
    ".txt",
    ".rst",
    ".tex",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".tsv",
}


def _helper_large_text_write_warning(helper_kind: str, path: str, content: str) -> dict[str, Any] | None:
    """Return recoverable feedback for oversized helper text/report writes."""
    if helper_kind not in {"edit", "code"}:
        return None
    norm_path = (path or "").replace("\\", "/").lstrip("./")
    if not norm_path or norm_path.startswith("_scratch/") or norm_path.startswith("scratch/"):
        return None
    suffix = os.path.splitext(norm_path)[1].lower()
    if suffix not in _LONG_TEXT_ARTIFACT_EXTS:
        return None
    content_text = content if isinstance(content, str) else str(content)
    char_count = len(content_text)
    # 2026-06-05: 字符总量是真正成本指标; 行数多但字符不大(短 bullet/大纲)不应硬卡。
    # 病因(实测 trace 394304 14:44:31): paper_outline.md 2786 字符 167 行短 bullet 大纲
    # 触发 line_count > 140 被硬拒, helper 卡住浪费 ~1 分钟。
    # 修法: 仅按字符总量(6000)判断;丢弃行数子条件(对短 bullet 误伤)。
    if char_count <= 6000:
        return None
    line_count = content_text.count("\n") + (1 if content_text else 0)
    return {
        "ok": False,
        "error": "helper_large_text_write_should_segment",
        "error_kind": "helper_large_text_write_should_segment",
        "blocked_path": norm_path,
        "content_chars": char_count,
        "content_lines": line_count,
        "recovery_action": "continue_same_task_segmented",
        "retry_same_tool": False,
        "hint": (
            "This text/report artifact is too large for one opaque workspace.write call. Keep the same task_id "
            "and continue with segmented authoring: write a compact skeleton or table of contents first, then "
            "append or edit one named section at a time in blocks of roughly 2,000-4,000 characters. For long "
            "papers, reports, contracts, datasets, or analysis notes, create section files such as "
            "`sections/01_background.md` and let a later assembly step merge them. Keep the same task identity. "
            "Use read_file on the local fragment before focused edits, and finish with the required Output files "
            "JSON only after the declared files exist.\n"
            "长文本/报告写入过大；保持同一任务，先写骨架，再按命名章节小段追加或拆成章节文件，完成后再声明 Output files。"
        ),
        "suggested_next_action": {
            "same_task": True,
            "write_first": "compact skeleton or section index for the target artifact",
            "then": "append or edit one named section at a time, about 2,000-4,000 characters per call",
            "large_artifact_strategy": "split long papers/reports/contracts into section files before final assembly",
            "avoid": "do not retry the same oversized workspace.write and do not create a v2 task",
            "finish": "declare exact existing paths in the final Output files JSON block",
        },
    }


def _helper_monolithic_project_write_warning(helper_kind: str, path: str, content: str) -> dict[str, Any] | None:
    """Return recoverable feedback for oversized helper source writes."""
    if helper_kind != "code":
        return None
    norm_path = (path or "").replace("\\", "/").lstrip("./")
    if not norm_path.startswith("_env/"):
        return None
    suffix = os.path.splitext(norm_path)[1].lower()
    if suffix not in _PROJECT_SOURCE_WRITE_EXTS:
        return None
    content_text = content if isinstance(content, str) else str(content)
    line_count = content_text.count("\n") + (1 if content_text else 0)
    char_count = len(content_text)
    if char_count <= 8000 and line_count <= 180:
        return None
    return {
        "ok": False,
        "error": "helper_monolithic_project_write_should_segment",
        "error_kind": "helper_monolithic_project_write_should_segment",
        "blocked_path": norm_path,
        "content_chars": char_count,
        "content_lines": line_count,
        "hint": (
            "This project source/script write is too large for one opaque workspace.write call. "
            "Keep the same task and continue with a segmented authoring workflow: write a compact "
            "skeleton or interface first, then use focused edit_file/multi_edit/insert steps for "
            "individual functions, classes, modules, config sections, or split the deliverable into "
            "smaller project files listed in the helper contract. Use workspace file tools rather than the isolated `python` tool "
            "for workspace file IO; use workspace.write only for small skeletons or new segment files, and use "
            "read_file plus edit_file/multi_edit/insert_in_file for follow-up edits. Verify after the pieces are assembled.\n"
            "该源码/脚本写入过大；请保持当前任务，先写骨架或接口，再按函数、类、模块或配置段分步编辑，必要时拆成多个契约内文件并验证。"
        ),
        "suggested_next_action": {
            "same_task": True,
            "write_first": "small skeleton/interface for the target file with workspace.write",
            "then": "read_file plus focused edit_file/multi_edit/insert_in_file operations, or split files listed in expected_outputs",
            "avoid": "do not use the isolated python tool for workspace file IO",
            "verify": "run the helper contract acceptance checks after assembly",
        },
    }


async def _handle_workspace(workspace_dir: str, args: dict) -> str:
    args = args or {}
    action = str(args.get("action", "")).strip().lower()

    # 2026-05-02 Bug E 修: 模型经常漏传 action 字段(实测主线程 trace 三批/任务都漏过)。
    # 从其他参数推断:
    #   - 有 command            → action="run"   (workspace.run 最高频)
    #   - 有 path 且有 content   → action="write"
    #   - 只有 path             → action="mkdir"
    # 推断后记录 debug,让运维能 grep 出来观测频率。
    if not action:
        if "command" in args:
            action = "run"
        elif "path" in args and "content" in args:
            action = "write"
        elif "path" in args and len(args) <= 2:
            # 只有 path(可能加 timeout_sec/recursive 等 opts)→ mkdir
            action = "mkdir"
        else:
            return json.dumps({
                "ok": False,
                "error": (
                    "workspace requires action=mkdir/write/run/locate, or enough fields to infer it: "
                    "command for run, path+content for write, or path alone for mkdir. "
                    f"Received args keys: {list(args.keys())}.\n"
                    "workspace 需要 action 或可推断的参数组合。"
                ),
            }, ensure_ascii=False)
        debug.log(
            f"workspace.action_inferred",
            f"inferred action={action} from args keys {list(args.keys())}",
        )

    if action not in ("mkdir", "write", "run", "locate"):
        return json.dumps({"ok": False, "error": f"unknown action: {action!r}"})
    if not workspace_dir:
        return json.dumps({"ok": False, "error": "workspace not available (easy path)"})
    try:
        from app.core.core_processes import current_helper_kind
        helper_kind = current_helper_kind()
    except Exception:
        helper_kind = ""
    if helper_kind == "general":
        return json.dumps({
            "ok": False,
            "error": (
                "The legacy general helper kind has been removed. Use a concrete helper kind and request "
                "the required resource instead of using workspace directly.\n"
                "general helper 已移除；应改用具体 helper 类型并按需请求资源。"
            ),
            "blocked_reason": "general_helper_workspace_forbidden",
            "blocked_action": action,
            "suggested_helper_kind": "code",
            "suggested_tool": "request_resource",
        }, ensure_ascii=False)
    if helper_kind == "edit" and action == "run":
        return json.dumps({
            "ok": False,
            "error": (
                "edit helpers cannot use workspace.run. If commands, scripts, tests, or computation are needed, "
                "request a code or read resource through request_resource.\n"
                "edit helper 需要命令或计算时应请求 code/read 资源。"
            ),
            "blocked_reason": "edit_helper_workspace_run_forbidden",
            "blocked_action": action,
            "suggested_helper_kind": "code",
            "suggested_tool": "request_resource",
        }, ensure_ascii=False)
    if helper_kind in {"read", "ocr"}:
        if action != "write":
            return json.dumps({
                "ok": False,
                "error": (
                    "read helpers may only write internal .txt evidence with workspace. Use reading/OCR tools "
                    "for clearer evidence, or request_resource when another resource is needed.\n"
                    "read helper 只能写 .txt 内部证据，其他需求应使用读取工具或请求资源。"
                ),
                "blocked_reason": "read_helper_workspace_action_forbidden",
                "blocked_action": action,
            }, ensure_ascii=False)
        read_path = str(args.get("path", "")).strip().lower()
        if not read_path.endswith(".txt"):
            return json.dumps({
                "ok": False,
                "error": (
                    "read helpers may only write .txt internal evidence, not images, scripts, Office files, or source code.\n"
                    "read helper 只能写 .txt 内部证据。"
                ),
                "blocked_reason": "read_helper_workspace_path_forbidden",
                "blocked_path": str(args.get("path", "")).strip(),
            }, ensure_ascii=False)
    if helper_kind == "tts":
        if action != "write":
            return json.dumps({
                "ok": False,
                "error": (
                    "tts helpers may only write .txt internal text material with workspace. Use the tts tool to generate audio.\n"
                    "tts helper 只能写 .txt 文本材料，音频生成使用 tts 工具。"
                ),
                "blocked_reason": "tts_helper_workspace_action_forbidden",
                "blocked_action": action,
            }, ensure_ascii=False)
        tts_path = str(args.get("path", "")).strip().lower()
        if not tts_path.endswith(".txt"):
            return json.dumps({
                "ok": False,
                "error": (
                    "tts helpers may only write .txt text notes, not audio, scripts, Office files, or source code.\n"
                    "tts helper 只能写 .txt 文本说明。"
                ),
                "blocked_reason": "tts_helper_workspace_path_forbidden",
                "blocked_path": str(args.get("path", "")).strip(),
            }, ensure_ascii=False)
    if helper_kind in {"inventory", "summarize"}:
        if action not in {"run", "locate", "write", "mkdir"}:
            return json.dumps({
                "ok": False,
                "error": (
                    "inventory helpers may inspect, locate, and write temporary analysis scripts or notes. "
                    "Use code/edit/read helpers for implementation, artifacts, extraction, or document work.\n"
                    "inventory helper 只做检查、定位和临时分析脚本/笔记；实现、产物和材料抽取交给对应 helper。"
                ),
                "blocked_reason": "inventory_helper_workspace_action_forbidden",
                "blocked_action": action,
                "suggested_helper_kind": "code",
                "suggested_tool": "request_resource",
            }, ensure_ascii=False)
        if action in {"write", "mkdir"}:
            raw_path = str(args.get("path", "")).strip()
            norm_path = raw_path.replace("\\", "/").lstrip("./")
            scratch_roots = {"_scratch", "scratch"}
            scratch_prefixes = tuple(root + "/" for root in scratch_roots)
            if not norm_path:
                return json.dumps({
                    "ok": False,
                    "error": "path is required for inventory helper temporary write/mkdir",
                    "blocked_reason": "inventory_helper_workspace_path_forbidden",
                }, ensure_ascii=False)
            if not (norm_path in scratch_roots or norm_path.startswith(scratch_prefixes)):
                return json.dumps({
                    "ok": False,
                    "error": (
                        "inventory helper temporary files belong under `_scratch/` or `scratch/`. "
                        "Project files, shared inputs, and deliverables belong to the matching helper contract.\n"
                        "inventory helper 的临时文件应写在 `_scratch/` 或 `scratch/`。"
                    ),
                    "blocked_reason": "inventory_helper_workspace_path_forbidden",
                    "blocked_path": raw_path,
                    "suggested_path": "_scratch/inventory_probe.py",
                }, ensure_ascii=False)
            if action == "write":
                suffix = os.path.splitext(norm_path)[1].lower()
                if suffix not in {".py", ".txt", ".md", ".json", ".csv", ".tsv", ".log"}:
                    return json.dumps({
                        "ok": False,
                        "error": (
                            "inventory helper may only write temporary text/data/script files. "
                            "Use the matching helper kind for Office, media, images, archives, binaries, or deliverables.\n"
                            "inventory helper 只能写临时文本、数据或脚本文件。"
                        ),
                        "blocked_reason": "inventory_helper_workspace_path_forbidden",
                        "blocked_path": raw_path,
                    }, ensure_ascii=False)
    if helper_kind == "verify":
        if action not in {"run", "locate"}:
            return json.dumps({
                "ok": False,
                "error": (
                    "verify helpers may only run validation commands or locate files. "
                    "Use the producing helper or main process for repairs and artifacts.\n"
                    "verify helper 只做验证运行和文件定位；修复与产物交给对应执行方。"
                ),
                "blocked_reason": "verify_helper_workspace_action_forbidden",
                "blocked_action": action,
                "suggested_helper_kind": "code",
                "suggested_tool": "request_resource",
            }, ensure_ascii=False)
    if action == "mkdir":
        path = str(args.get("path", "")).strip()
        if not path:
            return json.dumps({"ok": False, "error": "path is required for mkdir"})
        result = await ws_tool.handle_mkdir(workspace_dir, path)

    elif action == "write":
        path = str(args.get("path", "")).strip()
        content = args.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        if not path:
            return json.dumps({"ok": False, "error": "path is required for write"})
        warning = _main_thread_project_write_warning(helper_kind, path, content)
        if warning is not None:
            return json.dumps(warning, ensure_ascii=False)
        warning = _helper_large_text_write_warning(helper_kind, path, content)
        if warning is not None:
            return json.dumps(warning, ensure_ascii=False)
        warning = _helper_monolithic_project_write_warning(helper_kind, path, content)
        if warning is not None:
            return json.dumps(warning, ensure_ascii=False)
        result = await ws_tool.handle_write(workspace_dir, path, content)

    elif action == "run":
        command = str(args.get("command", "")).strip()
        if not command:
            return json.dumps({"ok": False, "error": "command is required for run"})
        # timeout_sec: LLM 自决预期耗时(1~300s),None=走 1s 默认(故意很短,强制 LLM 显式传)
        timeout_sec = args.get("timeout_sec")
        if timeout_sec is not None:
            try:
                timeout_sec = int(timeout_sec)
            except (TypeError, ValueError):
                timeout_sec = None
        warning = _main_thread_project_run_warning(helper_kind, command, timeout_sec)
        if warning is not None:
            return json.dumps(warning, ensure_ascii=False)
        # ── abort_event: 从 ContextVar 读取(Phase 5++ — 中途 abort 修)──
        # 主线程: 共享 group abort;helper: local abort(已包含 shared 桥接)
        from app.core.core_processes import current_abort_event
        abort_event = current_abort_event()
        result = await ws_tool.handle_run(
            workspace_dir, command,
            timeout_sec=timeout_sec, abort_event=abort_event,
        )

    elif action == "locate":
        pattern = str(args.get("pattern", "")).strip()
        result = await ws_tool.handle_locate(workspace_dir, pattern)

    return json.dumps(result, ensure_ascii=False)


async def _handle_read_file(workspace_dir: str, args: dict) -> str:
    if not workspace_dir:
        return json.dumps({"ok": False, "error": "workspace not available (easy path)"})
    path = str(args.get("path") or args.get("file_path") or args.get("filename") or "").strip()
    inventory_guard = _inventory_helper_read_guard(path, tool_name="read_file")
    if inventory_guard is not None:
        return json.dumps(inventory_guard, ensure_ascii=False)
    start_line = int(args.get("start_line", 1) or 1)
    end_line = int(args.get("end_line", -1) or -1)
    # 2026-05-02 part22 微调:不再硬编码 16000(老值),让 workspace.handle_read_file
    # 用自己的 _READ_MAX_CHARS_DEFAULT(500KB,part20 调高的值)。
    # 之前的 bug:workspace.py 默认 500K,但 dispatcher 这里默认 16K,模型不传时
    # 实际只能拿 16K。三处不一致(workspace=500K, schema=80K, dispatcher=16K)。
    if "max_chars" in args and args["max_chars"] is not None:
        max_chars = int(args["max_chars"])
        result = await ws_tool.handle_read_file(
            workspace_dir, path,
            start_line=start_line, end_line=end_line, max_chars=max_chars,
        )
    else:
        result = await ws_tool.handle_read_file(
            workspace_dir, path,
            start_line=start_line, end_line=end_line,
        )
    if (
        not result.get("ok")
        and "_delegate_" not in str(workspace_dir or "")
        and _should_try_env_read_redirect(path, result)
    ):
        redirected = _try_env_read_redirect(path, start_line, end_line, args)
        if redirected is not None:
            return json.dumps(redirected, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


def _inventory_helper_read_guard(path: str, *, tool_name: str) -> dict | None:
    """Keep inventory helpers from drifting into source-material extraction."""
    try:
        from app.core.core_processes import current_helper_kind
        helper_kind = current_helper_kind()
    except Exception:
        helper_kind = ""
    if helper_kind not in {"inventory", "summarize"}:
        return None
    norm = str(path or "").replace("\\", "/").strip().strip("`\"'").lstrip("./")
    low = norm.lower()
    if not norm:
        return None
    base = low.rsplit("/", 1)[-1]
    allowed_exact = {
        "_env/project_inventory.md",
        "_env/.resource_manifest.json",
        "_session_manifest.json",
        "project_inventory.md",
        ".resource_manifest.json",
    }
    allowed_bases = {
        "readme.md", "readme.txt", "package.json", "pyproject.toml",
        "requirements.txt", "setup.py", "setup.cfg", "makefile",
        "cmakelists.txt", "tox.ini", "pytest.ini",
    }
    if low in allowed_exact or base in allowed_bases:
        return None
    if low.startswith(("_scratch/", "scratch/")):
        return None
    material_exts = (
        ".txt", ".md", ".html", ".htm", ".docx", ".doc", ".pdf", ".xlsx", ".xls",
        ".pptx", ".ppt", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif",
        ".mp3", ".wav", ".m4a", ".zip", ".rar", ".7z",
    )
    if low.startswith("_env/") and low.endswith(material_exts):
        return {
            "ok": False,
            "error": (
                f"inventory helper cannot use {tool_name} to extract source-material body content from {norm!r}. "
                "Use `_env/project_inventory.md`, `_env/.resource_manifest.json`, locate/search, and lightweight "
                "statistics to classify paths. Delegate actual text/OCR/Office/PDF/archive reading to read helpers.\n\n"
                "inventory 只做目录和清单摸底；材料正文读取交给 read helper。"
            ),
            "blocked_reason": "inventory_helper_source_material_read_forbidden",
            "blocked_path": norm,
            "suggested_helper_kind": "read",
            "suggested_next_action": (
                "Return an inventory category and a focused read-helper recommendation for this path group.\n"
                "返回目录分类，并建议 read helper 读取该路径组。"
            ),
        }
    return None


def _should_try_env_read_redirect(path: str, result: dict) -> bool:
    if not path:
        return False
    try:
        from app.core.filesystem import PathZone, classify_path
        classification = classify_path(path)
        if classification.zone in {PathZone.STAGED_ROOT, PathZone.STAGED_FILE} or classification.is_directory_hint:
            return False
    except Exception:
        pass
    error = str(result.get("error") or "").lower()
    if (
        "file not found" not in error
        and "not found" not in error
        and "absolute path must be inside this sandbox" not in error
    ):
        return False
    try:
        from app.core.runtime_mode import is_environment_mode
        if not is_environment_mode():
            return False
        if workspace_dir := result.get("_workspace_dir"):
            return "_delegate_" not in str(workspace_dir)
        return True
    except Exception:
        return False


def _project_path_for_environment_redirect(path: str) -> str | None:
    """Map read-only workspace misses to an environment project path.

    In environment mode, real project files are read with env_* tools. Models
    still sometimes pass `app/...` directly to workspace tools or pass
    `_env/app/...` before the file has been fetched. For read-only evidence
    tools, redirecting to the project path is safe and keeps main/helper
    behavior consistent. Editing tools must still use env_fetch first.

    environment 模式下读取型工具可自动转到项目目录；编辑仍必须先 env_fetch。
    """
    raw = (path or "").strip().strip('"')
    if not raw:
        return None
    norm = raw.replace("\\", "/")
    try:
        from app.core.runtime_mode import current_environment, is_environment_mode
        from app.llm.tools import environment as env_tool

        if not is_environment_mode():
            return None
        env = current_environment()
        if os.path.isabs(raw):
            if env is None:
                return None
            from pathlib import Path as _Path

            root = _Path(env.root_dir).resolve()
            target = _Path(raw).resolve()
            try:
                norm = target.relative_to(root).as_posix()
            except ValueError:
                return None
    except Exception:
        return None
    if norm == "_env":
        return None
    if norm.startswith("_env/"):
        norm = norm[5:].lstrip("/")
    if not norm or norm.startswith("../") or "/../" in f"/{norm}/":
        return None
    try:
        env_tool._resolve_env_path(norm, must_exist=True)
    except Exception:
        return None
    return norm


def _try_env_read_redirect(path: str, start_line: int, end_line: int, args: dict) -> dict | None:
    env_path = _project_path_for_environment_redirect(path)
    if not env_path:
        return None
    try:
        from app.llm.tools import environment as env_tool
        env_args = {
            "path": env_path,
            "start_line": start_line,
            "end_line": end_line,
        }
        if "max_chars" in args and args["max_chars"] is not None:
            env_args["max_chars"] = args["max_chars"]
        redirected = env_tool._handle_read(env_args)
    except Exception as exc:
        return {
            "ok": False,
            "error": (
                f"read_file could not find {path!r} in the chat workspace. "
                f"An automatic env_read retry also failed: {type(exc).__name__}: {exc}"
            ),
            "_next_action_instruction": (
                "Use env_list_tree/env_read/env_search for project-directory files. "
                "Use read_file only for chat-workspace files or fetched _env/... copies.\n\n"
                "项目目录文件请用 env_* 工具；read_file 只用于聊天工作区或已 fetch 的 _env 副本。"
            ),
        }
    if not isinstance(redirected, dict) or not redirected.get("ok"):
        return None
    redirected = dict(redirected)
    redirected["_redirected_from"] = "read_file"
    redirected["_original_workspace_path"] = path
    redirected["_next_action_instruction"] = (
        "This read-only workspace request was redirected to env_read because the target is a project file. "
        "Use env_read/env_search/env_list_tree/env_run for real project files. Use read_file only for "
        "chat-workspace files or fetched _env/... copies.\n\n"
        "本次读取已自动转为 env_read；真实项目文件后续直接用 env_*，已 fetch 的 _env 副本才用工作区工具。"
    )
    return redirected


def _workspace_project_path_hint(path: str, tool_name: str) -> dict | None:
    if not _should_try_env_read_redirect(path, {"error": "file not found"}):
        return None
    env_path = _project_path_for_environment_redirect(path)
    if not env_path:
        return None
    return {
        "ok": False,
        "error": (
            f"{tool_name} looked for {path!r} in the chat workspace, but the current mode has a separate "
            "project directory. Main processes may use env_read/env_search/env_list_tree/env_run. "
            "Helpers should read `_env/project_inventory.md` or `_env/.resource_manifest.json`, then use "
            "fetch_to_temp(source='main', paths=[project_path]) or request_resource for missing project files. "
            "Only use workspace tools for chat-workspace files or _env/... fetched copies.\n\n"
            "当前模式存在独立项目目录；主进程用 env_*，helper 按清单获取精确项目路径。"
        ),
        "_project_path": env_path,
        "_next_action_instruction": (
            "Use the project resource manifest as path truth. Main processes may use env_read/env_search/env_list_tree/env_run. "
            "Helpers should fetch exact project_path entries into `_env/...` or request_resource when absent. "
            "Workspace read/search/code_index tools only see chat-workspace files and fetched _env/... copies.\n\n"
            "项目资源清单是路径依据；helper 按精确 project_path 获取或请求资源，工作区工具只看已暂存副本。"
        ),
    }


async def _handle_edit_file(workspace_dir: str, args: dict) -> str:
    if not workspace_dir:
        return json.dumps({"ok": False, "error": "workspace not available (easy path)"})
    path = str(args.get("path", "")).strip()
    old_str = args.get("old_str", "")
    new_str = args.get("new_str", "")
    if not isinstance(old_str, str):
        old_str = str(old_str)
    if not isinstance(new_str, str):
        new_str = str(new_str)
    # ── 2026-05-05: 空编辑检测 ──
    if old_str and old_str == new_str:
        result = {
            "ok": True,
            "action": "edit_file",
            "path": path,
            "_empty_edit": True,
            "_empty_edit_warning": (
                "old_str and new_str are identical, so this edit made no file change. "
                "Re-read the exact target region and construct a real replacement before trying again.\n\n"
                "本次编辑无变化；重新定位代码段后再构造有效替换。"
            ),
        }
        return json.dumps(result, ensure_ascii=False)
    expected_count = int(args.get("expected_count", 1) or 1)
    result = await ws_tool.handle_edit_file(
        workspace_dir, path, old_str, new_str,
        expected_count=expected_count,
    )
    return json.dumps(result, ensure_ascii=False)


# ── 2026-05-02 part20:multi_edit dispatcher ──
async def _handle_multi_edit(workspace_dir: str, args: dict) -> str:
    """Claude Code 风格 MultiEdit dispatcher。"""
    if not workspace_dir:
        return json.dumps({"ok": False, "error": "workspace not available (easy path)"})
    path = str(args.get("path", "")).strip()
    edits = args.get("edits", [])
    if not isinstance(edits, list):
        return json.dumps({
            "ok": False,
            "error": f"edits must be a list, got {type(edits).__name__}",
        })
    result = await ws_tool.handle_multi_edit(workspace_dir, path, edits)
    return json.dumps(result, ensure_ascii=False)


async def _handle_insert_in_file(workspace_dir: str, args: dict) -> str:
    if not workspace_dir:
        return json.dumps({"ok": False, "error": "workspace not available (easy path)"})
    path = str(args.get("path", "")).strip()
    after_line = int(args.get("after_line", 0) or 0)
    content = args.get("content_to_insert", "")
    if not isinstance(content, str):
        content = str(content)
    result = await ws_tool.handle_insert_in_file(
        workspace_dir, path, after_line, content,
    )
    return json.dumps(result, ensure_ascii=False)


async def _handle_search_in_file(workspace_dir: str, args: dict) -> str:
    if not workspace_dir:
        return json.dumps({"ok": False, "error": "workspace not available (easy path)"})
    path = str(args.get("path", "")).strip()
    pattern = str(args.get("pattern", ""))
    is_regex = bool(args.get("is_regex", False))
    max_results = int(args.get("max_results", 50) or 50)
    result = await ws_tool.handle_search_in_file(
        workspace_dir, path, pattern,
        is_regex=is_regex, max_results=max_results,
    )
    if not result.get("ok") and "_delegate_" not in str(workspace_dir or ""):
        hint = _workspace_project_path_hint(path, "search_in_file")
        if hint is not None:
            try:
                from app.llm.tools import environment as env_tool

                redirected = env_tool._handle_search({
                    "path": hint.get("_project_path") or path,
                    "query": pattern,
                    "regex": is_regex,
                    "limit": max_results,
                })
                if isinstance(redirected, dict) and redirected.get("ok"):
                    redirected = dict(redirected)
                    redirected["_redirected_from"] = "search_in_file"
                    redirected["_next_action_instruction"] = hint["_next_action_instruction"]
                    result = redirected
                else:
                    result = hint
            except Exception as exc:
                hint["error"] += f" Automatic env_search retry failed: {type(exc).__name__}: {exc}"
                result = hint
    # 2026-05-15 P114: search 重复检测
    _search_repeat_check("search_in_file", workspace_dir, f"{path}|{pattern}", result)
    return json.dumps(result, ensure_ascii=False)


# 2026-05-15 P114: search 类工具重复跟踪
# 病因(实测压缩 trace): 580+ search 调用, 9× `#include`, 6× `comp_ops_t` 等
# 反复 search 同 pattern 浪费 ~10-20 分钟 cumulative time。
# 修法: 维护 (tool, ws_dir, signature) → count, 第 2 次重复警告 + 提醒结果在 context。
# 不缓存返回 (文件可能改了) — 仅警告引导。
_search_tracker: dict[tuple[str, str, str], int] = {}
_SEARCH_REPEAT_WARN_THRESHOLD = 2


def _search_repeat_check(tool_name: str, ws_dir: str, signature: str, result) -> None:
    """检测 search 类工具重复调用, 在 result dict 加警告。signature = pattern 或 path|pattern."""
    if not isinstance(result, dict) or not signature:
        return
    key = (tool_name, ws_dir or "", signature[:120])
    _search_tracker[key] = _search_tracker.get(key, 0) + 1
    # P116: 写入后做轻量 GC
    _maybe_gc_tracker(_search_tracker)
    count = _search_tracker[key]
    if count < _SEARCH_REPEAT_WARN_THRESHOLD:
        return
    result["_redundant_search_warning"] = (
        f"This {tool_name} search has been repeated {count} times (signature: {signature[:80]}). "
        "Reuse the previous result already in context unless the files changed; if searching again, use a narrower pattern.\n\n"
        "搜索重复；优先复用已有结果，必要时缩小 pattern。"
    )
    result["_search_repeat_count"] = count



async def _handle_code_index(workspace_dir: str, args: dict) -> str:
    """2026-05-02 part14:code_index 工具 dispatcher。
    part16 加 name_filter / kinds 参数。"""
    if not workspace_dir:
        return json.dumps({"ok": False, "error": "workspace not available (easy path)"})
    path = str(args.get("path", "")).strip()
    include_includes = bool(args.get("include_includes", True))
    name_filter = args.get("name_filter")
    if name_filter is not None:
        name_filter = str(name_filter).strip() or None
    kinds = args.get("kinds")
    if kinds is not None:
        if isinstance(kinds, str):
            kinds = [kinds]
        kinds = [str(k).strip() for k in kinds if str(k).strip()]
        if not kinds:
            kinds = None
    result = await ws_tool.handle_code_index(
        workspace_dir, path,
        include_includes=include_includes,
        name_filter=name_filter,
        kinds=kinds,
    )
    if not result.get("ok") and "_delegate_" not in str(workspace_dir or ""):
        hint = _workspace_project_path_hint(path, "code_index")
        if hint is not None:
            try:
                from app.llm.tools import environment as env_tool

                redirected = env_tool._handle_read({
                    "path": hint.get("_project_path") or path,
                    "start_line": 1,
                    "end_line": 240,
                    "max_chars": 30000,
                })
                if isinstance(redirected, dict) and redirected.get("ok"):
                    redirected = dict(redirected)
                    redirected["_redirected_from"] = "code_index"
                    redirected["_original_workspace_path"] = path
                    redirected["_next_action_instruction"] = (
                        hint["_next_action_instruction"]
                        + " 本次已自动读取真实项目文件前 240 行；如需符号级索引，请先 env_fetch 到 _env/... 后再 code_index。"
                    )
                    result = redirected
                else:
                    result = hint
            except Exception as exc:
                hint["error"] += f" Automatic env_read retry failed: {type(exc).__name__}: {exc}"
                result = hint
    return json.dumps(result, ensure_ascii=False)


async def _handle_read_function(workspace_dir: str, args: dict) -> str:
    """2026-05-02 part16:read_function 工具 dispatcher。

    2026-05-02 part18 修:trace 9f1c537f 显示 helper 模型(deepseek-v4-pro)有 12 次
    把 `function_name` 简化成 `fn_name` 调用,全部失败 → helper 退回反复 read_file。
    模型可能从 prompt 里 `read_function('rdh.c', 'huff_decode')` 这种简写形式学到
    "fn_name" 的习惯。修:接受 fn_name / name / func_name / func 作为 alias。

    2026-05-02 part20 加:include_xref=False 时附 warning 提示用户关闭了全局视野。
    实测 trace 74769ad9 主线程 8 次调用 read_function 全部 include_xref=false,
    导致看不到 caller/callee 链,数据流分析能力被削弱。
    """
    if not workspace_dir:
        return json.dumps({"ok": False, "error": "workspace not available (easy path)"})
    path = str(args.get("path", "")).strip()
    # 接受多种 alias(模型经常省成 fn_name)
    function_name = str(
        args.get("function_name")
        or args.get("fn_name")
        or args.get("func_name")
        or args.get("func")
        or args.get("name", "")
    ).strip()
    if not path or not function_name:
        # 回报清晰错误,告诉模型实际收到了什么参数
        provided = ", ".join(f"{k}={v!r}"[:60] for k, v in args.items())
        return json.dumps({
            "ok": False,
            "error": (
                f"read_function requires 'path' and 'function_name'. "
                f"Got: {{{provided}}}. "
                f"Note: 'function_name' field name is required (also accepts: "
                f"fn_name / func_name / func / name)."
            ),
        }, ensure_ascii=False)
    include_xref = bool(args.get("include_xref", True))
    # 2026-05-02 part22 微调:之前漏传 xref_scope,模型传 "workspace" 也被忽略
    xref_scope = str(args.get("xref_scope", "file")).strip().lower()
    if xref_scope not in ("file", "workspace"):
        xref_scope = "file"
    result = await ws_tool.handle_read_function(
        workspace_dir, path, function_name,
        include_xref=include_xref,
        xref_scope=xref_scope,
    )
    # part20:模型主动关 xref 时附 warning(实测高频反模式)
    if not include_xref and isinstance(result, dict) and result.get("ok"):
        result["_warning"] = (
            "include_xref=false disables caller/callee context. "
            "For debugging or interface reasoning, keep xref enabled unless you only need the local function body. "
            "Omit this parameter next time to use the default global call context.\n\n"
            "关闭 xref 会丢失调用关系；调试时默认保留。"
        )
    return json.dumps(result, ensure_ascii=False)


async def _handle_search_across_files(workspace_dir: str, args: dict) -> str:
    """2026-05-02 part16:search_across_files 工具 dispatcher。"""
    if not workspace_dir:
        return json.dumps({"ok": False, "error": "workspace not available (easy path)"})
    pattern = str(args.get("pattern", ""))
    file_glob = str(args.get("file_glob", "*")) or "*"
    is_regex = bool(args.get("is_regex", False))
    max_results_per_file = int(args.get("max_results_per_file", 5) or 5)
    max_files = int(args.get("max_files", 30) or 30)
    result = await ws_tool.handle_search_across_files(
        workspace_dir, pattern,
        file_glob=file_glob,
        is_regex=is_regex,
        max_results_per_file=max_results_per_file,
        max_files=max_files,
    )
    # 2026-05-15 P114: search 重复检测
    _search_repeat_check("search_across_files", workspace_dir,
                         f"{pattern}|{file_glob or '*'}", result)
    return json.dumps(result, ensure_ascii=False)


def _tool_out_summary(name: str, result: str) -> str:
    """Short summary of tool result for console display."""
    try:
        r = json.loads(result)
    except (json.JSONDecodeError, ValueError):
        return f"({len(result)} chars)"
    if isinstance(r, dict):
        ok = r.get("ok")
        err = r.get("error", "")
        if err:
            return f"error: {str(err)[:100]}"
        if ok is True:
            if name == "workspace":
                action = r.get("action", "")
                if action == "write":
                    return f"ok: wrote {r.get('path', '?')} ({r.get('size', 0)} bytes)"
                elif action == "run":
                    rc = r.get("returncode", "?")
                    out = r.get("stdout", "")
                    return f"rc={rc} stdout={len(out)} chars"
                elif action == "mkdir":
                    return f"ok: mkdir {r.get('path', '?')}"
            if name == "delegate":
                action = str(r.get("action") or "").strip().lower()
                if action == "kill" or r.get("killed_proc_id") or r.get("kill_reason"):
                    task_id = r.get("task_id") or r.get("killed_task_id") or "?"
                    proc_id = r.get("killed_proc_id") or "?"
                    mode = r.get("mode") or r.get("kill_mode") or "killed"
                    return f"ok: killed helper {task_id} ({proc_id}, {mode})"
                if action == "spawn_async":
                    spawned = r.get("spawned") or []
                    return f"ok: spawned {len(spawned)} helper(s) in background"
                if action == "poll":
                    polled = r.get("polled") or []
                    counts = {}
                    for item in polled:
                        if isinstance(item, dict):
                            status = str(item.get("status") or "unknown")
                            counts[status] = counts.get(status, 0) + 1
                    if counts:
                        bits = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                        return f"ok: poll {len(polled)} helper(s) ({bits})"
                    return f"ok: poll {len(polled)} helper(s)"
                if action == "collect":
                    requested = r.get("helpers_requested", 0)
                    returned = r.get("helpers_completed", 0)
                    success = r.get("success_count", 0)
                    running = r.get("helpers_still_running", 0)
                    unavailable = r.get("helpers_unavailable", 0)
                    suffix = []
                    suffix.append(f"success={success}")
                    if running:
                        suffix.append(f"{running} still running")
                    if unavailable:
                        suffix.append(f"{unavailable} unavailable")
                    extra = f" ({', '.join(suffix)})" if suffix else ""
                    return f"ok: collect {returned}/{requested} helper result(s) returned{extra}"
                if action == "wait_any":
                    winner = r.get("winner_task_id")
                    if winner:
                        return f"ok: wait_any winner={winner}"
                    polled = r.get("polled") or []
                    return f"ok: wait_any timeout; polled {len(polled)} helper(s)"
                if action == "status":
                    summary = r.get("summary") or {}
                    running = summary.get("running", 0)
                    done_ok = summary.get("done_ok", 0)
                    done_failed = summary.get("done_failed", 0)
                    stuck = summary.get("stuck", 0)
                    return (
                        f"ok: status running={running}, stuck={stuck}, "
                        f"done_ok={done_ok}, done_failed={done_failed}"
                    )
                # spawn 结果摘要。delegate 返回的是 helpers_initially_spawned
                # + helpers_forked_during_run + helpers_still_running。
                returned = r.get("helpers_completed", 0)
                success = int(r.get("success_count") or 0)
                running = r.get("helpers_still_running", 0)
                task_ok = r.get("task_ok")
                incomplete = int(r.get("incomplete_count") or 0)
                resource_required = int(r.get("resource_required_count") or 0)
                failed = int(r.get("failed_count") or 0)
                total = returned + running
                if total <= 0:
                    # fallback:回看 initially_spawned + forked
                    total = (r.get("helpers_initially_spawned", 0)
                             + r.get("helpers_forked_during_run", 0))
                if task_ok is False or incomplete or resource_required or failed:
                    reasons = []
                    if incomplete:
                        reasons.append(f"incomplete={incomplete}")
                    if resource_required:
                        reasons.append(f"resource_required={resource_required}")
                    if failed:
                        reasons.append(f"failed={failed}")
                    suffix = f" ({', '.join(reasons)})" if reasons else ""
                    return f"blocked: returned_results={returned}/{total}, success={success}{suffix}"
                # 2026-05-08: 去重/已完成拦截时显示原因
                if r.get("already_completed"):
                    dup_ids = r.get("duplicate_task_ids", [])
                    return f"ok: returned_results={returned}/{total}, success={success} (already completed: {dup_ids})"
                if r.get("duplicate_task_ids"):
                    dup_ids = r.get("duplicate_task_ids", [])
                    if running > 0:
                        return f"ok: returned_results={returned}/{total}, success={success} ({running} still running, dup blocked: {dup_ids})"
                    return f"ok: returned_results={returned}/{total}, success={success} (dup blocked: {dup_ids})"
                if running > 0:
                    return f"ok: returned_results={returned}/{total}, success={success} ({running} still running)"
                return f"ok: returned_results={returned}/{total}, success={success}"
            if name == "office":
                action = r.get("action", "")
                p = r.get("path", "") or ""
                ext = (p.rsplit(".", 1)[-1].lower() if "." in p else "?")
                if action == "read":
                    if ext == "docx":
                        return (f"office read .docx {r.get('paragraph_count',0)}p "
                                f"{r.get('table_count',0)}t {r.get('image_count',0)}img")
                    if ext == "pptx":
                        slides = r.get("slides") or []
                        n_img = sum((s or {}).get("image_count", 0) for s in slides)
                        return (f"office read .pptx {r.get('slide_count',0)}s "
                                f"{n_img}img")
                    if ext in ("xlsx", "xlsm"):
                        return f"office read .{ext} {r.get('sheet_count',0)}sh"
                if action in ("write", "append"):
                    n = (r.get("blocks_written") or r.get("blocks_appended")
                         or r.get("slides_written") or r.get("slides_appended")
                         or r.get("sheets_written") or 0)
                    return f"office {action} {p} (.{ext}, {n} items)"
                if action == "extract_images":
                    return (f"office extracted {r.get('count', 0)} images → "
                            f"{r.get('out_dir', '?')}")
                if action == "insert_image":
                    return f"office insert_image {p} ok"
                return f"office {action}"
            return "ok"
        elif ok is False:
            if name == "request_resource" or r.get("requires_main_resource"):
                kind = r.get("resource_kind") or r.get("suggested_helper_kind") or "resource"
                needed = r.get("needed_outputs") or []
                suffix = ""
                if needed:
                    suffix = f" needed={','.join(str(x) for x in needed[:3])}"
                return f"frozen: requested {kind} resource{suffix}"
            return f"FAIL: rc={r.get('returncode', '?')}"
        return f"result: {len(result)} chars"
    return f"({len(result)} chars)"


def _tool_in_summary(name: str, args: dict) -> str:
    """Generate a short one-line description of a tool call for console display."""
    if name == "workspace":
        action = args.get("action", "?")
        path = args.get("path", "")
        cmd = args.get("command", "")
        if action == "write":
            return f"write {path or '?'} ({len(args.get('content', ''))} chars)"
        elif action == "run":
            return f"run {cmd[:120]}"
        elif action == "mkdir":
            return f"mkdir {path}"
        return f"workspace {action}"
    if name == "python":
        code = args.get("code", "")
        return f"python ({len(code)} chars)"
    if name == "expand_warm":
        ids = args.get("ids", [])
        return f"expand_warm {len(ids)} ids"
    if name == "expand_cold":
        ids = args.get("ids", [])
        depth = args.get("depth", 1)
        return f"expand_cold {len(ids)} ids depth={depth}"
    if name == "expand_kb":
        ids = args.get("ids", [])
        depth = args.get("depth", 1)
        return f"expand_kb {len(ids)} ids depth={depth}"
    if name == "mark_avoid_mention":
        topics = args.get("topics", [])
        return f"mark_avoid {len(topics)} topics"
    if name == "search_files":
        return f"search_files {args.get('query', '?')}"
    if name in {"fetch_indexed_file", "fetch_group_file"}:
        return f"fetch_indexed_file {args.get('kb_node_id', '?')}"
    if name == "delegate":
        action = str(args.get("action") or "spawn")
        tasks = args.get("tasks", [])
        if action in ("poll", "collect", "wait_any", "kill", "status"):
            task_ids = args.get("task_ids") or []
            if action == "kill" and not task_ids and args.get("task_id"):
                task_ids = [args.get("task_id")]
            if action == "status":
                return "delegate status"
            return f"delegate {action} {len(task_ids)} task_ids: {task_ids}"
        return f"delegate {action} {len(tasks)} tasks: {[t.get('task_id','?') for t in tasks]}"
    if name == "office":
        action = args.get("action", "?")
        path = args.get("path", "?")
        ext = (path.rsplit(".", 1)[-1] if "." in path else "").lower()
        if action in ("write", "append"):
            count = 0
            for k in ("blocks", "slides", "sheets"):
                v = args.get(k)
                if isinstance(v, list):
                    count = len(v)
                    break
            return f"office {action} {path} (.{ext}, {count} items)"
        if action == "insert_image":
            return f"office insert_image {path} ← {args.get('image_path', '?')}"
        if action == "extract_images":
            return f"office extract_images {path} → {args.get('out_dir', '<auto>')}"
        return f"office {action} {path}"
    return f"{name}"


def _try_parse(s: str):
    if not isinstance(s, str):
        return s
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return s


async def _handle_python(args: dict) -> str:
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        return json.dumps({"ok": False, "error": "missing or empty 'code'"})
    if len(code) > 20000:
        return json.dumps({"ok": False, "error": "code too long (>20000 chars)"})
    result = await run_python(code)
    if isinstance(result, dict) and result.get("ok") is False:
        error = str(result.get("error", ""))
        if "SecurityError" in error:
            result.setdefault("recovery_action", "switch_tool")
            result.setdefault("retry_same_tool", False)
            result.setdefault(
                "recommended_tools",
                ["read_file", "workspace(action='write')", "workspace(action='run')"],
            )
            if "open" in error:
                result.setdefault(
                    "fix_hint",
                    "tool=python is an isolated calculation sandbox and cannot use open() for workspace files. Use read_file for reads, workspace(action='write', path='...', content='...') for text artifact writes, or write a script into the workspace and run it with workspace(action='run', command='python script.py') for file IO.\npython 工具只适合隔离计算；涉及工作区文件读写时切换到 read_file 或 workspace 工具。",
                )
            elif any(name in error for name in ("compile", "exec", "eval")):
                result.setdefault(
                    "fix_hint",
                    "tool=python blocks dynamic code execution primitives such as compile(), exec(), and eval(). For generated scripts, write the script into the workspace and run it with workspace(action='run'); for direct calculations, provide ordinary sandbox code without dynamic execution.\npython 沙箱不执行动态代码；需要生成脚本时写入工作区后用 workspace.run 运行。",
                )
            else:
                result.setdefault(
                    "fix_hint",
                    "tool=python is an isolated calculation sandbox with restricted names and attributes. If the task needs workspace files, external scripts, dynamic execution, or system interaction, switch to read_file or workspace(action='write'/'run') and continue the same goal.\npython 沙箱受限；需要文件、脚本或系统交互时切换到 read_file 或 workspace 工具。",
                )
        if "ModuleNotFoundError" in error:
            result.setdefault("recovery_action", "switch_tool")
            result.setdefault("retry_same_tool", False)
            result.setdefault("recommended_tools", ["workspace(action='run')"])
            result.setdefault(
                "fix_hint",
                "tool=python is an isolated calculation sandbox with a limited module set and no direct workspace file access. For third-party libraries or file IO, write a script into the workspace and run it with workspace(action='run', command='python script.py').\npython 沙箱模块有限；需要第三方库或文件 IO 时写工作区脚本并用 workspace.run 执行。",
            )
    return json.dumps(result, ensure_ascii=False)


async def _handle_expand_warm(archive_id: str, args: dict) -> str:
    ids = args.get("ids") or []
    if not isinstance(ids, list):
        return json.dumps({"error": "ids must be a list"})
    ids = [str(x) for x in ids][:32]
    items = await warm_mem.expand_warm(archive_id, ids)
    return json.dumps({"items": items}, ensure_ascii=False)


async def _handle_expand_cold(archive_id: str, user_id: str, args: dict) -> str:
    ids = args.get("ids") or []
    depth = int(args.get("depth", 1) or 1)
    depth = max(1, min(depth, 2))
    ids = [str(x) for x in ids][:32]
    items = await cold_mem.expand_cold(
        archive_id, ids, depth, viewer_user_id=user_id,
    )
    return json.dumps({"items": items}, ensure_ascii=False)


async def _handle_expand_kb(archive_id: str, user_id: str, args: dict) -> str:
    ids = args.get("ids") or []
    depth = int(args.get("depth", 1) or 1)
    depth = max(1, min(depth, 2))
    ids = [str(x) for x in ids][:32]
    items = await kb_mem.expand_kb(
        archive_id, ids, depth, viewer_user_id=user_id,
    )
    return json.dumps({"items": items}, ensure_ascii=False)


async def _handle_mark_avoid(
    archive_id: str, group_id: str, user_id: str, args: dict,
) -> str:
    """
    异步触发 LLM 匹配 + 标记，立即返回。本轮回应不依赖标记完成——
    模型应已在 plan.avoid 中处理。
    """
    import asyncio
    topics = args.get("topics") or []
    reason = str(args.get("reason", ""))[:300]
    if not isinstance(topics, list) or not topics:
        return json.dumps({"error": "topics must be a non-empty list"})
    topics = [str(t)[:200] for t in topics if str(t).strip()][:10]
    if not topics:
        return json.dumps({"error": "topics empty after sanitize"})

    from app.core.bg_tasks import schedule
    schedule(
        cold_mem.apply_avoid_mention(
            archive_id=archive_id, group_id=group_id, user_id=user_id,
            topics=topics, reason=reason,
        ),
        name="avoid_mention",
    )
    return json.dumps({
        "ok": True,
        "message": (
            "The avoid-mention request was recorded. Background maintenance is marking related memory nodes "
            "as not proactively mentioned; nodes are retained, not deleted. Include the topic in plan.avoid for this round.\n"
            "已记录不主动提及请求，记忆节点保留但降低主动提及。"
        ),
    }, ensure_ascii=False)


async def _handle_fetch_group_file(archive_id: str, group_id: str, workspace_dir: str, args: dict) -> str:
    kb_node_id = str(args.get("kb_node_id", "")).strip()
    if not kb_node_id:
        return json.dumps({"ok": False, "error": "kb_node_id is required"}, ensure_ascii=False)
    result = await gf_mem.fetch_group_file(kb_node_id, archive_id, group_id, workspace_dir)
    return json.dumps(result, ensure_ascii=False)


async def _handle_search_files(archive_id: str, group_id: str, workspace_dir: str, args: dict) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return json.dumps({"error": "query is required"})
    limit = min(int(args.get("limit", 10) or 10), 50)
    items: list[dict] = []
    kb_error: str | None = None
    try:
        items = await kb_mem.search_files(archive_id, group_id, query, limit=limit)
    except RuntimeError as e:
        kb_error = str(e)
    for it in items:
        fname = it.get("filename", "")
        aid = it.get("archive_id", "")
        gid = it.get("group_id", "")
        if fname and aid and gid:
            it["url"] = f"/v1/chat/files/{aid}/{gid}/{fname}"

    workspace_matches: list[dict] = []
    if workspace_dir:
        try:
            locate_queries = [query]
            parts = [p.strip() for p in query.replace(",", " ").split() if p.strip()]
            file_like_parts = [p for p in parts if "." in p or "_" in p]
            for part in file_like_parts:
                if part not in locate_queries:
                    locate_queries.append(part)
            seen_paths: set[str] = set()
            for locate_query in locate_queries[:12]:
                locate = await ws_tool.handle_locate(workspace_dir, locate_query)
                if not (locate.get("ok") and isinstance(locate.get("matches"), list)):
                    continue
                for m in locate["matches"]:
                    if not isinstance(m, dict):
                        continue
                    path = str(m.get("path") or "")
                    if not path or path in seen_paths:
                        continue
                    seen_paths.add(path)
                    workspace_matches.append({
                        "filename": os.path.basename(path.replace("\\", "/")),
                        "workspace_path": path,
                        "source": "workspace",
                        "layer": m.get("layer") or m.get("source") or "workspace",
                        "size": m.get("size"),
                        "matched_query": locate_query,
                    })
        except Exception as e:
            debug.log("search_files.workspace_locate_error", f"{type(e).__name__}: {e}")

    payload = {
        "items": items,
        "count": len(items),
        "workspace_matches": workspace_matches[:limit],
        "workspace_count": len(workspace_matches),
    }
    if kb_error:
        payload["kb_unavailable"] = True
        payload["kb_error"] = kb_error
    # 2026-05-15 P114: search_files 重复检测 (按 query)
    _search_repeat_check("search_files", workspace_dir, query, payload)
    return json.dumps(payload, ensure_ascii=False)


# ── 2026-05-04 Claude Code 移植:ask_user_question handler ─────
async def _handle_ask_user_question(
    args: dict,
    *,
    archive_id: str,
    group_id: str,
    user_id: str,
) -> str:
    """helper 主动向主线程提问。返回问题文本,主线程/用户作答后注入下一轮。"""
    question = str(args.get("question", "")).strip()
    options = args.get("options") or []
    if isinstance(options, str):
        options = [options]
    # 2026-05-11 F: 接受可选 context 字段(为什么犹豫)
    context_str = str(args.get("context", "")).strip() or None

    result = {
        "status": "asked",
        "question": question,
        "options": options,
        "note": (
            "The question was recorded. In interactive mode, the user can answer; otherwise the main process "
            "will make the best available decision from context. Wait for the next round for the answer.\n"
            "问题已记录，等待用户或主进程在下一轮提供答案。"
        ),
    }
    if context_str:
        result["context"] = context_str
    return json.dumps(result, ensure_ascii=False)


# ── 2026-05-09 Patch 22: inspect_file handler ────────────────
async def _handle_inspect_file(workspace_dir: str, args: dict) -> str:
    """主线程验证二进制产物。委托到 tool_workspace.inspect_file。

    实现是纯 ZIP/header 解析(无 subprocess、无用户代码执行),所以不需要
    Semaphore / to_thread 包裹。但解析 ZIP 在大文件上会有 IO,微小阻塞 ok。
    """
    from app.llm.tools.workspace import inspect_file as _inspect_impl

    path = str(args.get("path", "")).strip()
    if not path:
        return json.dumps({
            "ok": False,
            "error": "Missing path. Provide a workspace-relative file path.\n缺少工作区相对 path 参数。",
            "hint": "Example: inspect_file(path='paper.docx').\n示例：inspect_file(path='paper.docx')。",
        }, ensure_ascii=False)
    inventory_guard = _inventory_helper_read_guard(path, tool_name="inspect_file")
    if inventory_guard is not None:
        return json.dumps(inventory_guard, ensure_ascii=False)

    try:
        result = _inspect_impl(workspace_dir, path)
    except Exception as e:
        log.exception("inspect_file failed: %s", e)
        return json.dumps({
            "ok": False,
            "error": f"inspect_file internal error: {e}.\ninspect_file 内部异常。",
        }, ensure_ascii=False)

    if not result.get("ok") and "_delegate_" not in str(workspace_dir or ""):
        staged_retry = _try_env_fetch_then_inspect(workspace_dir, path, _inspect_impl)
        if staged_retry is not None:
            return json.dumps(staged_retry, ensure_ascii=False)

    if not result.get("ok") and "_delegate_" not in str(workspace_dir or ""):
        hint = _workspace_project_path_hint(path, "inspect_file")
        if hint is not None:
            try:
                from app.llm.tools import environment as env_tool

                redirected = env_tool._handle_read({
                    "path": hint.get("_project_path") or path,
                    "start_line": 1,
                    "end_line": 240,
                    "max_chars": 30000,
                })
                if isinstance(redirected, dict) and redirected.get("ok"):
                    redirected = dict(redirected)
                    redirected["_redirected_from"] = "inspect_file"
                    redirected["_original_workspace_path"] = path
                    redirected["_next_action_instruction"] = (
                        "inspect_file is for chat-workspace artifacts. This project text file was redirected "
                        "to env_read so the answer can proceed from real evidence. Use env_fetch first if the "
                        "project file must be edited as an _env/... workspace copy.\n\n"
                        "inspect_file 用于工作区产物；项目文本文件本次已转为 env_read，需要编辑时先 env_fetch。"
                    )
                    result = redirected
                else:
                    result = hint
            except Exception as exc:
                hint["error"] += f" Automatic env_read retry failed: {type(exc).__name__}: {exc}"
                result = hint

    return json.dumps(result, ensure_ascii=False)


def _try_env_fetch_then_inspect(
    workspace_dir: str,
    path: str,
    inspect_impl: Callable[[str, str], dict],
) -> dict | None:
    """Auto-stage a missing `_env/...` project file for read-only inspection."""
    norm = str(path or "").replace("\\", "/").strip().strip("`\"'").lstrip("./")
    if not norm.startswith("_env/"):
        return None
    try:
        from app.core.runtime_mode import is_environment_mode
        from app.llm.tools import environment as env_tool

        if not is_environment_mode():
            return None
        project_path = norm[len("_env/"):].lstrip("/")
        if not project_path:
            return None
        try:
            env_tool._resolve_env_path(project_path, must_exist=True)
        except Exception:
            return None
        fetch_result = env_tool._handle_fetch(workspace_dir, {"path": project_path})
        if not isinstance(fetch_result, dict) or not fetch_result.get("ok"):
            if isinstance(fetch_result, dict) and fetch_result.get("error"):
                out = dict(fetch_result)
                out["_redirected_from"] = "inspect_file_auto_env_fetch"
                out["_original_workspace_path"] = path
                out["_next_action_instruction"] = (
                    "inspect_file tried to auto-stage this missing _env path from the real project, "
                    "but env_fetch could not copy it. Follow next_action/suggested_actions instead of "
                    "retrying inspect_file on the missing staged path.\n\n"
                    "自动暂存 _env 文件失败；按建议动作处理，不要重复 inspect_file。"
                )
                return out
            return None
        workspace_path = str(fetch_result.get("workspace_path") or norm)
        inspected = inspect_impl(workspace_dir, workspace_path)
        if isinstance(inspected, dict) and inspected.get("ok"):
            inspected = dict(inspected)
            inspected["_redirected_from"] = "inspect_file_auto_env_fetch"
            inspected["_original_workspace_path"] = path
            inspected["_env_fetch"] = {
                "path": fetch_result.get("path"),
                "workspace_path": fetch_result.get("workspace_path"),
                "sha256": fetch_result.get("sha256"),
                "size": fetch_result.get("size"),
            }
            inspected["_next_action_instruction"] = (
                "The missing staged _env file was copied from the real project for this read-only inspection. "
                "Use the returned workspace_path for further workspace reads. Intentional edits still require "
                "env_diff/env_apply with the latest fetch hash.\n\n"
                "_env 文件已从真实项目暂存；后续读取用返回路径，写入仍走差异流程。"
            )
            return inspected
        return None
    except Exception as exc:
        return {
            "ok": False,
            "error": f"inspect_file could not auto-stage {path!r}: {type(exc).__name__}: {exc}",
            "_redirected_from": "inspect_file_auto_env_fetch",
        }


# ── OCR handler ───────────────────────────────────────────────
async def _handle_ocr(workspace_dir: str, args: dict) -> str:
    """离线 OCR 图片文字识别 handler。"""
    from app.llm.tools.ocr_bridge import ocr_file_tiered, ocr_base64

    image_path = str(args.get("image_path", "")).strip()
    image_base64 = str(args.get("image_base64", "")).strip()
    tier = str(args.get("tier", "fast") or "fast").strip().lower()
    if tier not in {"fast", "balanced", "accurate"}:
        tier = "fast"
    max_tier = str(args.get("max_tier", "accurate") or "accurate").strip().lower()
    if max_tier not in {"fast", "balanced", "accurate"}:
        max_tier = "accurate"
    allow_upgrade = bool(args.get("allow_upgrade", False))
    return_raw = bool(args.get("return_raw", False))
    save_to = str(args.get("save_to", "")).strip()
    purpose = str(
        args.get("purpose")
        or args.get("target")
        or args.get("goal")
        or args.get("query")
        or ""
    ).strip()

    if image_base64 and not image_path:
        compact_b64 = image_base64.replace("\n", "").replace(" ", "")
        looks_like_path = (
            len(compact_b64) < 512
            and any(sep in compact_b64 for sep in ("/", "\\", "."))
            and compact_b64.lower().split("?", 1)[0].endswith((
                ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".pdf", ".docx", ".pptx", ".xlsx"
            ))
        )
        if looks_like_path:
            return json.dumps({
                "ok": False,
                "error": (
                    "image_base64 appears to contain a file path, not base64 image bytes. "
                    "Use image_path for workspace files.\n\n"
                    "image_base64 只放图片字节编码；文件路径请改用 image_path。"
                ),
                "suggested_args": {"image_path": compact_b64, "image_base64": ""},
            }, ensure_ascii=False)

    if not image_path and not image_base64:
        return json.dumps({
            "ok": False,
            "error": (
                "OCR needs either image_path for a workspace file or image_base64 for inline image bytes.\n\n"
                "OCR 需要工作区文件路径 image_path，或图片字节编码 image_base64。"
            ),
        }, ensure_ascii=False)

    if image_path:
        try:
            target = ws_tool._safe_resolve(workspace_dir, image_path)
        except ValueError as e:
            return json.dumps({
                "ok": False,
                "error": (
                    f"Invalid OCR path: {e}. OCR image_path must be inside the current workspace. "
                    "For project files, the main thread should env_fetch the project-relative file first, "
                    "then pass the staged `_env/...` path.\n\n"
                    "OCR 路径必须在当前工作区内；项目文件先由主线程 env_fetch 到 _env。"
                ),
                "expected_path_kind": "workspace_relative",
            }, ensure_ascii=False)
        if not os.path.isfile(target):
            # Missing visual source: list available media and explain the generic
            # staged-workspace / upload / project-fetch possibilities.
            _media_dir = os.path.join(workspace_dir, "_downloaded_media")
            _existing = []
            try:
                if os.path.isdir(_media_dir):
                    for _f in sorted(os.listdir(_media_dir)):
                        if _f.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")):
                            try:
                                _mtime = os.path.getmtime(os.path.join(_media_dir, _f))
                                _age = time.time() - _mtime
                                _existing.append({
                                    "name": _f,
                                    "age_sec": int(_age),
                                })
                            except OSError:
                                pass
            except OSError:
                pass
            _hint = (
                f"OCR source file is not present in this workspace: {image_path}\n"
                f"Available saved visual inputs in _downloaded_media/: {len(_existing)}\n"
                "Next action:\n"
                "- If this is an uploaded chat file, inspect the current workspace file/media lists and use the exact saved relative path.\n"
                "- If this is a project file in bot project mode, the main thread should env_fetch the exact project-relative file, then resume the helper with the resulting `_env/...` path.\n"
                "- If the source is still being saved or the path is ambiguous, report the missing resource instead of guessing.\n\n"
                "OCR 缺少工作区实体文件；上传文件查实际保存路径，项目文件由主线程 env_fetch 后再用 _env 路径。"
            )
            return json.dumps({
                "ok": False,
                "error": _hint,
                "existing_media": _existing[:10],  # 最多 10 张参考
                "file_not_found": True,  # 给主线程明确信号
                "missing_path": image_path,
                "expected_path_kind": "workspace_relative",
                "environment_project_hint": (
                    "Main thread should env_fetch the project-relative path, then resume the helper with the resulting `_env/...` resource.\n\n"
                    "项目文件由主进程先 env_fetch，再用 `_env/...` 资源恢复 helper。"
                ),
                "suggested_resource_action": "request_resource_or_main_env_fetch",
            }, ensure_ascii=False)
        suffix = os.path.splitext(target)[1].lower()
        if suffix in {
            ".py", ".pyw", ".js", ".ts", ".tsx", ".jsx", ".c", ".h", ".hpp", ".cpp", ".cc",
            ".java", ".go", ".rs", ".php", ".rb", ".cs", ".swift", ".kt", ".m", ".mm",
            ".md", ".txt", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".toml",
            ".ini", ".cfg", ".log", ".xml", ".html", ".css", ".scss", ".sql",
        }:
            return json.dumps({
                "ok": False,
                "error": (
                    f"OCR is for visual/document containers, but {image_path!r} is a plain text or source file. "
                    "Use read_file with line ranges, search_in_file, code_index, or read_function instead.\n\n"
                    "OCR 用于图片/PDF/Office 等视觉或文档容器；纯文本和源码请用 read_file/search/code_index。"
                ),
                "wrong_tool": "ocr_for_plain_text",
                "suggested_tools": ["read_file", "search_in_file", "code_index", "read_function"],
                "path": image_path,
            }, ensure_ascii=False)
        # 2026-05-09 BUG FIX: ocr_file 内部 subprocess.run 阻塞最长 120s。
        # 直接 await 调用会冻结整个 event loop,所有并发 user/helper/HTTP 请求都暂停。
        # 包 to_thread 让阻塞落到 threadpool。
        # 2026-05-09 Patch 15: Semaphore 限并发 → 防 OOM。超过 2 个会排队。
        r = await run_gpu_ocr(ocr_file_tiered, target, tier=tier, allow_upgrade=allow_upgrade, max_tier=max_tier)
    else:
        r = await run_gpu_ocr(ocr_base64, image_base64)

    # 2026-05-17 P164: OCR 数学完整性后检
    # 病因(实测 trace 23:14 高数试卷): OCR 返回 score=1.0 但数学符号大量丢失,
    # ∫/lim/求和/矩阵都变成 "..." 或单字符残片。主线程看到 score=1.0 以为成功,
    # 把损坏 prompt 直接喂给 math helper, 解错题。
    # 修复: 扫描输出文本, 识别"OCR 残缺信号", 命中时降 score + 加 warning。
    math_quality_warning = None
    if r.ok and r.text:
        warning_signals = _detect_ocr_math_damage(r.text)
        if warning_signals:
            math_quality_warning = {
                "warning_kind": "math_content_damaged",
                "signals": warning_signals,
                "advice": (
                    "OCR 可能漏掉数学符号、公式或矩阵。请结合上下文谨慎推断；"
                    "信息不足且返回 next_tier 时，手动升档重试；仍不可读再标注 OCR 不完整。"
                    "OCR text 只代表识别到的文字串，不代表视觉语义；不要把文件名/目录名里的 latex、公式等词扩写成图片实际展示了公式内容。"
                ),
            }

    result_payload = {
        "ok": r.ok,
        "text": r.text,
        "score": r.score,
        **({"tier": r.tier} if getattr(r, "tier", "") else {}),
        **({"engine": r.engine} if getattr(r, "engine", "") else {}),
        **({"engine_config": r.engine_config} if getattr(r, "engine_config", None) else {}),
        **({"elapsed_ms": r.elapsed_ms} if getattr(r, "elapsed_ms", 0) else {}),
        **({"quality_flags": r.quality_flags} if getattr(r, "quality_flags", None) else {}),
        **({"folded_spans": r.folded_spans[:8]} if getattr(r, "folded_spans", None) else {}),
        **({"candidates": r.candidates} if getattr(r, "candidates", None) else {}),
        **({"next_tier": r.next_tier} if getattr(r, "next_tier", "") else {"no_stronger_tier": True}),
        **({"math_quality_warning": math_quality_warning} if math_quality_warning else {}),
        **({} if r.ok else {"error": r.error}),
    }
    if getattr(r, "raw_text_path", ""):
        result_payload["raw_text_len"] = getattr(r, "raw_text_len", 0)
        result_payload.update(_copy_ocr_raw_text_to_workspace(workspace_dir, r.raw_text_path))
    if save_to and r.ok:
        try:
            save_path = ws_tool._safe_resolve(workspace_dir, save_to)
            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(r.text or "")
            lines = (r.text or "").count("\n") + (1 if r.text and not r.text.endswith("\n") else 0)
            result_payload["saved_to"] = save_to
            result_payload["saved_text_bytes"] = len((r.text or "").encode("utf-8"))
            result_payload["saved_text_lines"] = lines
            result_payload["text_preview"] = (r.text or "")[:600]
            result_payload.pop("text", None)
            result_payload.pop("raw_text_path", None)
            result_payload["hint"] = (
                f"OCR text was saved to {save_to}. Use read_file with start_line/end_line "
                "to inspect it in chunks; keep large OCR evidence paged across tool results.\n\n"
                "OCR 文本已保存；用行号分段读取，避免大文本反复进入上下文。"
            )
        except (OSError, ValueError) as e:
            result_payload["save_to_error"] = f"{type(e).__name__}: {e}"
    purpose_quality = _ocr_purpose_quality(r.text or "", purpose)
    if purpose_quality:
        flags = list(result_payload.get("quality_flags") or [])
        flags.append(purpose_quality)
        result_payload["quality_flags"] = flags
        result_payload["purpose_quality_warning"] = purpose_quality
    elif purpose:
        result_payload["sufficient_for_purpose"] = True
    if return_raw and getattr(r, "raw_text_path", ""):
        try:
            with open(r.raw_text_path, "r", encoding="utf-8", errors="replace") as f:
                result_payload["raw_text_preview"] = f.read(2000)
        except OSError:
            pass
    return json.dumps(result_payload, ensure_ascii=False)


def _ocr_purpose_quality(text: str, purpose: str) -> dict | None:
    """Add task-aware OCR quality hints without deciding the final answer."""
    purpose_l = (purpose or "").lower()
    if not purpose_l:
        return None
    wants_numbers = any(
        token in purpose_l
        for token in ("数字", "数值", "编号", "金额", "分数", "比例", "percent", "number", "value", "score")
    )
    wants_labels = any(
        token in purpose_l
        for token in ("标签", "字段", "表格", "结果", "判定", "pass", "fail", "label", "result", "verdict")
    )
    if not wants_numbers and not wants_labels:
        return None
    stripped = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text or "").strip()
    digits = re.findall(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])", stripped)
    latin_words = re.findall(r"[A-Za-z]{2,}", stripped)
    missing: list[str] = []
    expected_numeric_count = 2
    if re.search(r"(三个|3\s*个|three)\s*(数值|数字|值|numbers?|values?)", purpose_l, re.I):
        expected_numeric_count = 3
        if "编号" in purpose_l or "number" in purpose_l:
            expected_numeric_count = 4
    if wants_numbers and len(digits) < expected_numeric_count:
        missing.append("numbers")
    if wants_labels and not re.search(r"\b(pass|fail|ok|ng|true|false|alpha|beta|gamma|result)\b|结果|通过|失败|判定", stripped, re.I):
        missing.append("labels_or_result")
    if len(stripped) < 40 and (wants_numbers or wants_labels):
        missing.append("very_short_text")
    if not missing:
        return None
    return {
        "signal": "ocr_purpose_incomplete",
        "missing": sorted(set(missing)),
        "text_len": len(stripped),
        "numbers_found": digits[:8],
        "latin_words_found": latin_words[:8],
        "advice": (
            "OCR 文本不足以覆盖本次目标。若工具候选里有补充候选,请合并使用; "
            "若仍缺失,报告具体缺失项,不要把工具档位/引擎细节写进面向用户的回答。"
        ),
    }


def _detect_ocr_math_damage(text: str) -> list[dict]:
    """检测 OCR 输出里的数学符号残缺信号。返回 hits 列表 (空列表 = 看着 OK)。

    P164: 实测表明 PaddleOCR/MinerU 对手写或扫描数学题, 经常把 ∫/lim/Σ/矩阵
    简化为占位符或单字符, 但 OCR 引擎仍报 score=1.0 (因为单字符识别 confidence 高)。
    这里用启发式模式识别 "OCR 残缺"。

    P164 fix (2026-05-18): 中文试卷中 （） 和 =（） 是标准"填空题留空"格式,
    不应被判为 OCR 残缺。区分 ASCII/CJK 空括号, 排除答题空模式。
    """
    hits: list[dict] = []

    # 信号 1: 空括号检测 — 区分三种类型
    #   (a) ASCII () — 几乎可以确定是 OCR 吃掉的内容 (中文文档极少用 () 留空)
    #   (b) CJK （） — 正常中文试卷填空题留空, 不是 OCR 残缺
    #   (c) 混合 (）或 （) — 中英括号混用, 通常是 OCR 吃掉部分内容
    ascii_empty = len(re.findall(r"[(]\s*[)]", text))
    cjk_empty = len(re.findall(r"[（]\s*[）]", text))
    mixed_empty = len(re.findall(r"[(]\s*[）]|[（]\s*[)]", text))
    true_empty = ascii_empty + mixed_empty
    if true_empty >= 2:
        hits.append({
            "signal": "empty_parens",
            "count": true_empty,
            "cjk_blank_parens": cjk_empty,
            "hint": (f"{true_empty} ASCII/mixed empty parenthesis span(s) were found; OCR may have dropped variables or parameters. "
                     f"{cjk_empty} CJK blank answer parenthesis span(s) were excluded as normal answer blanks.\n"
                     f"OCR 可能漏掉空括号中的变量或参数。"
                     if cjk_empty else
                     f"{true_empty} empty parenthesis span(s) were found; OCR may have dropped variables or parameters.\n"
                     f"OCR 可能漏掉空括号中的变量或参数。"),
        })

    # 信号 2: 孤立等号 = 之后立刻是标点/换行 — 但排除 =（）/=() 答题空模式
    #   =（）是中文试卷标准格式 "f(x)=___", 不是 OCR 残缺
    raw_dangling = len(re.findall(r"=\s*[\n，,。；;]", text))
    eq_before_cjk_blank = len(re.findall(r"=\s*[（]\s*[）]", text))
    eq_before_ascii_blank = len(re.findall(r"=\s*[(]\s*[)]", text))
    true_dangling = raw_dangling + eq_before_ascii_blank  # =() is damage, =（） is normal blank
    if true_dangling >= 5:
        hits.append({
            "signal": "dangling_equals",
            "count": true_dangling,
            "cjk_answer_blanks": eq_before_cjk_blank,
            "hint": (f"{true_dangling} equals-sign span(s) have no right-hand content; formula right sides may be missing. "
                     f"{eq_before_cjk_blank} CJK answer blank span(s) were excluded as normal blanks.\n"
                     f"等号右侧可能被 OCR 漏读。"
                     if eq_before_cjk_blank else
                     f"{true_dangling} equals-sign span(s) have no right-hand content; formula right sides may be missing.\n"
                     f"等号右侧可能被 OCR 漏读。"),
        })

    # 信号 3: 频繁省略号 — OCR 对识别不出的区域常用 ... 占位
    ellipsis_count = (text.count("...") + text.count("…")
                      + text.count("..."))
    if ellipsis_count >= 5:
        hits.append({
            "signal": "frequent_ellipsis",
            "count": ellipsis_count,
            "hint": f"{ellipsis_count} ellipsis marker(s) were found; mathematical symbols may be missing.\n省略号过多，可能有数学符号丢失。",
        })

    # 信号 4: "求/设/已知/计算 + 几个字 + 标点/换行" 短句过多
    # 一道完整数学题不会有 "设f(x)=" 然后立即换行 (除非定义函数, 但极少出现)
    truncated_intro = len(re.findall(
        r"(?:求|设|已知|计算|证明)[^，。；,]{0,8}[=:：]\s*[\n，。；,]", text))
    if truncated_intro >= 3:
        hits.append({
            "signal": "truncated_problem_intro",
            "count": truncated_intro,
            "hint": f"{truncated_intro} short problem-introduction fragment(s) were found; the question text may be incomplete.\n题面开头疑似残缺。",
        })

    # 信号 5: 出现"积分/极限/矩阵/导数/求和"等关键词但邻近没有相应符号
    # 例: "二次积分" 后直接换行无公式, "行列式" 后无矩阵内容
    math_keyword_no_content = 0
    for kw in ("二次积分", "行列式", "矩阵", "求极限", "求积分", "求不定积分"):
        for m in re.finditer(kw, text):
            tail = text[m.end():m.end() + 60]
            # 关键词后 60 字符内应该有公式 (含 ∫/Σ/[/{/\\/^/_/分数等)
            # 若一片"干净文字"无任何运算符 → 残缺
            if not re.search(r"[∫∑∏√∂Σ∇⟨⟩∮≤≥≠≈±÷×{\\\\\\[\\(\\^_/]", tail) \
                    and not re.search(r"\d.*\d", tail):  # 也允许纯数字范围
                math_keyword_no_content += 1
    if math_keyword_no_content >= 2:
        hits.append({
            "signal": "math_keyword_no_formula",
            "count": math_keyword_no_content,
            "hint": (
                f"{math_keyword_no_content} math keyword span(s) have no nearby formula content; "
                f"the main problem body may be missing.\n"
                f"数学关键词附近缺公式，题目主体可能未识别。"
            ),
        })

    # 信号 6: 单字符 / 双字符行 占比过高 (OCR 吃掉大块内容后只剩残片)
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(lines) >= 20:
        short_lines = sum(1 for ln in lines if len(ln.strip()) <= 4)
        short_ratio = short_lines / len(lines)
        if short_ratio >= 0.30:
            hits.append({
                "signal": "high_short_line_ratio",
                "count": short_lines,
                "total_lines": len(lines),
                "ratio_pct": round(short_ratio * 100, 1),
                "hint": (
                    f"{short_lines}/{len(lines)} line(s) are at most 4 characters "
                    f"({short_ratio*100:.0f}%); OCR output is highly fragmented.\n"
                    f"OCR 输出高度碎片化。"
                ),
            })

    # 信号 7 (2026-05-18 P177): 单字符 runaway (实测 trace 0518 124610: 矩阵 A 被
    # MinerU 读成 "100000...0" 巨长 0 串, 主线程 prompt 还要手动告诉 helper "正确
    # 矩阵应该是 diag(1,-2,1)"). 检测同一字符连续出现 30+ 次的异常段。
    unique_runaway: dict[str, int] = {}
    run_start = 0
    for idx in range(1, len(text) + 1):
        if idx < len(text) and text[idx] == text[run_start]:
            continue
        length = idx - run_start
        if length >= 30:
            ch = text[run_start]
            unique_runaway[ch] = max(unique_runaway.get(ch, 0), length)
        run_start = idx
    if unique_runaway:
        max_len = max(unique_runaway.values())
        hits.append({
            "signal": "runaway_repeated_chars",
            "count": len(unique_runaway),
            "max_run_length": max_len,
            "characters": list(unique_runaway.keys())[:5],
            "hint": (
                f"Repeated-character runaway detected for {list(unique_runaway.keys())[:3]} "
                f"(maximum run length {max_len}); matrix/vector OCR may be severely distorted.\n"
                f"字符异常长重复，矩阵或向量识别可能严重失真。"
            ),
        })

    # 信号 8 (2026-05-18 P177): OCR 元数据残留 (markdown 图片占位 + HTML table)
    # MinerU 把识别不出的区域用 ![](images/<hash>.jpg) 占位, helper 看到这种
    # 内容会以为它是真实的图片引用, 而实际是 "OCR 没认出来" 的信号
    md_img_placeholders = len(re.findall(r"!\[\]\(images/[a-f0-9]+\.[a-z]+\)", text))
    if md_img_placeholders >= 1:
        hits.append({
            "signal": "ocr_image_placeholder",
            "count": md_img_placeholders,
            "hint": (
                f"OCR output contains {md_img_placeholders} `![](images/...)` placeholder(s). "
                f"Treat them as unrecognized visual regions, not real image references; important content may be missing.\n"
                f"OCR 图片占位表示对应区域未识别。"
            ),
        })

    return hits


# ── TTS handler ───────────────────────────────────────────────
async def _handle_tts(workspace_dir: str, args: dict, *, archive_id: str = "") -> str:
    """离线 TTS 语音合成 handler。
    
    2026-05-16 fix (trace b4c71c63): 之前不传 output 参数 → OmniVoice 自己写到
    F:\\chatbot\\ominvioce\\_tts_out_0000.wav (它自己的目录, **不在 workspace**).
    然后 LLM 把 `_tts_out_0000.wav` 写进 deliverables, workspace.missing 检查
    找不到 → P56 demote → round3 道歉 "语音文件没生成出来" → 自动 voice 流程把
    道歉合成语音发给用户. 用户听到 "测试失败啦" 而不是 "收到，测试没问题".
    
    修复: 强制 output 指向 workspace 内的稳定文件名, 返工作区相对路径.
    """
    from app.llm.tools.tts_bridge import tts_design, tts_clone, tts_auto, is_available

    if not is_available():
        return json.dumps({
            "ok": False,
            "error": "TTS engine is unavailable; the runtime may be missing or the model cache is absent.\nTTS 引擎不可用。",
        }, ensure_ascii=False)

    text = str(args.get("text", "")).strip()
    if not text:
        return json.dumps({"ok": False, "error": "Missing text parameter.\n缺少 text 参数。"}, ensure_ascii=False)

    ignored_voice_args = {
        k: args.get(k)
        for k in ("mode", "instruct", "ref_audio", "ref_text")
        if k in args and str(args.get(k, "")).strip()
    }
    if ignored_voice_args:
        debug.log(
            "tool.tts.voice_args_ignored",
            "LLM-supplied voice parameters ignored; voice is selected only by system persona",
            sorted(ignored_voice_args.keys()),
        )

    persona_voice_ref_audio = current_voice_ref_audio()
    persona_text_for_voice = ""
    if archive_id and not persona_voice_ref_audio:
        try:
            from app.memory.archive import get_persona
            from app.memory.persona_files import find_persona_voice_sample_by_content
            persona_text_for_voice = await get_persona(archive_id)
            _ref = find_persona_voice_sample_by_content(persona_text_for_voice)
            persona_voice_ref_audio = str(_ref) if _ref else ""
        except Exception:
            persona_voice_ref_audio = ""
    if persona_voice_ref_audio:
        mode = "voice_clone"
    persona_voice_instruct = current_voice_instruct(default="")
    if not persona_voice_instruct and archive_id:
        try:
            from app.memory.archive import get_persona
            from app.memory.persona_files import persona_voice_instruct_by_content
            if not persona_text_for_voice:
                persona_text_for_voice = await get_persona(archive_id)
            persona_voice_instruct = persona_voice_instruct_by_content(
                persona_text_for_voice,
                default="",
            )
        except Exception:
            persona_voice_instruct = ""
    if not persona_voice_instruct:
        return json.dumps({
            "ok": False,
            "error": "TTS voice profile is not configured for the active persona",
        }, ensure_ascii=False)
    mode = "voice_clone" if persona_voice_ref_audio else "voice_design"
    instruct = persona_voice_instruct
    language = str(args.get("language", "")).strip() or None
    speed_val = args.get("speed")
    speed = float(speed_val) if speed_val is not None else None
    push = bool(args.get("push", False))

    _guard_ok, _guard_reason = await tts_persona_guard(text, purpose="round2/tool tts")
    debug.log("tool.tts.persona_guard", f"allow={_guard_ok} reason={_guard_reason}")
    if not _guard_ok:
        return json.dumps({
            "ok": False,
            "error": "persona_guard_refused_tts",
            "reason": _guard_reason or "人设拒绝执行这次 TTS",
        }, ensure_ascii=False)

    # 2026-05-16: 自动分配 workspace 内输出路径
    # 用 timestamp 防覆盖 + 让 LLM 看到稳定 filename 当 deliverable
    import time as _t
    _ts = int(_t.time() * 1000)
    _trace_short = (debug.current_trace_id() or "tts")[:8]
    requested_output = str(args.get("output_filename", "")).strip()
    output_filename = ""
    if requested_output:
        try:
            requested_output = requested_output.replace("\\", "/").split("/")[-1]
            ext = os.path.splitext(requested_output)[1].lower()
            if ext not in {".wav", ".mp3", ".m4a", ".ogg"}:
                raise ValueError("unsupported extension")
            output_filename = requested_output
        except Exception:
            return json.dumps({
                "ok": False,
                "error": "output_filename must be a simple audio filename ending with .wav/.mp3/.m4a/.ogg",
            }, ensure_ascii=False)
    if not output_filename:
        output_filename = f"tts_{_trace_short}_{_ts}.wav"
    output_abs_path = os.path.join(workspace_dir, output_filename)

    # 2026-05-18 P171: 确保 workspace 存在 + 把 cwd 传给子进程
    # 病因(trace 2026-05-16 b4c71c63, 2026-05-17 14j): OmniVoice 子进程无 cwd 时继承
    # chatbot 进程 CWD (项目根目录), 即使我们传 output 参数它也常忽略, 写 _tts_out_*.wav
    # 到根目录, 然后 LLM 把这文件名当 deliverable → workspace 检查找不到 → 道歉循环。
    # cwd=workspace_dir 让 OmniVoice 即使写相对路径也落到 workspace, 配合 output 双保险。
    try:
        os.makedirs(workspace_dir, exist_ok=True)
    except OSError as _e:
        return json.dumps({"ok": False, "error": f"Workspace is not writable: {_e}.\nworkspace 不可写。"}, ensure_ascii=False)

    # ── 执行 TTS ──
    try:
        ref_audio_path = None
        if persona_voice_ref_audio:
            ref_audio_path = persona_voice_ref_audio

        if mode == "voice_clone":
            if not ref_audio_path:
                return json.dumps({"ok": False, "error": "voice_clone mode requires ref_audio.\nvoice_clone 需要 ref_audio。"}, ensure_ascii=False)
            r = await run_gpu_tts(
                tts_clone, text, ref_audio=ref_audio_path, ref_text=None,
                language=language, speed=speed, output=output_abs_path,
                cwd=workspace_dir,
            )
        else:
            # voice_design (默认)
            r = await run_gpu_tts(
                tts_design, text, instruct=instruct,
                language=language, speed=speed, output=output_abs_path,
                cwd=workspace_dir,
            )

    except Exception as e:
        return json.dumps({"ok": False, "error": f"TTS error: {e}.\nTTS 异常。"}, ensure_ascii=False)

    if not r.ok:
        return json.dumps({"ok": False, "error": r.error}, ensure_ascii=False)
    
    # 2026-05-16: OmniVoice 即使指定 output 也可能写到自己的目录 (它自己拼了序号忽略我们).
    # 检查实际产物位置, 不在 workspace 就主动 move 进来.
    # 用 shutil.move 而非 copy2 — 跨盘自动 copy+delete; copy2 在 Windows 上某些情况
    # 写完后 voice_path_tmp 状态不可靠 (实测 trace 61167a31).
    final_paths = []
    for idx, src_path in enumerate(r.paths or []):
        if not src_path:
            continue
        # 2026-05-17 Round 14j: OmniVoice 可能返相对路径 (e.g. '_tts_out_0.wav')
        # 尝试 workspace_dir / OmniVoice dir 兜底
        if not os.path.isabs(src_path):
            _candidates = [
                os.path.join(workspace_dir, src_path),
                src_path,  # cwd
            ]
            try:
                from app.config import settings as _s
                _ov_dir = getattr(_s, "tts_omnivoice_dir", None) or \
                          getattr(_s, "omnivoice_dir", None)
                if _ov_dir:
                    _candidates.append(os.path.join(_ov_dir, src_path))
            except Exception:
                pass
            for _c in _candidates:
                if os.path.isfile(_c):
                    src_path = _c
                    break
        
        if not os.path.isfile(src_path):
            debug.log(
                "tool.tts.missing_output",
                f"OmniVoice reported {src_path} but file not found",
            )
            final_paths.append(src_path)  # 给 LLM 但 deliverable 找不到
            continue
        # 期望路径就是 output_abs_path; 实际可能是 OmniVoice 自拼的
        if os.path.abspath(src_path) == os.path.abspath(output_abs_path):
            final_paths.append(output_filename)  # 已在 workspace
            continue
        # OmniVoice 写到了别处 → move 到 workspace (跨盘自动 copy+delete)
        target_name = output_filename if idx == 0 else (
            f"tts_{_trace_short}_{_ts}_{idx}.wav"
        )
        target_abs = os.path.join(workspace_dir, target_name)
        try:
            import shutil
            shutil.move(src_path, target_abs)
            final_paths.append(target_name)
        except OSError as _me:
            debug.log(
                "tool.tts.move_failed",
                f"src={src_path} → {target_abs}: {_me}",
            )
            # move 失败 → 用 absolute path (但 deliverable 还是会找不到)
            final_paths.append(src_path)

    result = {
        "ok": True,
        "paths": final_paths,
        "durations": r.durations,
    }

    if push:
        result["push_supported"] = False
        result["push_note"] = (
            "push=true 当前未实现:文件已生成在 paths 列出的路径,但**不会自动发出**。"
            "若希望让用户听到语音,有三种正确做法:"
            "(1) 让你的最终回复保持口语化短句,自动语音决策器(decide_voice)会判定后合成并发送;"
            "(2) 若这次生成的音频就是本轮最终回复,在 ResponsePlan 写 voice_reply_file 指向该 wav,不要写入 deliverables;"
            "(3) 把生成的 .wav 路径写入 plan.deliverables,作为附件文件交付,用户可手动点开。"
            "**不要**靠 push=true 让用户'听到'你刚生成的内容——它不会自动发。"
        )

    return json.dumps(result, ensure_ascii=False)


async def _handle_delegate(
    archive_id: str, group_id: str, user_id: str,
    workspace_dir: str, args: dict,
) -> str:
    # Lazy import：见文件顶部说明。函数被调用时 registry 已完全加载，
    # delegate 此时也能正常引用回 registry 中的 schema。
    from app.llm.tools import delegate as dl_tool
    return await dl_tool.handle_delegate(
        workspace_dir, args,
        archive_id=archive_id, group_id=group_id, user_id=user_id,
    )
