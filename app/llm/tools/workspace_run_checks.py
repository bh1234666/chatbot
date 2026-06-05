"""Command diagnostics and safety checks for workspace.run."""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys
from typing import Callable

from app.llm.tools.workspace_text import _helper_missing_file_fetch_hint, _same_pattern


_is_main_thread_provider: Callable[[], bool] | None = None


def set_main_thread_provider(provider: Callable[[], bool] | None) -> None:
    global _is_main_thread_provider
    _is_main_thread_provider = provider


def _is_main_thread() -> bool:
    if _is_main_thread_provider is None:
        return True
    try:
        return bool(_is_main_thread_provider())
    except Exception:
        return True


def _helper_has_env_workspace(ws_dir: str) -> bool:
    try:
        return bool(ws_dir) and (Path(ws_dir) / "_env").is_dir()
    except OSError:
        return False


def _helper_scope_error(command: str, ws_dir: str) -> str:
    normalized = command.replace("\\", "/")
    if "_env/" in normalized or "/_env/" in normalized or "\\_env\\" in command or _helper_has_env_workspace(ws_dir):
        return (
            "helpers cannot access project files through absolute or parent-directory paths. "
            "In environment/project work, commands run from the helper sandbox, not from the real project root. "
            "Use only relative paths that exist inside this sandbox, usually the staged local `_env/...` copy "
            "such as `_env/<project-relative-path>`; "
            "run commands as `cd _env/... && ...` or point tools at `_env/...` files. "
            "If the local `_env/...` copy is missing, locate the sandbox file first; then report the exact "
            "project-relative path the main process must fetch and resume with."
            "\n项目 helper 的命令只在沙箱中运行；用本地 _env 相对路径，缺副本时先定位，再向主线程请求精确项目路径。"
        )
    return (
        "helpers cannot access .prev/ or files outside .temp/ directly; "
        "use fetch_to_temp(source='prev', paths=[...]) to request files "
        "from the previous session snapshot, or fetch_to_temp(source='main') "
        "for files from the permanent workspace."
        "\nhelper 只能访问沙箱；历史或主区文件需先获取到本地。"
    )


_DANGEROUS_KEYWORDS = {
    "format",
    "diskpart",
    "regedit",
    "reg add",
    "reg delete",
    "reg import",
    "reg export",
    "net user",
    "net localgroup",
    "net share",
    "shutdown",
    "logoff",
    "takeown",
    "icacls",
    "cacls",
    "runas",
    "schtasks",
    "bcdedit",
    "bootcfg",
    "fsutil",
    "label",
    "convert",
    "compact",
    "cipher",
    "vssadmin",
    "wmic",
    "powershell",
    "start",
    "taskkill",
    "tskill",
    "rundll32",
    "mshta",
    "cscript",
    "wscript",
    "msiexec",
}

# ═══════════════════════════════════════════════════════════════
# cmd：破坏性操作（需检查目标路径是否在沙箱外）
# ═══════════════════════════════════════════════════════════════
_CMD_DESTRUCTIVE_OPS = {
    "del", "erase",
    "rmdir", "rd",
    "ren", "rename",
    "move",
    "mkdir", "md",
    "copy", "xcopy", "robocopy",
}

# ═══════════════════════════════════════════════════════════════
# Windows shell 包装(handle_run 用)
# ═══════════════════════════════════════════════════════════════
# create_subprocess_exec 不走 shell;Windows 上以下三类命令必须走 cmd /c 才能跑通:
#   1. cmd 内置命令(dir / echo / type 等,不是独立 .exe)
#   2. shell 操作符(&& / || / | / 重定向)
#   3. 裸调 cwd 中的 .exe(shutil.which 不搜 cwd,会找不到)
# 检测到任一条件时,把命令包装成 cmd /c <原命令> 走 shell 模式。
_WIN_BUILTIN_CMDS = {
    # 这些是 cmd.exe 的内置命令,不是独立可执行文件;exec 调不到
    "dir", "echo", "type", "copy", "move", "del", "erase",
    "md", "mkdir", "rd", "rmdir", "cd", "chdir",
    "cls", "ver", "set", "where", "find", "findstr",
    "more", "tree", "title", "ren", "rename",
    "date", "time", "vol", "color", "prompt", "path",
    "assoc", "ftype", "help", "if", "for", "goto", "call",
}
# shell 操作符——出现这些字符就必须走 shell 解析
_WIN_SHELL_OPERATORS = ("&&", "||", " | ", "|>", ">>", "2>", "1>", " > ", " < ")

