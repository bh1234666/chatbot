"""workspace.run command execution handler."""
from __future__ import annotations

import shlex
import re
import sys
from pathlib import Path

from app.llm.tools.command_risk import helper_sandbox_copy_error


_UNIX_INVENTORY_COMMANDS = {
    "ls", "find", "grep", "egrep", "fgrep", "head", "tail", "wc", "sed", "awk",
    "xargs", "cat",
}


def _bin_not_found_guidance(bin_name: str) -> tuple[str, str]:
    """Return model-facing recovery text for missing executables."""
    name = (bin_name or "").strip()
    lower = name.lower()
    if lower in _UNIX_INVENTORY_COMMANDS and sys.platform == "win32":
        return (
            (
                f"Executable '{name}' was not found in this Windows workspace. "
                "This is usually a Unix inventory command, not a missing project dependency."
            ),
            (
                "Use platform-neutral file tools instead of retrying Unix commands: workspace locate for file lists, "
                "search_files/search_in_file for matching, read_file/inspect_file for content and metadata. "
                "For environment inventories, first inspect `_env/project_inventory.md` and "
                "`_env/.resource_manifest.json` when present. If a custom scan is still needed, write a small "
                "Python script under `_scratch/` and run it with an explicit timeout.\n"
                "Unix 盘点命令不可用时，改用 locate/search/read/inspect，或在 _scratch 写小型 Python 脚本。"
            ),
        )
    return (
        (
            f"Executable '{name}' was not found in PATH or in the workspace cwd. "
            "It may be missing, misspelled, or intended to run through `python -m <module>` instead of a bare executable."
        ),
        (
            "Prefer `python -m <package>` when the tool is a Python module, or install the missing package first. "
            "For visual text extraction, use the OCR tool rather than trying to install tesseract in the workspace.\n"
            "Python module tools should use python -m; visual text extraction should use the OCR tool."
        ),
    )


def _python_c_syntax_fix_hint(command: str, stderr: str) -> str | None:
    """Guide the model away from fragile long python -c commands."""
    cmd = command or ""
    if "python" not in cmd.lower() or " -c " not in cmd.lower():
        return None
    if "SyntaxError" not in (stderr or ""):
        return None
    return (
        "Python `-c` failed with a SyntaxError. For anything beyond a trivial one-liner, write a temporary "
        "script under `_scratch/` with workspace.write, run `python _scratch/<script>.py` with an explicit timeout, "
        "then read the output file or concise stdout. This avoids shell quoting loss and makes retries editable.\n"
        "python -c 语法失败时，把多行或复杂逻辑写成 _scratch 临时脚本再运行。"
    )


def _unix_inventory_syntax_fix_hint(command: str, stderr: str, stdout: str) -> str | None:
    """Detect Unix-style inventory commands that failed under Windows/cmd."""
    if sys.platform != "win32":
        return None
    cmd = (command or "").strip()
    lower = cmd.lower()
    if not cmd:
        return None
    unix_patterns = (
        r"(^|[&|]\s*)find\s+[^&|]*\s-(?:type|name|maxdepth|mindepth|print)\b",
        r"(^|[&|]\s*)ls(?:\s|$)",
        r"(^|[&|]\s*)grep(?:\s|$)",
        r"(^|[&|]\s*)head(?:\s|$)",
        r"(^|[&|]\s*)tail(?:\s|$)",
        r"(^|[&|]\s*)wc(?:\s|$)",
        r"/dev/null|<<\s*['\"]?\w+|`[^`]+`|\$\(",
    )
    if not any(re.search(pattern, lower) for pattern in unix_patterns):
        return None
    combined = f"{stderr}\n{stdout}".lower()
    failure_signals = (
        "invalid switch", "参数格式不正确", "找不到", "not recognized",
        "was unexpected", "file not found", "系统找不到", "用法",
        "security blocked", "refusing redirect outside workspace",
    )
    if combined.strip() and not any(signal in combined for signal in failure_signals):
        return None
    return (
        "This command uses Unix-style file inventory syntax on Windows/cmd and failed. Switch to a platform-appropriate "
        "`find|head|ls|grep` pattern. Use workspace locate for file lists, search_files/search_in_file for matching, "
        "read_file/inspect_file for content and metadata, or write a small `_scratch/*.py` inventory script and run it "
        "with an explicit timeout. In environment helpers, inspect `_env/project_inventory.md` and "
        "`_env/.resource_manifest.json` first when they exist.\n"
        "Windows/cmd 下 Unix 盘点语法失败时，改用 locate/search/read/inspect，或写 _scratch Python 脚本。"
    )


def _windows_unix_inventory_command_hint(command: str) -> str | None:
    """Return a recovery hint when Windows blocks Unix inventory syntax early."""
    return _unix_inventory_syntax_fix_hint(
        command,
        "security blocked: refusing redirect outside workspace",
        "",
    )


def _sync_workspace_globals() -> None:
    from app.llm.tools import workspace as _workspace
    globals().update({
        name: value
        for name, value in vars(_workspace).items()
        if not name.startswith("__") and name != "handle_run"
    })


def _subprocess_start_error_response(label: str, exc: BaseException, *, executable: str | None = None) -> dict:
    name = type(exc).__name__
    return {
        "ok": False,
        "error": f"{label} start failed: {name}: {exc}. Check command, permissions, executable path, or cwd.",
        "error_type": name,
        "executable": executable or label,
    }


async def _create_subprocess_exec_guarded(label: str, *args, executable: str | None = None, **kwargs):
    try:
        return await asyncio.create_subprocess_exec(*args, **kwargs)
    except FileNotFoundError:
        raise
    except (PermissionError, OSError) as e:
        debug.log("workspace.run.exec_failed", f"P87 {label} {type(e).__name__}: {e}")
        return _subprocess_start_error_response(label, e, executable=executable or (args[0] if args else None))


async def _create_subprocess_shell_guarded(label: str, cmd: str, **kwargs):
    try:
        return await asyncio.create_subprocess_shell(cmd, **kwargs)
    except FileNotFoundError:
        raise
    except (PermissionError, OSError) as e:
        debug.log("workspace.run.exec_failed", f"P87 {label} {type(e).__name__}: {e}")
        return _subprocess_start_error_response(label, e)


