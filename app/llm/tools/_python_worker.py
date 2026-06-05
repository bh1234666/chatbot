"""
Python 沙箱 worker。独立进程运行，不被外部代码 import。
通过 stdin 读取代码，stdout 输出单行 JSON 结果。

资源限制：
- CPU 时间 10s
- 地址空间 256MB
- 文件描述符 16

安全策略（宽松，避免阻碍模型正常工作）：
- 不限制 import——资源限制（CPU/内存/时间）是真正的安全边界
- 仅拦截少数真正危险的动态执行：eval/exec/compile/__import__（裸调用）
- 禁止访问下划线开头的属性（dunder/private）
- open/input/breakpoint 禁用

输出（JSON 单行）：
  {"ok": true,  "stdout": "...", "result": "..."}
  {"ok": false, "stdout": "...", "error": "..."}
"""
from __future__ import annotations
import sys
import ast
import json
import io
import contextlib
import builtins
import traceback


DENIED_NAMES = frozenset({
    "eval", "exec", "compile", "__import__",
    "open", "input", "breakpoint",
    "globals", "locals", "vars",
    "exit", "quit", "help",
    "memoryview", "object",
})

# 安全 builtins 白名单（仅这些会出现在沙箱命名空间）
SAFE_BUILTINS = frozenset({
    # 类型
    "bool", "int", "float", "complex", "str", "bytes", "bytearray",
    "list", "tuple", "dict", "set", "frozenset",
    "type", "isinstance", "issubclass",
    # 迭代
    "iter", "next", "range", "enumerate", "zip", "map", "filter",
    "reversed", "sorted",
    # 数值
    "abs", "min", "max", "sum", "round", "pow", "divmod",
    # 输入输出
    "print", "repr", "format", "ascii", "chr", "ord", "hex", "oct", "bin",
    # 集合操作
    "len", "all", "any",
    # 异常
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
    "ArithmeticError", "ZeroDivisionError", "OverflowError",
    "AttributeError", "RuntimeError", "NotImplementedError",
    "StopIteration", "AssertionError",
    # 其他
    "True", "False", "None",
    "hash", "id",
    "callable",
    "slice",
    "property", "staticmethod", "classmethod",
    "super",
})


# ── AST 校验 ────────────────────────────────────────────────
class SecurityError(Exception):
    pass