# 2026-05-07 Bug 1+11 fix: detect already-wrapped cmd /c|k (case-insensitive)
import re as _re
_ALREADY_CMD_RE = _re.compile(r'^cmd(?:\.exe)?\s+/(?:c|k)\b', _re.IGNORECASE)


# ═══════════════════════════════════════════════════════════════
# GCC 安全检查：禁止可加载任意代码的标志
# ═══════════════════════════════════════════════════════════════
_GCC_BLOCKED_FLAGS = {"-fplugin", "-wrapper", "-specs"}

# 路径提取正则
_PATH_RE = re.compile(
    r'([A-Za-z]:[\\/][^\s"\'&|><;]+'    # 绝对路径 C:\...
    r'|"[^"]*?[\\/][^"]*?"'              # 含路径分隔符的引号字符串
    r"|'[^']*?[\\/][^']*?')"             # 含路径分隔符的单引号字符串
)
_REDIRECT_RE = re.compile(r'[>]{1,2}\s*([^\s&|<>]+)')


def _compact_repeating_lines(text: str, min_repeat: int = 3) -> tuple[str, int]:
    """折叠"同前缀连续重复 ≥ min_repeat 次"的行。

    教训(trace 74b1295b iter 89):helper 加 fprintf 在循环里输出 9 个测试用例 ×
    完整 freq 表(每测试 30+ 行 DEBUG),stdout 13K 中 ~10K 是这种重复模式 dump。
    模型从顶部读起被淹没,看不到尾部 PASS/FAIL。

    策略:同"前缀 30 字符"连续 ≥ N 次 → 保留前 1 行 + "[similar M lines collapsed]"。
    保留每段重复区间的首行 + 末行,中间折叠。

    Returns (compacted_text, n_lines_collapsed)。短文本(<2KB)直接原样返回。
    """
    if not text or len(text) < 2000:
        return text, 0

    lines = text.split("\n")
    if len(lines) < 10:
        return text, 0

    out_lines = []
    i = 0
    n_collapsed = 0
    while i < len(lines):
        line = lines[i]
        prefix = line[:30] if line else ""
        # 看本行往后多少行同前缀
        j = i + 1
        while j < len(lines):
            next_line = lines[j]
            next_prefix = next_line[:30] if next_line else ""
            # 同前缀 OR 同样 N-byte 模式(允许行末小变化,如 sym=0x42 → sym=0x55)
            if (next_prefix == prefix and prefix.strip()) or _same_pattern(line, next_line):
                j += 1
            else:
                break
        run = j - i
        if run >= min_repeat:
            # 保留首行 + 折叠中间 + 保留末行
            out_lines.append(line)  # 首
            if run > 2:
                out_lines.append(f"  [... similar {run - 2} lines collapsed (same pattern) ...]")
                n_collapsed += run - 2
            out_lines.append(lines[j - 1])  # 末
            i = j
        else:
            out_lines.append(line)
            i += 1

    return "\n".join(out_lines), n_collapsed


