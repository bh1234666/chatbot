"""Helper system-prompt construction: platform, hardware, and shell hints.

Extracted from llm/tools/delegate.py as pure prompt-building helpers. The
functions have no side effects beyond local environment inspection, and
delegate.py re-exports them for compatibility.
"""
import os
import platform as _platform
import subprocess as _subprocess
import sys as _sys

# Process-local memoized hardware facts used in helper system prompts.
_HARDWARE_INFO: str | None = None


def _get_hardware_info() -> str:
    global _HARDWARE_INFO
    if _HARDWARE_INFO is not None:
        return _HARDWARE_INFO

    lines: list[str] = []
    try:
        cpu_count = os.cpu_count() or "unknown"
        lines.append(f"- CPU logical cores: {cpu_count}")

        if _sys.platform == "win32":
            # PowerShell 获取 CPU 名称 + 总 RAM
            # 注意: 1GB=1073741824 bytes, 1MB=1048576 bytes (PowerShell 常数)
            ps_script = (
                "$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1;"
                "$ram = Get-CimInstance Win32_ComputerSystem;"
                "Write-Host \"CPU=$($cpu.Name)\";"
                "Write-Host \"RAM_GB=$([math]::Round($ram.TotalPhysicalMemory/1GB))\""
            )
            r = _subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                for line in r.stdout.strip().splitlines():
                    line = line.strip()
                    if line.startswith("CPU="):
                        cpu_name = line[4:]
                        lines.append(f"- CPU: {cpu_name}")
                    elif line.startswith("RAM_GB="):
                        ram_gb = int(line[7:])
                        lines.append(f"- Memory: {ram_gb} GB")
        else:
            # Linux: /proc/cpuinfo + /proc/meminfo
            try:
                cpu_info = _subprocess.run(
                    ["grep", "-m1", "model name", "/proc/cpuinfo"],
                    capture_output=True, text=True, timeout=5,
                )
                if cpu_info.returncode == 0 and cpu_info.stdout.strip():
                    name = cpu_info.stdout.strip().split(":", 1)[-1].strip()
                    lines.append(f"- CPU: {name}")
            except Exception:
                pass
            try:
                mem_info = _subprocess.run(
                    ["grep", "MemTotal", "/proc/meminfo"],
                    capture_output=True, text=True, timeout=5,
                )
                if mem_info.returncode == 0:
                    lines.append(f"- {mem_info.stdout.strip()}")
            except Exception:
                pass

        # 兜底: platform 模块
        if not any("CPU:" in l for l in lines):
            lines.append(f"- CPU: {_platform.processor() or 'unknown'}")
        lines.append(f"- Architecture: {_platform.machine() or 'unknown'}")
        lines.append(f"- System: {_platform.system()} {_platform.release()}")

    except Exception:
        # 完全降级: 只用 platform 模块
        try:
            lines.append(f"- CPU: {_platform.processor() or 'unknown'}")
            lines.append(f"- Architecture: {_platform.machine() or 'unknown'}")
            lines.append(f"- System: {_platform.system()} {_platform.release()}")
            if os.cpu_count():
                lines.append(f"- CPU logical cores: {os.cpu_count()}")
        except Exception:
            lines.append("- Hardware information unavailable")

    _HARDWARE_INFO = "\n".join(lines)
    return _HARDWARE_INFO


def _build_platform_hint() -> str:
    hw = _get_hardware_info()
    python_snippet_hint = (
        "- Python snippets: use `python -c` only for short one-line expressions. "
        "For multiline code, nested quotes, or f-strings, write a `.py` file "
        "with workspace.write/edit_file, then run `python script.py`.\n"
        "- The `python` tool is an isolated calculation sandbox: keep file IO in "
        "workspace tools. Read/write workspace files with read_file/edit_file/"
        "workspace.write; use workspace.run for scripts that need file IO.\n"
        "Python 简短表达式可用 `python -c`；多行脚本和文件读写放到工作区脚本或 workspace 工具中执行。\n"
    )

    if _sys.platform != "win32":
        return (
            "## Runtime Platform\n"
            "- You are running on a Unix-like system, as is the main process.\n"
            "- Run compiled binaries with `bash(\"./name\")`; executables normally have no `.exe` suffix.\n"
            "- Standard Unix shell tools are available, including grep, sed, awk, find, xargs, head, tail, and wc.\n"
            f"{python_snippet_hint}"
            "\n"
            "## Hardware Evidence\n"
            "Use these local hardware facts exactly when a report, paper, or benchmark description needs environment details.\n"
            f"{hw}\n"
            "\n平台为 Unix-like；命令和硬件信息必须按实测环境表述。\n"
        )
    # Windows — 看 git-bash 是否检测到
    try:
        from app.llm.tools.workspace import has_unix_shell
        unix_ok = has_unix_shell()
    except Exception:
        unix_ok = False

    if unix_ok:
        return (
            "## Runtime Platform\n"
            "- You are running on Windows, as is the main process.\n"
            "- The `bash` tool is backed by Git Bash, so Unix shell commands, pipes, and redirection work.\n"
            "- Run compiled binaries with `bash(\"./name.exe\")` or `bash(\"name.exe\")`.\n"
            "- Prefer `/` as the path separator because Python and gcc both accept it on this host.\n"
            f"{python_snippet_hint}"
            "- For MinGW gcc portability: print size_t with `%lu` plus `(unsigned long)`, print int64_t with `PRId64` plus `#include <inttypes.h>`, and declare C89 loop variables at block start.\n"
            "- For process memory on Windows, use GlobalMemoryStatusEx or GetProcessMemoryInfo rather than Unix-only process files or APIs.\n"
            "\n"
            "## Hardware Evidence\n"
            "Use these local hardware facts exactly when a report, paper, or benchmark description needs environment details.\n"
            f"{hw}\n"
            "\n平台为 Windows + Git Bash；路径、编译和内存测量按 Windows 事实处理。\n"
        )
    # Windows + 没装 git-bash:cmd.exe 实情如实告知
    return (
        "## Runtime Platform\n"
        "- You are running on Windows, as is the main process.\n"
        "- Git Bash was not detected. The `bash` tool is effectively cmd.exe, so Unix-only shell syntax and tools are not available.\n"
        "- Use dedicated file tools for discovery and editing: workspace locate/list, search_files, search_in_file, search_across_files, read_file, edit_file, and workspace.write. Do not use `ls`, `find`, `grep`, `head`, `tail`, `wc`, or heredocs for directory inventory unless Git Bash is explicitly available.\n"
        "- For command output redirection, use Windows forms such as `2>nul`. `/dev/null`, heredocs, command substitution, `export`, and Unix quoting patterns do not apply.\n"
        "- Run compiled binaries with `bash(\"name.exe\")` or `bash(\"cmd /c name.exe\")`.\n"
        "- For MinGW gcc portability: print size_t with `%lu` plus `(unsigned long)`, print int64_t with `PRId64` plus `#include <inttypes.h>`, and declare C89 loop variables at block start.\n"
        "- For process memory on Windows, use GlobalMemoryStatusEx or GetProcessMemoryInfo rather than Unix-only process files or APIs.\n"
        "- When Python reads UTF-8 or BOM-marked Chinese text on Windows, pass `encoding='utf-8-sig'` or another explicit encoding that matches the file.\n"
        f"{python_snippet_hint}"
        "\n"
        "## Hardware Evidence\n"
        "Use these local hardware facts exactly when a report, paper, or benchmark description needs environment details.\n"
        f"{hw}\n"
        "\n平台为 Windows + cmd.exe；目录和文件盘点优先 workspace/search/read/edit 等专用工具，不使用 Unix-only 命令；编码和命令语法按 Windows 处理。\n"
    )


