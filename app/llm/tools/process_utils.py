"""进程与平台 helper:git-bash 探测、进程树终止。

2026-05-20 重构: 从 llm/tools/workspace.py 原样抽出。closure 自包含(2 函数, 0 unsafe),
仅依赖 stdlib(os/shutil/signal/subprocess/sys)。workspace.py re-export 兼容。
"""
import os
import shutil
import signal
import subprocess
import sys


def _detect_git_bash() -> str | None:
    """启动时检测 git-bash / MSYS2 bash 可执行路径。Windows 用,Linux/macOS 跳过。

    返回绝对路径(找到)或 None(未找到,bash 工具会 fallback 到 cmd.exe 并
    在 prompt 里告诉 LLM 没有 unix 工具)。
    """
    if sys.platform != "win32":
        return None
    # 1) PATH 上的 bash
    p = shutil.which("bash")
    # PATH 上的 bash 必须排除 WSL 的 bash.exe(C:\Windows\System32\bash.exe)——
    # 它会启动 WSL,工作区路径是 Windows 风格 cwd 在 WSL 里看不到,反而更难用。
    if p and os.path.isfile(p):
        # WSL bash 一般在 System32 下,排除掉
        norm = p.replace("\\", "/").lower()
        if "system32/bash.exe" not in norm and "/syswow64/bash.exe" not in norm:
            return os.path.abspath(p)
    # 2) 常见 git-bash 安装位置
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    candidates = [
        os.path.join(pf, "Git", "bin", "bash.exe"),
        os.path.join(pf, "Git", "usr", "bin", "bash.exe"),
        os.path.join(pf86, "Git", "bin", "bash.exe"),
        os.path.join(pf86, "Git", "usr", "bin", "bash.exe"),
        r"C:\msys64\usr\bin\bash.exe",
        r"C:\msys2\usr\bin\bash.exe",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


# ── 2026-05-21: AddressSanitizer 可用性探测 ──
# 病因(实测 trace c6e42ed6 / debug_20260521_151510):helper system prompt 多处硬编码
# "C 代码默认带 -fsanitize=address",但运行环境是 Windows + MinGW,默认不带 libasan
# → 所有听话用 ASan 的 helper 编译必然 `cannot find -lasan` 链接失败(实测 8 次),反复撞墙。
# 修法:启动时实际编一个最小程序探测 gcc 能否真正用 -fsanitize=address 并链接成功,
# 公开 detect_asan_support() 让 prompt 据实反映能力(不能用 ASan 时给替代调试方案)。
def _detect_asan_support() -> bool:
    """探测当前环境的 gcc/clang 是否真能用 -fsanitize=address 编译并链接。

    实际编译一个最小 C 程序(而非只查 gcc 是否存在),因为 MinGW 默认有 gcc 但缺 libasan。
    返回 True=可用 / False=不可用(缺 libasan / 无编译器 / 探测失败)。结果在模块加载时缓存一次。
    """
    cc = shutil.which("gcc") or shutil.which("cc") or shutil.which("clang")
    if not cc:
        return False
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as _td:
            src = os.path.join(_td, "_asan_probe.c")
            out = os.path.join(_td, "_asan_probe.exe" if sys.platform == "win32" else "_asan_probe")
            with open(src, "w", encoding="utf-8") as f:
                f.write("int main(void){return 0;}\n")
            # 必须同时编译 + 链接(-lasan 缺失只在链接期暴露)
            proc = subprocess.run(
                [cc, "-fsanitize=address", "-g", "-O0", src, "-o", out],
                capture_output=True, timeout=30,
            )
            return proc.returncode == 0
    except Exception:
        return False
# proc.kill() 只杀直系进程。Windows 上 cmd.exe 包装的子进程链
# (cmd → gcc → ld.exe, cmd → python → benchmark.exe) 会成为孤儿继续持有文件锁,
# 导致后续编译 Permission denied。taskkill /T 杀子树,POSIX 用 killpg。
def _kill_process_tree(pid: int, *, proc_obj=None) -> None:
    """Kill a process and all its descendants."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        except Exception:
            try:
                if proc_obj is not None:
                    proc_obj.kill()
            except (ProcessLookupError, OSError):
                pass
    else:
        # POSIX: kill process group
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                if proc_obj is not None:
                    proc_obj.kill()
            except (ProcessLookupError, OSError):
                pass