def _diagnose_build_failure(command: str, stderr: str, stdout: str,
                           returncode: int = 0,
                           ws_dir: str | None = None) -> str:
    """B9 (2026-05-02): 智能解读 build 错误,返回针对性修正提示。

    实测 trace 3da78120 — helper 收到 undefined reference 反复改源码瞎试 67 轮。
    问题是 gcc 命令本身错(写了 .o 但工作区没 .o 文件),不是源码错。如果工具层
    主动指出"你的命令引用了 .o 文件,但工作区只有 .c — 改用 .c 试试",helper
    一次就能修对。

    2026-05-11 增强:ws_dir 参数,header-not-found 时实际扫工作区给出位置建议
    (实测 trace 822f2aaa: `#include "common.h"` 失败,common.h 实际在 `_shared/common.h`)。

    返回的 hint 文字会被加入 result["fix_it_hint"] 字段,LLM 看 tool result 时
    自然会看到。

    Returns: English-first correction hint shown directly to the model; empty when unrecognized.
    """
    # ── 2026-05-04 Bug #26/#27/#31: NTSTATUS / cmd.exe 专属 returncode 诊断 ──
    # 这些错误不会在 stderr/stdout 里体现为可解析文本,
    # 但 returncode 直接就是 NTSTATUS 或 cmd.exe 标准错误码。
    # 实测 trace 904c47ec: rc=3221226356 (堆损坏) + rc=255 (head 不存在) 均未被旧版诊断。
    if returncode == 3221226356:  # 0xC0000374 STATUS_HEAP_CORRUPTION
        return (
            "HEAP CORRUPTION (rc=0xC0000374). Treat this as a C/C++ memory-management bug, "
            "usually double-free, use-after-free, or buffer overflow corrupting heap metadata. "
            "Check malloc/free ownership including error paths, compile with gcc -fsanitize=address when available, "
            "verify writes stay inside allocated sizes, and inspect strcpy/sprintf-style buffer use. Fix code before rerunning.\n"
            "堆损坏通常是内存管理 bug；先修所有权、越界和缓冲区写入。"
        )
    if returncode == 3221225477:  # 0xC0000005 STATUS_ACCESS_VIOLATION
        return (
            "SEGFAULT / ACCESS VIOLATION (rc=0xC0000005). Common causes are null dereference, "
            "out-of-bounds write, use-after-free, or stack overflow from huge locals or recursion. "
            "Add a small trace before suspected crash points or use gcc -fsanitize=address when available.\n"
            "访问冲突优先检查空指针、越界、释放后使用和栈溢出。"
        )
    if returncode == 3221226505:  # 0xC0000409 STATUS_STACK_BUFFER_OVERRUN
        return (
            "STACK BUFFER OVERRUN (rc=0xC0000409). A stack buffer write likely exceeded a local array "
            "and corrupted the stack canary. Inspect local char buffers and scanf/gets/strcpy/sprintf usage.\n"
            "栈缓冲区溢出优先检查局部数组和不安全字符串写入。"
        )
    if returncode == 255 and sys.platform == "win32":
        # cmd.exe 返回 255 = 命令不存在或无法执行
        _cmd = command.split()[0] if command.split() else "?"
        _stderr_tail = (stderr or "")[-200:]
        if ("不是内部或外部命令" in _stderr_tail
                or "不是内部命令" in _stderr_tail
                or "not recognized" in _stderr_tail.lower()):
            return (
                f"Command '{_cmd}' is not available under cmd.exe (rc=255). Common causes: Unix-only commands "
                "such as head/tail/grep/sed/awk/wc, a wrong .exe path, or running a program before compiling it. "
                "Use Windows equivalents, dedicated read/search tools, locate the executable, or compile first.\n"
                "cmd 下命令不存在；换 Windows 等价命令、专用工具，或先编译。"
            )
    if returncode == 1 and sys.platform == "win32":
        # 返回 1 + 空输出。两种可能：
        #   a) 编译/运行了 exe,但程序 crash 或 exit(1) 无消息
        #   b) 纯粹 shell 命令(findstr 无匹配 / dir 空目录 / 重定向吞输出)
        # 只有情况 a 需要 exe 诊断;情况 b 给简化提示即可。
        _stderr_tail = (stderr or "")[-200:]
        if not stdout and not stderr:
            _is_shell_cmd = (
                "|" in command
                or ">" in command
                or "2>nul" in command
                or command.startswith("cmd /c")
                or any(
                    command.startswith(c) for c in
                    ("dir", "findstr", "where", "type", "copy", "move",
                     "del ", "echo", "set ", "ver", "vol", "date ", "time ",
                     "tree", "more", "cls", "ren ", "rename ")
                )
            )
            if _is_shell_cmd:
                return (
                    "Command returned rc=1 with no output. For shell commands this is often a normal no-match "
                    "or missing-target condition, such as findstr with no matches, dir on a missing target, or "
                    "output swallowed by >nul/2>nul. Remove redirection or use search_files/read_file for evidence.\n"
                    "shell 命令空输出 rc=1 常是无匹配或重定向吞输出。"
                )
            return (
                "Command returned rc=1 with empty stdout and stderr. The program likely ran and exited silently. "
                "Check for wrong console/GUI entrypoint, pre-main crash or missing DLL dependency, and redirection to "
                "NUL/$null. Remove redirection and add explicit trace/flush points before diagnosing deeper.\n"
                "程序静默失败时先去掉重定向，检查入口、DLL 和关键输出。"
            )

    if not stderr and not stdout:
        return ""
    text = (stderr or "") + "\n" + (stdout or "")
    cmd_lower = command.lower()

    if "SyntaxError:" in text:
        return (
            "Python SyntaxError. If the command uses `cmd /c python -c ...`, complex quotes, or f-strings, "
            "stop retrying one-line `-c`. Write a .py script with workspace(action='write') and run it with "
            "workspace(action='run', command='python script.py') so quoting is stable and line numbers are real.\n"
            "复杂 Python 检查写成脚本再运行，避免 shell 引号破坏。"
        )
    if "KeyError:" in text:
        return (
            "Python KeyError. Ground the fix in actual CSV/JSON field names. Print columns, head(), and unique "
            "values with a small probe, then update code using the actual field names and values.\n"
            "KeyError 时先打印真实字段和值，再改代码。"
        )

    # ── 1. gcc 链接错误: undefined reference ──
    import re as _re_local
    undef_matches = _re_local.findall(
        r"undefined reference to [`']([A-Za-z_][A-Za-z0-9_]*)['']", text,
    )
    if undef_matches:
        # 看 command 里有没有 .o 引用
        o_files = _re_local.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\.o\b", command)
        c_files = _re_local.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\.c\b", command)
        unique_undef = sorted(set(undef_matches))[:8]  # 截前 8 个
        symbol_list = ", ".join(unique_undef)
        if o_files:
            return (
                f"Link failed: undefined reference to {symbol_list}. The command references object files {o_files}. "
                "If those .o files are not present in the workspace, compile the corresponding .c sources together "
                "instead, such as `gcc smoke.c first_fit.c best_fit.c -o exe`. Confirm object/source files with search_files first.\n"
                "链接失败且引用 .o 时，确认 .o 是否存在；不存在就改用源码一起编译。"
            )
        elif c_files and len(unique_undef) > 0:
            # 有 .c 文件但仍 undefined — 可能是函数定义在 .c 但未链接进来
            return (
                f"Link failed: undefined reference to {symbol_list}. The command includes .c files {c_files}, "
                "so the missing symbols are not defined there. Search for the definitions in other .c files and add "
                "them to the compile command, or compare header declarations with implementation names for spelling mismatch.\n"
                "已有 .c 仍 undefined 时，查找定义所在源码或核对声明/定义拼写。"
            )
        else:
            return (
                f"Link failed: undefined reference to {symbol_list}. GCC cannot find definitions for these symbols. "
                "Check whether the compile command is missing a .c source file, symbol names differ, or a library such as "
                "-lm or -lpthread is required.\n"
                "undefined reference 要查缺失源码、符号拼写或链接库。"
            )

    # ── 2. missing header ──
    m = _re_local.search(r"fatal error: ([^:\s]+(?:\.h)?): No such file", text)
    if m:
        header = m.group(1).strip()
        # 2026-05-11 增强: 实际扫工作区找这个 header,给精确建议
        actual_locations: list[str] = []
        if ws_dir and os.path.isdir(ws_dir):
            header_basename = os.path.basename(header)
            try:
                for root, dirs, files in os.walk(ws_dir):
                    dirs[:] = [d for d in dirs
                               if not d.startswith((".", "_delegate_"))
                               or d in ("_shared", "_helpers_shared")]
                    if header_basename in files:
                        rel = os.path.relpath(os.path.join(root, header_basename), ws_dir)
                        actual_locations.append(rel.replace("\\", "/"))
                    if len(actual_locations) >= 5:
                        break
            except OSError:
                pass
        if actual_locations:
            # 找到了 — 给出精确路径,LLM 一次改对
            loc_str = ", ".join(actual_locations[:3])
            return (
                f"Header '{header}' was not found by the compiler, but matching files exist at: {loc_str}. "
                "Use the correct include path, such as `#include \"_shared/common.h\"`, or add an -I include directory "
                "such as `gcc -I_shared ...`. Prefer a precise include path when stable.\n"
                "头文件存在但 include 路径不对；改 include 路径或加 -I。"
            )
        return (
            f"Header '{header}' was not found. Check whether the header exists in the workspace, whether an -I include "
            "directory is needed, or whether it belongs to an unavailable system dependency.\n"
            "头文件缺失时先查工作区位置、include 目录和系统依赖。"
        )

    # ── 3. WinMain (gcc 默认 GUI 链接,代码里只有 main) ──
    if "undefined reference to `WinMain" in text:
        return (
            "Windows MinGW is looking for WinMain instead of a console main entrypoint. Confirm the source has "
            "`int main(...)` and that the file containing main is included in the compile command.\n"
            "WinMain 错误通常是缺 main 或没把含 main 的文件编进去。"
        )

    # ── 4. implicit declaration ──
    m = _re_local.search(
        r"warning: implicit declaration of function ['`]([A-Za-z_][A-Za-z0-9_]*)['`]",
        text,
    )
    if m:
        fn = m.group(1)
        # 知名 C 标准库函数
        common_headers = {
            "printf": "stdio.h", "scanf": "stdio.h", "fprintf": "stdio.h",
            "sprintf": "stdio.h", "fopen": "stdio.h", "fclose": "stdio.h",
            "malloc": "stdlib.h", "free": "stdlib.h", "calloc": "stdlib.h",
            "realloc": "stdlib.h", "exit": "stdlib.h", "atoi": "stdlib.h",
            "strlen": "string.h", "strcpy": "string.h", "strcmp": "string.h",
            "memcpy": "string.h", "memset": "string.h",
            "assert": "assert.h",
            "sqrt": "math.h", "pow": "math.h", "log": "math.h",
            "time": "time.h", "clock": "time.h",
        }
        if fn in common_headers:
            return (
                f"Implicit declaration warning for '{fn}'. Add `#include <{common_headers[fn]}>` before recompiling.\n"
                "隐式声明通常是缺对应头文件。"
            )
        return (
            f"Implicit declaration warning for '{fn}'. Ensure the project header declaring it is included, or find "
            "the correct library header if it is external.\n"
            "隐式声明需补项目头文件或库头文件。"
        )

    # ── 5. segfault / abort 在 stdout (运行时) ──
    if _re_local.search(r"Segmentation fault|core dumped|SIGSEGV", text):
        return (
            "Runtime segmentation fault (SIGSEGV). Check null dereferences, out-of-bounds writes, use-after-free, "
            "and stack overflow. Add a small trace before suspected crash points or use sanitizers when available.\n"
            "段错误优先检查空指针、越界、释放后使用和栈溢出。"
        )

    # ── 6. python 异常 (run python script) ──
    m = _re_local.search(
        r"\n([A-Z][A-Za-z]+(?:Error|Exception)): (.+)",
        text,
    )
    if m and "python" in cmd_lower:
        exc_type = m.group(1)
        exc_msg = m.group(2)[:120]
        if exc_type == "ModuleNotFoundError":
            return (
                f"Python module is missing: {exc_msg}. Install the package or check the module spelling. "
                "Confirm which Python/pip executable the subprocess is using.\n"
                "缺 Python 模块时检查拼写、安装和 pip 路径。"
            )
        if exc_type == "FileNotFoundError":
            hint = (
                f"Python file path is missing: {exc_msg}. Locate the file with workspace.locate/list_files and remember "
                "paths are relative to the workspace root, not necessarily the process cwd.\n"
                "文件不存在时先定位工作区相对路径。"
            )
            fetch_hint = _helper_missing_file_fetch_hint(ws_dir or "", exc_msg.strip("'\""))
            return hint + fetch_hint
        return (
            f"Python {exc_type}: {exc_msg}. Understand the concrete exception and code path before editing or retrying.\n"
            "先理解异常原因和代码路径，再修改。"
        )

    # ── 7. C99 declaration in for-loop (gcc 旧默认 C89) ──
    if ("C99 or C11 mode" in text
            or "for loop initial declarations" in text
            or "ISO C90 forbids" in text):
        return (
            "GCC is compiling in a C89/C90 mode that disallows declarations inside for-loops. Prefer adding "
            "`-std=c99` or `-std=c11` to the compile command rather than rewriting valid C99 code.\n"
            "for 循环声明变量报错时优先给 gcc 加 -std=c99/-std=c11。"
        )

    # ── 8. multiple definition (头文件里写实现 / 重复传 .c) ──
    if "multiple definition" in text:
        m = _re_local.search(
            r"multiple definition of [`'](.+?)['`]", text,
        )
        sym = m.group(1) if m else "<symbol>"
        return (
            f"Link error: '{sym}' has multiple definitions. Common causes are function implementations placed in "
            "headers without static/inline, or the same .c file passed more than once to GCC.\n"
            "多重定义通常是头文件放实现或重复编译同一源码。"
        )

    # ── 9. npm / node ──
    if "npm" in cmd_lower or "node" in cmd_lower:
        if "ERESOLVE" in text:
            return (
                "npm dependency conflict (ERESOLVE). Try `npm install --legacy-peer-deps` when appropriate, or inspect "
                "package versions before using `--force`.\n"
                "npm 依赖冲突先看版本，可考虑 legacy-peer-deps。"
            )
        if "ENOENT" in text and "package.json" in text:
            return "npm cannot find package.json. Check cwd or initialize the project first.\nnpm 找不到 package.json 时检查 cwd 或初始化。"

    return ""