async def _retry_windows_cmd_after_start_error(
    error_result: dict,
    cmd_line: str,
    ws_dir: str,
    sandbox_env: dict,
    creationflags: int,
    preexec_fn,
):
    """Retry simple Windows raw-exec launch failures through cmd.exe.

    Some tools resolve through PATH but still fail CreateProcess with
    PermissionError/OSError (for example shim/alias/Store-app edge cases).
    Let cmd.exe perform normal Windows command resolution before reporting a
    tool failure to the model.
    """
    if sys.platform != "win32":
        return error_result
    if error_result.get("error_type") not in {"PermissionError", "OSError"}:
        return error_result
    debug.log("workspace.run.cmd_fallback", f"raw exec failed; retrying via cmd /c: {cmd_line[:120]}")
    proc = await _create_subprocess_exec_guarded(
        "cmd /c fallback",
        "cmd", "/c", cmd_line,
        executable="cmd",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=ws_dir,
        env=sandbox_env,
        creationflags=creationflags,
        preexec_fn=preexec_fn,
    )
    if isinstance(proc, dict):
        proc["fallback_from"] = error_result
    return proc


def _augment_pytest_command(cmd_line: str, ws_dir: str) -> str:
    needle = "python -m pytest"
    lower = cmd_line.lower()
    idx = lower.find(needle)
    if idx < 0:
        return cmd_line
    flags: list[str] = []
    if "--rootdir" not in lower:
        flags.append("--rootdir=.")
    if " -c " not in lower and " --config-file" not in lower:
        root = Path(ws_dir)
        prefix = cmd_line[:idx].strip()
        import re as _re
        cd_match = _re.search(r"(?:^|&&)\s*cd\s+(?:/d\s+)?(?P<subdir>[^\s&]+)\s*&&\s*$", prefix, _re.IGNORECASE)
        if cd_match:
            subdir = cd_match.group("subdir").strip().strip('"')
            candidate = (Path(ws_dir) / subdir).resolve()
            try:
                candidate.relative_to(Path(ws_dir).resolve())
                if candidate.is_dir():
                    root = candidate
            except ValueError:
                pass
        project_config = next(
            (name for name in ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")
             if (root / name).is_file()),
            None,
        )
        if project_config:
            flags.append(f"-c {shlex.quote(project_config)}")
        else:
            empty_config = root / ".workspace_pytest_empty.ini"
            if not empty_config.exists():
                empty_config.write_text("[pytest]\n", encoding="utf-8")
            config_path = str(empty_config)
            if sys.platform == "win32":
                config_path = '"' + config_path.replace('"', '\\"') + '"'
            else:
                config_path = shlex.quote(config_path)
            flags.append(f"-c {config_path}")
    if not flags:
        return cmd_line
    insert_at = idx + len(needle)
    return cmd_line[:insert_at] + " " + " ".join(flags) + cmd_line[insert_at:]