# 模块导入时调用一次。注意:这里用函数返回再赋值,而不是在模块作用域写
# `_sys.platform == "win32"` if-else,因为 has_unix_shell() 需要 workspace
# 模块完成 git-bash 检测,有依赖顺序问题。函数延迟到首次访问。
# 2026-05-21: ASan 可用性条件化提示。病因见 process_utils._detect_asan_support。
# helper prompt 多处硬编码"默认带 -fsanitize=address",但 Windows+MinGW 缺 libasan →
# `cannot find -lasan` 链接失败反复撞墙。这里据实给出可用/替代方案。
def _build_asan_hint() -> str:
    try:
        from app.llm.tools.workspace import has_asan
        asan_ok = has_asan()
    except Exception:
        asan_ok = False
    if asan_ok:
        return (
        "## C Debugging And Benchmark Discipline\n"
        "- AddressSanitizer is available on this host. For memory bugs, compile a small debug build with `gcc -g -O0 -fsanitize=address -fno-omit-frame-pointer x.c -o x`.\n"
        "- Validate correctness on small ASan runs before large benchmarks. Remove sanitizers for final timing only after the debug build is clean.\n"
        "- Estimate complexity and operation counts before running performance experiments. Use probes and per-algorithm input limits for quadratic or exponential methods; timeouts are safeguards, not a planning strategy.\n"
        "\n本机可用 ASan；先小规模验证正确性，再做受控性能实验。\n"
    )
    return (
        "## C Debugging And Benchmark Discipline\n"
        "- AddressSanitizer is not available on this host because gcc cannot link libasan in the current environment.\n"
        "- Use portable alternatives: `-g -O0 -Wall -Wextra`, UBSan when available, assertions, boundary checks, small reproducible cases, and focused diagnostic output.\n"
        "- Validate correctness on small inputs before large runs.\n"
        "- Estimate complexity and operation counts before running performance experiments. Use probes and per-algorithm input limits for quadratic or exponential methods; timeouts are safeguards, not a planning strategy.\n"
        "\n本机不可用 ASan；使用 warning、UBSan、断言和小规模复现来调试。\n"
    )


_PLATFORM_HINT = _build_platform_hint()
_ASAN_HINT = _build_asan_hint()


# 2026-05-03 Bug 1 修:bash 速查也要平台条件化,否则在 Windows + 无 git-bash
# 时下面这段硬编码的 grep/sed/find 例子会反复诱导模型撞墙。
def _build_bash_examples_block() -> str:
    """Return the platform-specific bash quick-reference block."""
    if _sys.platform != "win32":
        unix_ok = True
    else:
        try:
            from app.llm.tools.workspace import has_unix_shell
            unix_ok = has_unix_shell()
        except Exception:
            unix_ok = False

    if unix_ok:
        return (
            "## bash Tool\n"
            "Use bash for real shell work such as compiling, running tests, git commands, and pipelines. Use dedicated workspace tools for file discovery, reading, and editing.\n"
            "- Compile and run a built binary in one command, then filter large output through standard pipe utilities before reading it.\n"
            "- Loop over matching source files for batch operations when a single command must touch many files.\n"
            "\n文件搜索、查看和编辑使用专用工具；bash 只处理真实命令执行。\n"
        )
    return (
        "## bash Tool On This Host\n"
        "Git Bash is not installed, so bash commands run through cmd.exe. Use bash only for commands that are valid in this Windows command environment, and use dedicated workspace tools for file discovery, reading, and editing.\n"
        "- Compile to a `.exe` and run it in one command using cmd-valid syntax.\n"
        "- Redirect large output to a file with Windows redirection forms, then inspect the file with read_file.\n"
        "\n本机 bash 实际为 cmd.exe；文件搜索、查看和编辑使用专用工具。\n"
    )