def _security_check(command: str, ws_dir: str) -> str | None:
    """安全检查。返回 None = 通过，否则返回错误描述。"""

    # Layer 1：高危关键字（词边界匹配，避免 -Wformat 误拦截 format）
    cmd_lower = command.lower()
    for kw in _DANGEROUS_KEYWORDS:
        if re.search(r'\b' + re.escape(kw) + r'\b', cmd_lower):
            return f"security blocked: command uses restricted system operation '{kw}'.\n安全策略拦截该系统操作。"

    # Layer 2：命令类型分发
    parts = command.split()
    exe = parts[0].lower() if parts else ""

    # 2026-05-09 Patch 28(撤回 Patch 27 的硬拦截):
    # 主线程不被禁写代码的根本原因是**能力**(对长串迭代编码效率低、正确率低),
    # 不是**绝对策略**。短校验脚本(10-30 行,如"docx 里有几张 PNG")主线程一次
    # 写对完全够用。硬拦截 python/make/node 把这类合法快速校验也封死,反而
    # 逼主线程要么瞎编 deliverable,要么白绕一圈派 helper 做 30s 能搞定的事。
    # 代码层现在只阻止"实质性破坏"(沙箱外写、危险关键字);主线程跑解释器
    # 的合理性由 Round 2 prompt 引导(短校验自己写,长串/迭代/.c 走 delegate)。

    if not _is_main_thread() and _touches_prev_or_outside(command, ws_dir):
        return _helper_scope_error(command, ws_dir)

    if exe in _CMD_DESTRUCTIVE_OPS:
        paths = _extract_paths(command)
        for p in paths:
            if _is_abs_outside(p, ws_dir):
                return f"security blocked: refusing {exe} outside workspace ({p})"

    if exe in ("cmd", "cmd.exe"):
        return _check_cmd(command, ws_dir)
    elif exe in ("gcc", "g++", "gcc.exe", "g++.exe"):
        return _check_gcc(command, ws_dir)

    return None