def validate(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        # 危险名字（裸用）
        if isinstance(node, ast.Name):
            if node.id in DENIED_NAMES:
                raise SecurityError(f"name not allowed: {node.id}")
        # 属性访问：禁止下划线开头（dunder/private）
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise SecurityError(f"attribute not allowed: {node.attr}")


# ── 资源限制 ────────────────────────────────────────────────
def _apply_resource_limits() -> None:
    """Apply CPU/memory/file limits. Best-effort per platform."""
    if sys.platform == "win32":
        _apply_limits_windows()
    else:
        _apply_limits_posix()


def _apply_limits_posix() -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        mem = 256 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
        except (ValueError, OSError):
            pass
    except ImportError:
        pass


def _apply_limits_windows() -> None:
    """Windows: use job object for memory limit (best-effort)."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32

        # Create job object
        hjob = kernel32.CreateJobObjectW(None, None)
        if not hjob:
            return

        # Set memory limit: 256 MB process working set
        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", wintypes.DWORD * 16),   # JOBOBJECT_BASIC_LIMIT_INFORMATION
                ("IoInfo", wintypes.DWORD * 8),                   # IO_COUNTERS 占位
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.ProcessMemoryLimit = 256 * 1024 * 1024
        # JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
        # 2026-05-11 F4 修: BASIC_LIMIT_FLAGS_OFFSET 应为 4 而非 2。
        # JOBOBJECT_BASIC_LIMIT_INFORMATION 结构按 DWORD 索引展开:
        #   [0..1] PerProcessUserTimeLimit (LARGE_INTEGER, 8 bytes)
        #   [2..3] PerJobUserTimeLimit     (LARGE_INTEGER, 8 bytes)
        #   [4]    LimitFlags               (DWORD)  ← 这里
        #   后面是 SIZE_T/ULONG_PTR 等
        # 旧值 2 写到 PerJobUserTimeLimit 中间,LimitFlags 永远 = 0,
        # 即 0x00000100 (JOB_OBJECT_LIMIT_PROCESS_MEMORY) 标志位从未设上,
        # ProcessMemoryLimit 形同摆设,Windows 下沙箱根本不限内存。
        BASIC_LIMIT_FLAGS_OFFSET = 4
        info.BasicLimitInformation[BASIC_LIMIT_FLAGS_OFFSET] = 0x00000100

        job_info_class = 9  # JobObjectExtendedLimitInformation

        kernel32.SetInformationJobObject(
            ctypes.c_void_p(hjob),
            job_info_class,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )

        # Assign current process to job
        kernel32.AssignProcessToJobObject(
            ctypes.c_void_p(hjob),
            kernel32.GetCurrentProcess(),
        )
    except Exception:
        pass  # resource limits are best-effort; parent's wall-clock timeout is the hard cap


# ── 执行 ────────────────────────────────────────────────────
def run() -> None:
    src = sys.stdin.read()

    # 资源限制
    _apply_resource_limits()

    # 清理 lone surrogate 字符（模型偶尔输出），避免 ast.parse 内部 UTF-8 编码报错
    src = src.encode("utf-8", errors="replace").decode("utf-8")

    try:
        tree = ast.parse(src)
        validate(tree)
    except SyntaxError as e:
        _emit_err("", f"SyntaxError: {e}")
        return
    except SecurityError as e:
        # 2026-05-11 实测教训: helper 用 open() 读 CSV 等场景被拒后没换方案
        # → 加 fix_hint 教导改用 workspace.read_file / workspace.write 等
        _hint = ""
        msg = str(e)
        if "open" in msg:
            _hint = (
                "\nFix: tool=python is an isolated calculation sandbox; open() "
                "is separate from workspace files. Use read_file for file reads, "
                "workspace(action='write', path=..., content=...) for writes, or write "
                "a script into the workspace and run it with workspace(action='run')."
                "\nPython 沙箱不能直接访问工作区文件；文件读写请使用 read_file/workspace 工具或工作区脚本。"
            )
        elif "__import__" in msg or "import" in msg.lower():
            _hint = (
                "\nFix: tool=python only allows a limited module set; use workspace(action='run') for broader scripts."
                "\nPython 沙箱只允许有限模块；更完整脚本请用 workspace.run。"
            )
        elif "exec" in msg or "eval" in msg:
            _hint = (
                "\nFix: dynamic exec/eval is blocked in the sandbox; write direct code instead."
                "\n沙箱不执行动态 exec/eval；请改写为直接代码。"
            )
        _emit_err("", f"SecurityError: {e}{_hint}")
        return

    # 拆分最后一个表达式（IPython 风格）
    last_expr_node: ast.Expression | None = None
    body = tree.body
    if body and isinstance(body[-1], ast.Expr):
        last_expr_node = ast.Expression(body=body[-1].value)
        ast.copy_location(last_expr_node, body[-1])
        tree = ast.Module(body=body[:-1], type_ignores=[])

    # 安全 builtins
    safe_bi = {
        n: getattr(builtins, n)
        for n in SAFE_BUILTINS
        if hasattr(builtins, n)
    }
    safe_bi["__import__"] = builtins.__import__

    ns: dict = {"__builtins__": safe_bi, "__name__": "__sandbox__"}

    stdout_buf = io.StringIO()
    result_value = None
    try:
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stdout_buf):
            if tree.body:
                exec(compile(tree, "<sandbox>", "exec"), ns)
            if last_expr_node is not None:
                result_value = eval(
                    compile(last_expr_node, "<sandbox>", "eval"), ns
                )
    except SecurityError as e:
        # 2026-05-11 同样加 fix_hint
        _hint = ""
        msg = str(e)
        if "open" in msg:
            _hint = (
                "\nFix: tool=python is an isolated calculation sandbox; open() "
                "is separate from workspace files. Use read_file/workspace write, or run "
                "a workspace script for file IO."
                "\nPython 沙箱不能直接访问工作区文件；文件读写请使用 read_file/workspace 或工作区脚本。"
            )
        elif "__import__" in msg or "import" in msg.lower():
            _hint = (
                "\nFix: tool=python only allows a limited module set; use workspace(action='run') for broader scripts."
                "\nPython 沙箱只允许有限模块；更完整脚本请用 workspace.run。"
            )
        _emit_err(stdout_buf.getvalue(), f"SecurityError: {e}{_hint}")
        return
    except SystemExit:
        _emit_err(stdout_buf.getvalue(), "SystemExit blocked")
        return
    except BaseException:
        _emit_err(stdout_buf.getvalue(), traceback.format_exc())
        return

    _emit_ok(stdout_buf.getvalue(), result_value)


def _emit_ok(stdout: str, result_value) -> None:
    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__
    payload = {
        "ok": True,
        "stdout": _trim(stdout, 8000),
        "result": _safe_repr(result_value),
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def _emit_err(stdout: str, err: str) -> None:
    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__
    payload = {
        "ok": False,
        "stdout": _trim(stdout, 4000),
        "error": _trim(err, 4000),
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def _safe_repr(v) -> str | None:
    if v is None:
        return None
    try:
        s = repr(v)
    except Exception:
        return "<unrepr-able>"
    return _trim(s, 4000)


def _trim(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 3] + "..."


if __name__ == "__main__":
    run()