async def handle_run(
    ws_dir: str, command: str, *,
    timeout_sec: int | None = None,
    abort_event: "asyncio.Event | None" = None,
    prefer_unix_shell: bool = False,
) -> dict:
    """执行子进程命令。

    timeout_sec: LLM 必须显式指定预期耗时(1~7200s)。None = 走 1s 默认(故意很短)。
        - 1s 默认是**故意的认知负担**:LLM 不传值 → 必失败 → error 信息教 LLM 下次必传
        - 启发式三档(30/90/300)已废弃 — 实测 ML 关键词误判频繁(gcc 编译被识别成 ML)
        - 超过 _RUN_TIMEOUT_HARD_CAP=7200 会被截断到 7200(防 LLM 失误传 99999)
        - <1 会被提到 1
    超时处理:必须 proc.kill() + wait,否则子进程留作僵尸,占文件锁导致
        后续 gcc/del 全部 Permission denied(trace da389409 helper i 实测,
        浪费 6 分钟)。
    abort_event: 用户/系统主动 abort 信号(Phase 5++ 修)
        实测 trace 9ca732f4: 用户 abort 但 tool 还在跑 → 等 tool 完才响应,
        plus 后面 forced_finalize 又 100s,user 等了 ~2 分钟。
        现在 abort_event set 时,**立刻 kill 子进程**,error 信息标 aborted=True。
    prefer_unix_shell: 2026-05-03 加(Bug 1 修)。`bash` 工具调用时传 True:
        Windows 上若检测到 git-bash 就用 `bash.exe -c <cmd>` 跑,unix 命令真生效。
        Linux/macOS 不影响(本来就 bash)。Windows 没 git-bash 时 fallback 到
        cmd /c 并在结果里加 hint 让模型自切 cmd 写法。
        非 bash 工具入口(workspace.run / 内部调用)默认 False 走 cmd 路径不变。
    """
    _sync_workspace_globals()

    if not command or not command.strip():
        return {"ok": False, "error": "empty command"}
    if not ws_dir:
        return {"ok": False, "error": "workspace.run has no workspace directory"}
    if not os.path.isdir(ws_dir):
        try:
            os.makedirs(ws_dir, exist_ok=True)
            debug.log("workspace.run.cwd_created", f"created missing cwd: {ws_dir}")
        except OSError as e:
            return {
                "ok": False,
                "error": f"workspace cwd is not available: {ws_dir} ({type(e).__name__}: {e})",
            }

    # ── 2026-05-06 §SEC1: per-owner bash 速率限制 ──
    _owner = current_owner() or "unknown"
    if not await _check_bash_rate(_owner):
        return {
            "ok": False,
            "error": f"bash rate limit ({_BASH_RATE_LIMIT}/min) exceeded; "
                     "wait before retrying or report failure",
        }

    _raw_copy_error = helper_sandbox_copy_error(command.strip(), is_main_thread=_is_main_thread())
    if _raw_copy_error:
        return {
            "ok": False,
            "error": _raw_copy_error,
            "blocked_reason": "helper_sandbox_copy",
            "suggested_recovery": (
                "Use delegate result file_map/main_available_files/copy_stats, then read or run the exposed "
                "main-workspace path. If the file is absent, resume or replace the helper instead of copying "
                "from its sandbox.\n\n"
                "读取 helper 输出请用 file_map/main_available_files/copy_stats 暴露的主工作区路径，缺文件则续作或重派 helper。"
            ),
        }

    cmd_line = _augment_pytest_command(command.strip(), ws_dir)

    # ── 早决:Windows + git-bash + bash 工具入口 → 直接走 git-bash exec 路径 ──
    # 2026-05-03 Bug 1 + B 修:这个标志要在所有路径变换之前算出,因为下面的
    # `./xxx` 重写在 git-bash 路径下反而有害(`avl_tree.exe` 在 PATH 找不到,
    # 但 `./avl_tree` 在 cwd 能找到)。早决 → 后面分支跳掉无用变换。
    _windows_git_bash = (
        sys.platform == "win32"
        and prefer_unix_shell
        and _GIT_BASH_EXE is not None
    )

    # ── Bug 7 修:Windows 上把 Linux 风格 `./xxx` 转换成可执行的形式 ──
    # 模型(尤其是 helper)经常写 `./avl_tree`,在 Windows 上 shutil.which 找不到、
    # cwd .exe 检测也匹配不上(因为 parts[0] 是 "./avl_tree" 而不是 "avl_tree"),
    # 直接 FileNotFoundError 退出。trace e023a6e0 浪费了 4 次工具调用、helper
    # 整整 120 秒超时全卡在这里。这里在所有其他逻辑之前先做规范化。
    # 2026-05-03 修:仅 cmd.exe 路径做这个转换。git-bash 自己懂 `./xxx` 而且
    # cwd 不在 bash $PATH 里,转成 `avl_tree.exe` 后 bash 反而找不到。
    if (sys.platform == "win32"
            and cmd_line.startswith("./")
            and not _windows_git_bash):
        first_word_end = cmd_line.find(" ")
        if first_word_end == -1:
            head, tail = cmd_line, ""
        else:
            head, tail = cmd_line[:first_word_end], cmd_line[first_word_end:]
        bare = head[2:]  # 去掉 ./
        # 找 cwd 里的可执行文件,优先 .exe
        resolved_name = None
        for cand_ext in (".exe", ".bat", ".cmd", ""):
            if os.path.isfile(os.path.join(ws_dir, bare + cand_ext)):
                resolved_name = bare + cand_ext
                break
        if resolved_name:
            cmd_line = resolved_name + tail
            debug.log("workspace.run.linux_path_fix",
                      f"./{bare}{tail} → {cmd_line[:80]}")
        # 没找到也不算错——继续走原逻辑,会得到清晰的"file not found"

    # ── Windows 命令翻译 (2026-05-04 v19.3) ──
    # 日志分析(trace 4a3c8973)发现 LLM 反复用 python -c "..." + 嵌套引号,
    # 在 cmd.exe 上引号截断导致 SyntaxError(unterminated string literal),
    # 同类错误重复 3 次不换写法。这里在工具层自动改写:
    #   1. python -c "..." 含内层引号 → 写 temp .py 文件执行
    #   2. cd X && cmd           → cmd /c "cd /d X && cmd"
    #   3. unix 路径分隔符 / → \   (仅简单命令,不破坏引号内内容)
    # git-bash 路径跳过(真 Unix shell 不需要翻译)。
    if (sys.platform == "win32"
            and not _windows_git_bash):
        cmd_line = _translate_windows_command(cmd_line, ws_dir)

    parts = cmd_line.split()
    exe = parts[0].lower() if parts else ""

    # ── 安全检查 ──
    legacy_security_error = _security_check(cmd_line, ws_dir)
    if legacy_security_error:
        debug.log("workspace.security", legacy_security_error, {"category": "workspace_run_check"})
        result = {"ok": False, "error": legacy_security_error}
        fix_hint = _windows_unix_inventory_command_hint(cmd_line)
        if fix_hint:
            result["FIX_HINT"] = fix_hint
        return result

    decision = analyze_command(cmd_line, ws_dir, is_main_thread=_is_main_thread())
    if not decision.allowed:
        debug.log("workspace.security", decision.reason, {"category": decision.category})
        result = {
            "ok": False,
            "error": decision.reason,
            "blocked_reason": decision.category,
        }
        fix_hint = _windows_unix_inventory_command_hint(cmd_line)
        if fix_hint:
            result["FIX_HINT"] = fix_hint
        return result

    # ── 构建含 MinGW 的 PATH ──
    _project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    _mingw_bin = os.path.join(_project_root, "MinGW64", "bin")
    _sandbox_path = os.environ.get("PATH", "")
    _python_dir = os.path.dirname(os.path.abspath(sys.executable)) if sys.executable else ""
    _python_scripts = os.path.join(_python_dir, "Scripts") if _python_dir else ""
    for _path_part in (_python_scripts, _python_dir):
        if _path_part and os.path.isdir(_path_part):
            _sandbox_path = _path_part + os.pathsep + _sandbox_path
    if os.path.isdir(_mingw_bin):
        _sandbox_path = _mingw_bin + os.pathsep + _sandbox_path

    # ── 解析可执行文件完整路径（Windows CreateProcess 用自定义 env 时 PATH 查找可能失败）──
    sandbox_env = os.environ.copy()
    sandbox_env["PATH"] = _sandbox_path
    # Do not inherit the service process' Python import path into helper commands.
    # Parallel environment/helper projects must only see packages from cwd or from
    # an explicit PYTHONPATH in the command (for example `set PYTHONPATH=src && ...`).
    sandbox_env.pop("PYTHONPATH", None)
    sandbox_env.pop("PYTHONHOME", None)
    # Toolchain/code subprocesses are CPU-only. Dedicated OCR/MinerU tools build their
    # own environment and may use GPU; generic workspace.run must not consume VRAM.
    sandbox_env["CUDA_VISIBLE_DEVICES"] = ""
    sandbox_env["NVIDIA_VISIBLE_DEVICES"] = "none"
    sandbox_env["HIP_VISIBLE_DEVICES"] = ""
    sandbox_env["ROCR_VISIBLE_DEVICES"] = ""
    sandbox_env["PYTORCH_ENABLE_MPS_FALLBACK"] = "0"
    sandbox_env["MINERU_DEVICE_MODE"] = "cpu"
    resolved = shutil.which(parts[0], path=sandbox_env["PATH"])
    if (
        sys.platform == "win32"
        and parts
        and parts[0].lower() == "python"
        and not resolved
    ):
        py_launcher = shutil.which("py", path=sandbox_env["PATH"])
        if py_launcher:
            debug.log("workspace.run.python_launcher", "python not found; using Windows py launcher")
            parts[0] = py_launcher
            cmd_line = " ".join(parts)
            resolved = py_launcher
    if resolved:
        parts[0] = resolved
    else:
        # 即使 which 没找到也继续尝试（cmd 的内置命令如 dir/echo 不需要完整路径）
        pass

    # ── Windows: 决定是否需要 cmd /c shell 包装 ──
    # exec 模式直接调可执行文件不走 shell,在 Windows 上以下命令会 FileNotFoundError:
    #   - cmd 内置命令(dir/echo/type 等不是独立 .exe)
    #   - shell 操作符(&& / || / | / 重定向)
    #   - 裸调 cwd 中的本地 .exe(shutil.which 不搜 cwd)
    # 历史教训(trace 6353027e):"dir /s /b hello.c"、"hello_fixed.exe"、
    # "gcc -o x x.c && x.exe" 全部 WinError 2,模型浪费 4 轮才猜出 "cmd /c x.exe"。
    # 这里自动检测 + cmd /c 包装,让模型直接写自然命令就能跑。
    #
    # 2026-05-03 Bug 1 修:`bash` 工具入口 prefer_unix_shell=True,Windows 上若有
    # git-bash 直接走 bash.exe -c <cmd>,unix 语法(2>/dev/null / head -N /
    # find -name / grep -rn / pipe)真正生效;没 git-bash 时记 hint,fallback cmd。
    # Linux/macOS 上 prefer_unix_shell 也强制走 shell,让 pipe / redirect / glob /
    # 命令替换 / heredoc 真生效(否则 exec 模式下 `bash("ls | head")` 把 `|`
    # 当成 `ls` 的参数,失败)。
    need_shell = False
    use_unix_bash = False
    if sys.platform == "win32":
        # 1. bash 工具显式要求 unix shell + 检测到 git-bash → 走真 bash
        if _windows_git_bash:
            use_unix_bash = True
            need_shell = True
        elif _ALREADY_CMD_RE.match(cmd_line):
            need_shell = True
        # 2. 含 shell 操作符
        elif any(op in cmd_line for op in _WIN_SHELL_OPERATORS):
            need_shell = True
        # 3. 第一个 token 是 cmd 内置命令
        elif exe in _WIN_BUILTIN_CMDS:
            need_shell = True
        # 4. 裸调 cwd 中的可执行文件:shutil.which 解析失败但 cwd 里存在
        #    (Windows 上 cwd 不在 PATH 搜索范围,所以需要 shell 解析)
        elif (not resolved) and any(
            os.path.isfile(os.path.join(ws_dir, parts[0] + ext))
            for ext in ("", ".exe", ".bat", ".cmd")
        ):
            need_shell = True
    elif prefer_unix_shell:
        # Linux/macOS:bash 工具入口 → 总是走 shell。这样 pipe/redirect/glob/
        # 命令替换 / heredoc 等 unix 语法都能用(exec 模式下不行)。
        # create_subprocess_shell 在 Linux/macOS 默认用 /bin/sh(主流发行版
        # 是 bash 或 dash)。
        need_shell = True

    # 2026-05-08: 记录是否需要 cmd /c exec 路径 (修复 create_subprocess_shell 双包装 bug)
    # shell=True 在 Windows 上自动加 cmd /c,若 cmd_line 已含 cmd /c 则双包装导致命令损坏
    # (如 dir /b → 参数格式不正确 - "b"")。用 create_subprocess_exec 避免此问题。
    _use_cmd_exec = False
    _cmd_sub_command = cmd_line  # cmd /c 后面的子命令

    if use_unix_bash:
        # 真 git-bash 路径 — 用 exec 而不是 shell(避免再包一层 cmd)
        debug.log(
            "workspace.run.bash",
            f"git-bash exec: {cmd_line[:80]} (bash={os.path.basename(_GIT_BASH_EXE)})",
        )
    elif need_shell:
        if _ALREADY_CMD_RE.match(cmd_line):
            # 模型已经写了 cmd /c xxx: 拆出子命令,用 create_subprocess_exec 避免双包装
            _parts = cmd_line.split(None, 2)  # ["cmd", "/c", "dir /b"]
            _cmd_sub_command = _parts[2] if len(_parts) >= 3 else cmd_line
            if len(_cmd_sub_command) >= 2 and _cmd_sub_command[0] == _cmd_sub_command[-1] == '"':
                _cmd_sub_command = _cmd_sub_command[1:-1]
            debug.log("workspace.run.shell", f"already wrapped (exec): {cmd_line[:80]}")
            _use_cmd_exec = True
        else:
            # 用 create_subprocess_exec("cmd", "/c", cmd_line) 避免 create_subprocess_shell 双包装
            _cmd_sub_command = cmd_line
            debug.log("workspace.run.shell", f"win shell (exec): {cmd_line[:80]}")
            _use_cmd_exec = True
        decision2 = analyze_command(cmd_line, ws_dir, is_main_thread=_is_main_thread())
        if not decision2.allowed:
            debug.log("workspace.security", f"shell wrap blocked: {decision2.reason}", {"category": decision2.category})
            return {"ok": False, "error": decision2.reason}

    # ── 执行(子进程启动失败如 FileNotFoundError 会传到 dispatcher 转 error JSON)──
    sandbox_env["PYTHONDONTWRITEBYTECODE"] = "1"
    sandbox_env["PYTHONUNBUFFERED"] = "1"
    # 2026-05-01 修: 强制子进程 stdout/stderr 用 UTF-8。
    # 旧版日志里至少 8 处 UnicodeEncodeError(\u2705/\u2713/emoji),原因是
    # Windows 默认 cp936(GBK)无法编码 emoji,模型每次都要再花一轮迭代去查、
    # 改、重跑。设 PYTHONIOENCODING=utf-8 一次修永久受益。
    sandbox_env["PYTHONIOENCODING"] = "utf-8"
    if "pytest" in cmd_line.lower():
        addopts = sandbox_env.get("PYTEST_ADDOPTS", "").strip()
        if "--rootdir" not in addopts:
            sandbox_env["PYTEST_ADDOPTS"] = (addopts + " --rootdir=.").strip()
    sandbox_env["TEMP"] = ws_dir
    sandbox_env["TMP"] = ws_dir
    sandbox_env["HOME"] = ws_dir
    _mpl_config = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "config", "matplotlib")
    )
    sandbox_env["MPLCONFIGDIR"] = _mpl_config
    # 2026-05-04 v19.2:确保 MPLCONFIGDIR 有 matplotlibrc 配置中文字体。
    # 实测 trace 2661da1f:LLM 写的 chart_generator.py 没自己设
    # `plt.rcParams['font.sans-serif']`,导致中文显示为方框/Tofu。
    # 同时 LLM 还用了 Unicode 上标字符 `n=10⁶`(U+2076),中文字体多数也不含
    # 数学上标 → 显示成 □。
    # 容器层兜底:写一个 matplotlibrc 全局配中文字体优先,即便 LLM 忘了
    # 显式设置也能正确渲染。如果用户已经放了自己的 matplotlibrc 就不覆盖。
    _ensure_matplotlibrc(_mpl_config)
    # ── 2026-05-04 Bug #23 修复:进程树隔离 ──
    # Windows:CREATE_NEW_PROCESS_GROUP 让 taskkill /T 能杀整棵树。
    # POSIX:setsid 创建新会话,os.killpg 杀整组。
    _ws_creationflags = (
        subprocess.CREATE_NEW_PROCESS_GROUP
        if sys.platform == "win32" else 0
    )
    _ws_preexec_fn = os.setsid if sys.platform != "win32" else None

    # 2026-05-15 P87: 预检 + 包 try/except 防 FileNotFoundError 冒泡为 ERROR
    # 病因(实测 23:28 ocr_medicine helper trace):
    #   bash 工具收到命令 (如 'tesseract foo.png' 但 tesseract 未安装),
    #   parts[0] 不是 builtin / 不在 cwd / shutil.which 返 None,
    #   fall through 到裸 create_subprocess_exec → CreateProcess [WinError 2] →
    #   完整 traceback 冒泡到 registry.dispatch, 记 ERROR 日志噪音 + 模型看模糊错误。
    # 修法:
    #   1. 进 create_subprocess_exec 前预检 — parts[0] 既不在 PATH 也不在 cwd
    #      → 早返 clean error, 给可读 hint (不开 subprocess, 不写 ERROR 日志)
    #   2. 任一 subprocess_exec/shell 调用包 try/except FileNotFoundError → clean error
    if not use_unix_bash and not need_shell:
        # 走 raw exec 路径 — parts[0] 必须存在 (PATH 内 / cwd 内)
        _bin_name = parts[0] if parts else ""
        _resolved_now = shutil.which(_bin_name, path=sandbox_env["PATH"])
        _cwd_has_bin = False
        if sys.platform == "win32":
            _cwd_has_bin = any(
                os.path.isfile(os.path.join(ws_dir, _bin_name + ext))
                for ext in ("", ".exe", ".bat", ".cmd")
            )
        else:
            _cwd_has_bin = os.path.isfile(os.path.join(ws_dir, _bin_name)) or (
                _bin_name.startswith("./")
                and os.path.isfile(os.path.join(ws_dir, _bin_name[2:]))
            )
        if not _resolved_now and not _cwd_has_bin:
            # 提前 bail — 不开 subprocess, 不记 ERROR
            _error_text, _fix_hint = _bin_not_found_guidance(_bin_name)
            debug.log(
                "workspace.run.bin_not_found",
                f"P87: '{_bin_name}' 在 PATH 和 cwd 都找不到 → 拒绝 exec",
            )
            return {
                "ok": False,
                "error": _error_text,
                "FIX_HINT": _fix_hint,
            }

    if use_unix_bash:
        # git-bash:用 exec 拼 [bash, -c, cmd_line]。bash.exe 自身处理所有 unix
        # 语法(redirect / pipe / glob / 命令替换 / heredoc 等),cwd 自然继承。
        # 不用 create_subprocess_shell — 那条路径在 Windows 走 cmd.exe。
        try:
            proc = await _create_subprocess_exec_guarded(
                "git-bash exec",
                _GIT_BASH_EXE,
                "-c",
                cmd_line,
                executable=_GIT_BASH_EXE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=ws_dir,
                env=sandbox_env,
                creationflags=_ws_creationflags,
                preexec_fn=_ws_preexec_fn,
            )
            if isinstance(proc, dict):
                return proc
        except FileNotFoundError as e:
            debug.log("workspace.run.exec_failed", f"P87 bash exec FileNotFoundError: {e}")
            return {
                "ok": False,
                "error": f"git-bash execution failed: {e}. Check the git-bash path or whether the command tool is installed.\n检查 git-bash 路径或命令依赖。",
            }
    elif _use_cmd_exec:
        # 2026-05-08: 用 create_subprocess_exec 执行 cmd /c <cmd>,避免 create_subprocess_shell
        # 在 Windows 上加第二层 cmd /c → 双包装损坏命令参数 (如 dir /b → "b"")。
        try:
            proc = await _create_subprocess_exec_guarded(
                "cmd /c",
                "cmd", "/c", _cmd_sub_command,
                executable="cmd",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=ws_dir,
                env=sandbox_env,
                creationflags=_ws_creationflags,
                preexec_fn=_ws_preexec_fn,
            )
            if isinstance(proc, dict):
                return proc
        except FileNotFoundError as e:
            debug.log("workspace.run.exec_failed", f"P87 cmd exec FileNotFoundError: {e}")
            return {
                "ok": False,
                "error": f"cmd /c execution failed: {e}.\ncmd 启动失败。",
            }
    elif need_shell:
        # shell 模式 (非 Windows 或已由 unix-bash 处理)
        try:
            proc = await _create_subprocess_shell_guarded(
                "shell exec",
                cmd_line,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=ws_dir,
                env=sandbox_env,
                creationflags=_ws_creationflags,
                preexec_fn=_ws_preexec_fn,
            )
            if isinstance(proc, dict):
                return proc
        except FileNotFoundError as e:
            debug.log("workspace.run.exec_failed", f"P87 shell exec FileNotFoundError: {e}")
            return {
                "ok": False,
                "error": f"Shell execution failed because the shell executable is missing: {e}.\nshell 程序缺失。",
            }
    else:
        try:
            proc = await _create_subprocess_exec_guarded(
                "raw exec",
                *parts,
                executable=parts[0] if parts else "?",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=ws_dir,
                env=sandbox_env,
                creationflags=_ws_creationflags,
                preexec_fn=_ws_preexec_fn,
            )
            if isinstance(proc, dict):
                proc = await _retry_windows_cmd_after_start_error(
                    proc,
                    cmd_line,
                    ws_dir,
                    sandbox_env,
                    _ws_creationflags,
                    _ws_preexec_fn,
                )
            if isinstance(proc, dict):
                return proc
        except FileNotFoundError as e:
            # 兜底 — 极少触发(上面预检已抓), 万一漏到这也不要冒泡 ERROR
            debug.log("workspace.run.exec_failed", f"P87 raw exec FileNotFoundError: {e}")
            return {
                "ok": False,
                "error": (
                    f"Executable '{parts[0] if parts else '?'}' was not found ({e}). "
                    f"Check spelling or installation/PATH.\n"
                    f"可执行文件未找到。"
                ),
            }

    # ── 注册进 ProcessRegistry(让 LLM 可见 + 可控)──
    # owner 由当前 dispatch 上下文决定:主线程是 "main:trace_id",
    # helper 是 "helper:trace_id:task_id"。LLM 后续可以:
    #   - 调 processes.list_my_processes 看到这个 subprocess
    #   - 调 processes.kill_my_process(proc_id) 主动杀掉(比如发现跑歪了)
    # try/finally 包裹保证任何路径(正常/异常/cancelled)都注销 — 防 leak。
    owner = current_owner()
    proc_id = await proc_registry().register_subprocess(
        owner=owner,
        proc_obj=proc,
        pid=proc.pid,
        command=cmd_line,
        workspace_dir=ws_dir,
    )
    _proc_start_time = time.monotonic()  # 用于 abort 时记录跑了多久

    try:
        # ── 超时决定:LLM 显式指定优先,否则默认 1s(强制 LLM 自决) ──
        # 启发式三档已废弃 — 让 LLM 自己想 "这命令该跑多久"
        if timeout_sec is not None:
            try:
                requested = int(timeout_sec)
            except (TypeError, ValueError):
                requested = _RUN_TIMEOUT_DEFAULT
            timeout = max(_RUN_TIMEOUT_MIN, min(requested, _RUN_TIMEOUT_HARD_CAP))
            timeout_source = "llm"
        else:
            # LLM 没传 → 1s 默认,失败时错误信息会教 LLM 下次传
            timeout = _RUN_TIMEOUT_DEFAULT
            timeout_source = "default_1s"

        try:
            # ── abort_event 支持(Phase 5++ — abort 中途响应修)──
            # 旧版只 wait_for(communicate, timeout) → tool 跑 60s 期间用户 abort 没用。
            # 新版同时监听 abort_event,任一触发即停。
            if abort_event is not None and not abort_event.is_set():
                # 包装成两个 task,谁先完成停手
                comm_task = asyncio.ensure_future(proc.communicate())
                abort_task = asyncio.ensure_future(abort_event.wait())
                try:
                    done, pending = await asyncio.wait(
                        {comm_task, abort_task},
                        timeout=timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if abort_task in done and comm_task not in done:
                        # ── abort 触发,subprocess 还没完 — 立刻杀进程树 ──
                        # Bug #23 修复:taskkill /T 杀子树,防孙子进程孤儿占文件锁
                        comm_task.cancel()
                        try:
                            _kill_process_tree(proc.pid, proc_obj=proc)
                        except (ProcessLookupError, OSError):
                            pass
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=3.0)
                        except (asyncio.TimeoutError, Exception):
                            pass
                        debug.log(
                            "workspace.run.aborted",
                            f"killed pid={proc.pid} due to abort_event "
                            f"(was running {time.monotonic() - _proc_start_time:.1f}s)",
                        )
                        return {
                            "ok": False,
                            "error": (
                                "command aborted by user/system request "
                                "(subprocess killed mid-execution)"
                            ),
                            "aborted": True,
                            "command": cmd_line,
                            "stdout": "", "stderr": "",
                        }
                    if not done:  # 都没完 = 超时
                        comm_task.cancel()
                        abort_task.cancel()
                        raise asyncio.TimeoutError()
                    # comm_task 完成,正常路径
                    abort_task.cancel()
                    stdout_bytes, stderr_bytes = comm_task.result()
                except asyncio.TimeoutError:
                    raise  # 让下面统一处理
                except asyncio.CancelledError:
                    raise
            else:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout,
                )
        except asyncio.TimeoutError:
            # ── 关键修复(trace da389409 helper i 教训)──
            # 旧版:超时后只 return,proc 依然在跑 → 占文件锁 →
            # 后续 gcc -o sorts.exe / del sorts.exe 全部 Permission denied,
            # helper 想 taskkill 又被安全拦截,陷入死循环浪费 6 分钟。
            # ── Bug #23 修复:超时杀进程树(taskkill /T),防孤儿占文件锁 ──
            try:
                _kill_process_tree(proc.pid, proc_obj=proc)
            except (ProcessLookupError, OSError):
                pass  # 进程可能在我们 kill 之前自己退出了
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                # 杀完 wait 还卡 3s 说明系统回收异常,记日志但继续
                log.warning("workspace.run: proc %s did not exit after kill", proc.pid)
            except Exception:
                pass
            # Drain remaining pipe buffers (avoid asyncio "transport closed" warning)
            try:
                if proc.stdout and not proc.stdout.at_eof():
                    proc.stdout.feed_eof()
                if proc.stderr and not proc.stderr.at_eof():
                    proc.stderr.feed_eof()
            except Exception:
                pass

            # 超时提示 — 区分两种场景给不同建议
            # 2026-05-11 B1 改文案: 原版"超过这个值说明任务设计有问题应该拆分"
            # 诱导 helper 怀疑代码而不是 timeout。实测 trace 822f2aaa: rbtree 5 次
            # 10s timeout,每次都改代码再重试,死循环。
            # 新版引导:连续 3 次都改大了 timeout 仍超时才考虑代码问题。
            # 2026-05-11 P7: 根据命令内容给精准推荐(不再泛泛说"benchmark 300s")
            _cmd_lower = cmd_line.lower() if cmd_line else ""
            _suggested_timeout = max(timeout * 5, 60)  # 默认 5x 或 60s
            _cmd_kind = ""
            if any(kw in _cmd_lower for kw in (" bench", "/bench", "_bench", "benchmark", "perf", "stress")):
                _suggested_timeout = 300
                _cmd_kind = "benchmark"
            elif any(kw in _cmd_lower for kw in (" train", "fit_model", "ml ", "tensorflow", "pytorch")):
                _suggested_timeout = 1800
                _cmd_kind = "ML training"
            elif any(kw in _cmd_lower for kw in ("gcc ", "clang ", " make", " cmake", "compile")):
                _suggested_timeout = 60
                _cmd_kind = "compile"
            elif any(kw in _cmd_lower for kw in (" test", "/test", "_test.exe", "ctest", "pytest")):
                _suggested_timeout = 120
                _cmd_kind = "test suite"
            elif any(kw in _cmd_lower for kw in (" install", "pip ", "npm ", "apt ")):
                _suggested_timeout = 180
                _cmd_kind = "package install"
            elif any(kw in _cmd_lower for kw in (".exe", "./")):
                _suggested_timeout = max(60, timeout * 3)
                _cmd_kind = "binary execution"

            if timeout_source == "default_1s":
                hint = (
                    f" — timeout_sec was omitted, so the default 1s budget was used. "
                    f"Retry the same command with an explicit timeout_sec in [1, {_RUN_TIMEOUT_HARD_CAP}]. "
                    "Typical budgets: echo/dir 5s, gcc compile 15-30s, Python scripts 60-180s, "
                    "benchmarks or ML-style runs 300-1800s. The process was killed and workspace locks were released.\n"
                    "未传 timeout_sec 时默认 1s；按任务类型显式提高后重试。"
                )
            else:
                kind_hint = f" This looks like a {_cmd_kind} command." if _cmd_kind else ""
                hint = (
                    f" (timeout_sec={timeout}s). The command may simply need more time; this timeout alone "
                    f"does not prove a code bug.{kind_hint} Retry the same command with timeout_sec={_suggested_timeout}. "
                    f"Suspect an infinite loop, deadlock, or stdin wait only after repeated larger-timeout failures "
                    f"near the hard cap {_RUN_TIMEOUT_HARD_CAP}s. The process was killed and workspace locks were released.\n"
                    "超时本身不等于代码错误；先提高 timeout 重试，连续大超时后再诊断死循环。"
                )
            debug.log(
                "workspace.run.timeout",
                f"killed pid={proc.pid} after {timeout}s ({timeout_source}, proc_id={proc_id})",
            )
            return {
                "ok": False,
                "error": f"command timed out after {timeout}s{hint}",
                "timed_out": True,
                "timeout_used": timeout,
                "proc_id": proc_id,  # LLM 看到这个能确认是哪个进程超的(虽然已注销)
            }

        # 智能解码: cmd.exe 内置命令 / 本地化错误信息在中文 Windows 上是 cp936(GBK),
        # 不是 UTF-8。直接 utf-8 解码会得到 mojibake (如 "'tail'      ڲ ..."),
        # 让模型看不懂错误。先试 utf-8(适用 Python child / chcp 65001 等),
        # 失败 fallback 到 OS preferred encoding (Windows 中文 = cp936)。
        #
        # 2026-05-02 Bug H 修:截断 stdout/stderr 到 _MAX_OUTPUT 字符上限时,
        # **必须告知模型**截断发生了。否则像 trace e4eeb133 中 helper 把输出
        # 重定向到一个 269MB 文件,workspace.run 默默截断,helper 以为输出"短",
        # 在后续 read_file 才发现不完整,浪费 iter。
        # 检测:用字节长度判断(decode 一次的开销不变,但 bytes 长度更精确,
        #       因 UTF-8 字符数 <= 字节数,bytes > _MAX_OUTPUT 必然截断)。
        # 2026-05-02 part10 (A8) 改:截断时**头尾都保留**(各 _MAX_OUTPUT/2),
        # 中间标记 [...truncated N chars...]。原来只保留头会丢失 stack trace 等
        # 关键尾部信息(Python 异常的 \"raise X\" 行、make 错误总结都在末尾)。
        _stdout_decoded = _smart_decode(stdout_bytes)
        _stderr_decoded = _smart_decode(stderr_bytes)
        stdout_truncated = len(_stdout_decoded) > _MAX_OUTPUT
        stderr_truncated = len(_stderr_decoded) > _MAX_OUTPUT
        stdout = _truncate_head_tail(_stdout_decoded, _MAX_OUTPUT) if stdout_truncated else _stdout_decoded
        stderr = _truncate_head_tail(_stderr_decoded, _MAX_OUTPUT) if stderr_truncated else _stderr_decoded

        # B9 修复 (2026-05-02): 编译/链接失败时给智能 fix-it 提示。
        # 实测 trace 150eb2f2 — lite 完全忽略 gcc 报错末尾的 "note: use option -std=c99",
        # 浪费 5 个 iter 把 C 代码改成 C89 兼容,然后才在第 6 次加编译参数。
        # trace 3da78120 — lite 看到 'undefined reference' 反复改源码,真正问题是 gcc
        # 命令缺 .c 文件(linker 找不到)。在工具层提取 hint 让模型一眼看到正解。
        # 字段名用 "FIX_HINT" 全大写,确保 lite 模型也能识别为高优先级提示。
        fix_hint = None
        if proc.returncode != 0:
            fix_hint = _diagnose_build_failure(command, stderr, stdout,
                                                returncode=proc.returncode,
                                                ws_dir=ws_dir)

        # 2026-05-03 Bug 1 修补:Windows 上 prefer_unix_shell 但没装 git-bash 时,
        # bash 命令实际跑在 cmd.exe 下,unix 语法(2>/dev/null / head -N / find / grep
        # / sed / awk / wc)会得到"系统找不到指定的路径"或"不是内部或外部命令"。
        # 给 LLM 一条明确的 hint 让它切 cmd 写法,而不是反复尝试同样的 unix 语法
        # (实测 trace 7e6629f228c84e78 sbt helper 因此被 stuck detector 32s 杀死)。
        if (proc.returncode != 0
                and prefer_unix_shell
                and sys.platform == "win32"
                and not _GIT_BASH_EXE
                and stderr):
            _unix_signals = ("/dev/null", "head", "tail", "grep", "sed", "awk",
                              "find:", "wc:", "xargs", "/dev/")
            _zh_signals = ("系统找不到", "不是内部或外部")
            if (any(s in stderr for s in _unix_signals)
                    or any(s in stderr for s in _zh_signals)):
                _bash_hint = (
                    "git-bash is not available, so bash-style commands are running through cmd.exe and Unix syntax is unavailable. "
                    "Use Windows equivalents: `2>/dev/null` -> `2>nul`, `head -N` -> read_file or `more +0`, "
                    "`find . -name X` -> `dir /s /b X`, `grep PAT FILE` -> `findstr PAT FILE`, "
                    "`grep -rn PAT .` -> `findstr /S /N PAT *`. Prefer search_in_file, search_across_files, or read_file when possible.\n"
                    "无 git-bash 时使用 Windows 命令或专用搜索/读取工具。"
                )
                fix_hint = f"{_bash_hint}\n{fix_hint}" if fix_hint else _bash_hint

        # 2026-05-05: Windows Python GBK/Unicode error → 给具体修复建议
        if (proc.returncode != 0
                and sys.platform == "win32"
                and stderr
                and ("UnicodeDecodeError" in stderr or "UnicodeEncodeError" in stderr)
                and ("'gbk'" in stderr or "'cp936'" in stderr or "'mbcs'" in stderr)):
            _enc_hint = (
                "Windows Python may default to GBK/cp936 and fail on UTF-8 files. Open text with "
                "`encoding='utf-8-sig'` for BOM-prone CSV/Chinese files or `encoding='utf-8'`; configure stdout "
                "with `sys.stdout.reconfigure(encoding='utf-8')` when printing Unicode.\n"
                "Windows Python 编码错误时显式使用 utf-8/utf-8-sig。"
            )
            fix_hint = f"{_enc_hint}\n{fix_hint}" if fix_hint else _enc_hint

        _py_c_hint = _python_c_syntax_fix_hint(command, stderr)
        if _py_c_hint:
            fix_hint = f"{_py_c_hint}\n{fix_hint}" if fix_hint else _py_c_hint
        _unix_inventory_hint = _unix_inventory_syntax_fix_hint(command, stderr, stdout)
        if _unix_inventory_hint:
            fix_hint = f"{_unix_inventory_hint}\n{fix_hint}" if fix_hint else _unix_inventory_hint

        # ── 2026-05-02 part14:stdout 智能摘要(模仿"先看 head/tail + grep 关键词"模式)──
        # 教训(trace 74b1295b iter 89):helper 自加 fprintf DEBUG → stdout 从 700→13K chars,
        # 关键的 PASS/FAIL 总结被淹没,helper 注意力被前部 gcc warning 牵走开始改 unused 函数。
        # 修复方向:
        #   (1) 提取关键测试信号到 result["test_summary"](字段在顶部,模型先看到)
        #   (2) 折叠重复 DEBUG/TRACE 行(同前缀连续 ≥3 次 → "[similar N lines collapsed]")
        # 仅对 returncode==0 但 stdout 较大(>2KB)的输出做处理 — 避免短输出反复额外加工。
        # 失败时 fix_hint / stderr 已经能引导模型,不需要进一步压缩。
        test_summary = _extract_test_summary(stdout, stderr)
        # ── 2026-05-05: 目录列表命令不折叠(stdout 折叠会遮蔽文件名,让 dir/ls 输出作废)──
        _cmd_lower = command.strip().lower()
        _is_dir_listing = (
            _cmd_lower.startswith("dir ") or _cmd_lower == "dir"
            or _cmd_lower.startswith("ls ") or _cmd_lower == "ls"
            or _cmd_lower.startswith("tree ") or _cmd_lower == "tree"
            or "get-childitem" in _cmd_lower or _cmd_lower.startswith("gci ")
            or _cmd_lower == "gci"
        )
        if _is_dir_listing:
            stdout_compacted, n_collapsed = stdout, 0
        else:
            stdout_compacted, n_collapsed = _compact_repeating_lines(stdout)

        result = {
            "ok": proc.returncode == 0,
            "action": "run",
            "command": command,
        }
        # test_summary 放在 result 顶部(returncode/stdout 之前)— 模型从顶部读时
        # 第一眼就看到 PASS/FAIL/Results,不会被 stdout 中部 noise 牵走注意力。
        if test_summary:
            result["test_summary"] = test_summary

        # FIX_HINT 放在 returncode 之前,模型从顶部读到这个字段时还没看到大段
        # stdout/stderr,有更高概率注意到。
        # 1.3: 同一 hint 重复计数 → 第 2 次警告换方向,第 3+ 次要求停手
        if fix_hint:
            _h = hash(fix_hint)
            _fd = _get_fix_hint_dict()
            _fd[_h] = _fd.get(_h, 0) + 1
            _c = _fd[_h]
            if _c >= 3:
                fix_hint = (
                    f"The same fix hint has appeared {_c} times. Stop retrying the identical command; change approach "
                    f"or report the verified blocker.\n同一错误多次出现时换策略或报告阻塞。\n{fix_hint}"
                )
            elif _c == 2:
                fix_hint = (
                    f"This fix hint has already appeared once. If the previous change did not resolve it, change the "
                    f"approach rather than repeating the same repair.\n提示重复时应换方向。\n{fix_hint}"
                )
            result["FIX_HINT"] = fix_hint

        # 2026-05-15 P70: 命令字符串失败重复检测 (与 fix_hint 互补)
        # 多次相同 gcc/cd 命令失败 → 即使没有匹配 fix_hint 也提示停手。
        if proc.returncode != 0:
            _cmd_key = hash(command.strip()[:200])  # 同命令字符串归一
            _bfd = _get_bash_failure_dict()
            _bfd[_cmd_key] = _bfd.get(_cmd_key, 0) + 1
            _fc = _bfd[_cmd_key]
            if _fc >= 3:
                result["_repeated_command_failure"] = (
                    f"The same command signature has failed {_fc} times. Change it before retrying. "
                    "Change parameters, paths, or break it into smaller steps; otherwise report the verified blocker and move on.\n"
                    "同一命令多次失败时改命令、拆步骤，或报告阻塞。"
                )
                result["_repeated_command_count"] = _fc
        else:
            # 成功时清零该命令的失败计数(允许间歇性失败的命令最终成功后不污染后续)
            _cmd_key = hash(command.strip()[:200])
            _bfd = _get_bash_failure_dict()
            if _cmd_key in _bfd:
                _bfd.pop(_cmd_key, None)
        result["returncode"] = proc.returncode
        # 用压缩后的 stdout(重复 DEBUG/TRACE 行已折叠)。原始大小给个数字让模型知道
        # 自己污染了多少。注:test_summary 已在 result 顶部,关键信号不会丢。
        result["stdout"] = stdout_compacted
        if n_collapsed > 0:
            result["stdout_lines_collapsed"] = n_collapsed
            result["stdout_compaction_note"] = (
                f"{n_collapsed} repeated DEBUG/TRACE lines were collapsed. Redirect full stdout to a file and read it "
                "in chunks if needed. Prefer compact trace lines instead of dumping large tables.\n"
                "重复调试输出已折叠；需要完整输出时重定向到文件分段读取。"
            )
        result["stderr"] = stderr
        # 2026-05-02 Bug H 修后半:截断时**显式告知**模型 + 提示如何拿完整输出。
        # 否则像 trace e4eeb133 helper 不知道输出被截,后续 read_file 才发现。
        if stdout_truncated or stderr_truncated:
            _orig_stdout = len(_stdout_decoded) if stdout_truncated else len(stdout)
            _orig_stderr = len(_stderr_decoded) if stderr_truncated else len(stderr)
            _parts = []
            if stdout_truncated:
                _parts.append(
                    f"stdout truncated: kept about {_MAX_OUTPUT//2} chars from both head and tail; full length {_orig_stdout} chars"
                )
            if stderr_truncated:
                _parts.append(
                    f"stderr truncated: kept about {_MAX_OUTPUT//2} chars from both head and tail; full length {_orig_stderr} chars"
                )
            result["output_truncated"] = True
            result["truncation_note"] = (
                "; ".join(_parts) + ". Middle content is marked with [...truncated N chars...]. "
                "For full output, redirect to a file such as `cmd > out.txt 2>&1` and read it in chunks, "
                "or filter the command output to the relevant lines before returning it.\n"
                "输出已截断；完整内容请重定向到文件后分段读取。"
            )
        return result
    except asyncio.CancelledError:
        # 主进程 cancel(用户 abort 等):必须 kill 子进程防僵尸,否则同样占文件锁。
        # 与 timeout 路径同样的清理 — 但不构造 hint(没有意义,主进程已经在退场)。
        try:
            if proc.returncode is None:
                _kill_process_tree(proc.pid, proc_obj=proc)
                await asyncio.wait_for(proc.wait(), timeout=3.0)
        except Exception:
            pass
        raise
    finally:
        # 任何路径都注销 registry — 防 leak
        await proc_registry().unregister(proc_id)