def _check_cmd(command: str, ws_dir: str) -> str | None:
    """检查 cmd 命令是否包含对沙箱外的破坏性操作。

    规则：
    - 读操作（dir/type/find/findstr/comp/fc/where/tree/more/sort）→ 放行（但 helper 不能访问 .prev/）
    - 破坏性操作（del/rmdir/move 等）→ 检查目标路径，禁止操作沙箱外
    - 重定向写入（> / >>）→ 检查写入目标，禁止指向沙箱外
    """
    # 解析出 cmd /c 或 /k 后的实际命令
    parts = command.split()
    actual_start = 1  # 跳过 "cmd"
    if len(parts) > 1 and parts[1].lower() in ("/c", "/k"):
        actual_start = 2

    if actual_start >= len(parts):
        return None  # 空命令，让 cmd 自己报错

    actual_parts = parts[actual_start:]
    first_token = actual_parts[0].lower().rstrip("/")

    # 2026-05-09: 原 Patch 27 在此处加 _MAIN_THREAD_INTERPRETERS_INNER 封死
    # `cmd /c python xxx` 等间接调用,已撤(见 _security_check 注释)。

    # 2026-05-07: helper 禁止访问 .prev/ (历史工作区) 和 main workspace 外部
    # 实测 trace: verify_sbt helper 用 `dir /b ...\.prev\..._sbt_impl_v2\` 访问旧工作区,
    # 拿到的内容严重污染(60+ 不相关文件),导致验证迷失方向。
    # .prev/ 是只读历史快照,helper 需用 fetch_to_temp 才能获取其中的文件。
    if not _is_main_thread() and _touches_prev_or_outside(command, ws_dir):
        return _helper_scope_error(command, ws_dir)

    # 读操作：放行（但已通过上面的 .prev/ 检查）
    _READ_OPS = {
        "dir", "type", "find", "findstr", "more", "sort",
        "comp", "fc", "where", "tree", "echo", "set",
        "cd", "chdir", "date", "time", "ver", "vol",
        "cls", "title", "color", "prompt", "path",
        "help", "assoc", "ftype", "driverquery", "systeminfo",
        "whoami", "hostname", "ipconfig", "ping", "tracert",
        "nslookup", "netstat", "arp", "getmac", "tasklist",
    }
    if first_token in _READ_OPS:
        # echo 配合重定向可能写入，单独检查
        if first_token == "echo" and _has_redirect_to_outside(command, ws_dir):
            return "security blocked: refusing redirect outside workspace.\n安全策略拦截沙箱外重定向写入。"
        return None

    # 破坏性操作：检查所有路径目标
    if first_token in _CMD_DESTRUCTIVE_OPS:
        paths = _extract_paths(command)
        for p in paths:
            if _is_abs_outside(p, ws_dir):
                return f"security blocked: refusing {first_token} outside workspace ({p}).\n安全策略拦截沙箱外文件操作。"

    # 检查重定向写入目标
    if _has_redirect_to_outside(command, ws_dir):
        return "security blocked: refusing redirect outside workspace.\n安全策略拦截沙箱外重定向写入。"

    return None


def _check_gcc(command: str, ws_dir: str) -> str | None:
    """Security check for gcc/g++ compilation.

    Ensures:
    1. No blocked flags (plugin loading, wrapper invocation, specs override)
    2. No @file argument bypass
    3. -o output stays within workspace
    4. No shell redirect outside workspace
    """
    parts = command.split()

    for i, part in enumerate(parts):
        part_lower = part.lower()
        base = part_lower.split("=")[0].rstrip("/")
        if base in _GCC_BLOCKED_FLAGS:
            return f"security blocked: GCC flag '{part}' is restricted.\n安全策略拦截该 GCC 参数。"
        if part.startswith("@") and len(part) > 1:
            return "security blocked: GCC @file argument files are restricted.\n安全策略拦截 GCC @file 参数。"

        # Check -o output targets
        if part == "-o" and i + 1 < len(parts):
            output_path = parts[i + 1]
            if _is_abs_outside(output_path, ws_dir):
                return f"security blocked: GCC -o output must stay inside the workspace ({output_path}).\nGCC 输出路径必须在工作区内。"
        elif part.startswith("-o") and len(part) > 2:
            output_path = part[2:]
            if _is_abs_outside(output_path, ws_dir):
                return f"security blocked: GCC -o output must stay inside the workspace ({output_path}).\nGCC 输出路径必须在工作区内。"

    # Check shell redirect
    if _has_redirect_to_outside(command, ws_dir):
        return "security blocked: refusing redirect outside workspace.\n安全策略拦截沙箱外重定向写入。"

    return None


def _extract_paths(command: str) -> list[str]:
    """从命令字符串中提取所有可能的文件路径。"""
    paths = []
    for m in _PATH_RE.finditer(command):
        p = m.group(1).strip("\"'")
        paths.append(p)
    return paths


def _is_abs_outside(path_str: str, ws_dir: str) -> bool:
    """绝对路径是否在工作区外。相对路径视为沙箱内。"""
    p = Path(path_str)
    if not p.is_absolute():
        return False
    try:
        resolved = p.resolve()
        ws = Path(ws_dir).resolve()
        resolved.relative_to(ws)
        return False
    except (ValueError, OSError):
        return True


def _has_redirect_to_outside(command: str, ws_dir: str) -> bool:
    """检查命令的重定向目标是否在沙箱外。"""
    m = _REDIRECT_RE.search(command)
    if not m:
        return False
    dest = m.group(1).strip("\"'")
    if dest.replace("\\", "/").lower() in {"/dev/null", "nul", "null"}:
        return False
    return _is_abs_outside(dest, ws_dir)


def _touches_prev_or_outside(command: str, ws_dir: str) -> bool:
    """检查命令是否触碰 .prev/ 或 helper 沙箱外的路径。

    对 helper 而言,合法访问范围:
      - 自己的 sandbox (ws_dir 内)
      - _shared/ (通过 workspace 工具的安全路径处理)
      - _helpers_shared/ (兄弟 helper 共享)
    禁止:
      - .prev/ (历史轮次的快照,可能严重污染)
      - main workspace 的直接文件(应通过 _helpers_shared/ 获取)
    """
    ws = Path(ws_dir).resolve()
    # 检查命令中每一个看起来像路径的片段
    for m in _PATH_RE.finditer(command):
        p_str = m.group(1).strip("\"'")
        if not p_str:
            continue
        p = Path(p_str)
        # 不需要 resolve——直接检查路径字符串和解析后的 parts
        if not p.is_absolute():
            continue
        try:
            resolved = p.resolve()
        except (OSError, ValueError):
            # 无法解析的路径——保守检查字符串
            if ".prev" in p_str.replace("\\", "/").split("/"):
                return True
            continue
        # 检查是否在沙箱外
        try:
            resolved.relative_to(ws)
        except ValueError:
            return True  # 在沙箱外
        # 在沙箱内但路径包含 .prev 组件
        parts = str(resolved.relative_to(ws)).replace("\\", "/").split("/")
        if ".prev" in parts:
            return True
    # 也检查命令字符串本身是否包含 .prev 路径组件(处理引号包裹的路径)
    if re.search(r'\b\.prev[/\\]', command):
        return True
    return False
