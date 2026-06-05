"""
Python 沙箱真实运行测试。

不依赖外部库，直接用 asyncio 跑子进程。
覆盖：
- 正常计算
- 最后表达式 / print / 多行
- 安全限制（动态执行拦截、dunder、危险内置；import 不限制，资源限制是安全边界）
- 资源/超时
- 语法错误
- 异常
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.tools.python_exec import run_python


def _safe_print(*args, **kwargs):
    """print that won't crash on GBK consoles."""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        # encode non-ASCII away
        safe_args = [str(a).encode("ascii", errors="replace").decode("ascii") for a in args]
        print(*safe_args, **kwargs)


async def expect_ok(name, code, *, contains_result=None, contains_stdout=None):
    r = await run_python(code)
    if not r.get("ok"):
        _safe_print(f"[FAIL] {name}: expected ok, got {r}")
        return False
    if contains_result is not None and contains_result not in (r.get("result") or ""):
        _safe_print(f"[FAIL] {name}: result={r.get('result')!r} should contain {contains_result!r}")
        return False
    if contains_stdout is not None and contains_stdout not in r.get("stdout", ""):
        _safe_print(f"[FAIL] {name}: stdout={r.get('stdout')!r} should contain {contains_stdout!r}")
        return False
    _safe_print(f"[OK]   {name}")
    return True


async def expect_fail(name, code, *, error_contains=None):
    r = await run_python(code)
    if r.get("ok"):
        _safe_print(f"[FAIL] {name}: expected failure, got ok with result={r.get('result')!r}")
        return False
    err = r.get("error", "")
    if error_contains is not None and error_contains not in err:
        _safe_print(f"[FAIL] {name}: error={err!r} should contain {error_contains!r}")
        return False
    _safe_print(f"[OK]   {name} ({error_contains!r})")
    return True


async def main():
    results = []

    # ── 正常计算 ──
    results.append(await expect_ok(
        "simple arithmetic", "1 + 2 * 3", contains_result="7"))
    results.append(await expect_ok(
        "math import", "import math\nmath.sqrt(2)",
        contains_result="1.4142"))
    results.append(await expect_ok(
        "json", 'import json\njson.dumps({"a": [1,2,3]})',
        contains_result='"a"'))
    results.append(await expect_ok(
        "regex", 'import re\nre.findall(r"\\d+", "a1b22c333")',
        contains_result="['1', '22', '333']"))
    results.append(await expect_ok(
        "datetime", 'import datetime\ndatetime.date(2026, 1, 1).weekday()',
        contains_result="3"))  # Jan 1, 2026 is Thursday (weekday=3)
    results.append(await expect_ok(
        "collections counter",
        'from collections import Counter\nCounter("aabbbcccc").most_common(2)',
        contains_result="('c', 4)"))
    results.append(await expect_ok(
        "stdout via print",
        'print("hello")\nprint(1+1)',
        contains_stdout="hello\n2\n"))
    results.append(await expect_ok(
        "complex computation",
        'def fib(n):\n  a,b=0,1\n  for _ in range(n): a,b=b,a+b\n  return a\nfib(20)',
        contains_result="6765"))

    # ── 安全限制（动态执行/dunder/open 仍拦截） ──
    # 注：import 不限制，安全边界是资源限制（CPU/内存/timeout）
    results.append(await expect_ok(
        "import os allowed", "import os\nos.path.join('a','b')",
        contains_result="'a"))
    results.append(await expect_ok(
        "import subprocess allowed", "import subprocess\n'ok'",
        contains_result="ok"))
    results.append(await expect_ok(
        "import socket allowed", "import socket\n'ok'",
        contains_result="ok"))
    results.append(await expect_ok(
        "import urllib allowed", "import urllib\n'ok'",
        contains_result="ok"))
    results.append(await expect_ok(
        "from os import allowed", "from os import path\npath.join('a','b')",
        contains_result="'a"))
    results.append(await expect_fail(
        "open file", 'open("/etc/passwd").read()',
        error_contains="open"))
    results.append(await expect_fail(
        "eval", 'eval("1+1")',
        error_contains="eval"))
    results.append(await expect_fail(
        "exec", 'exec("x=1")',
        error_contains="exec"))
    results.append(await expect_fail(
        "compile", 'compile("1", "<x>", "eval")',
        error_contains="compile"))
    results.append(await expect_fail(
        "__import__ direct call", '__import__("os")',
        error_contains="__import__"))
    results.append(await expect_fail(
        "dunder access", '().__class__.__bases__',
        error_contains="attribute not allowed"))
    results.append(await expect_fail(
        "getattr dunder via attribute syntax",
        'x = []; x.__class__',
        error_contains="attribute not allowed"))
    results.append(await expect_fail(
        "syntax error", "1 + ",
        error_contains="SyntaxError"))
    results.append(await expect_fail(
        "div by zero", "1/0",
        error_contains="ZeroDivisionError"))

    # ── 边界场景 ──
    results.append(await expect_fail(
        "infinite loop (CPU limit)",
        'while True: pass',
        # CPU rlimit 触发后会返回非 ok；具体错误信息因平台差异
    ))

    # 大输出截断
    try:
        r = await run_python('print("x" * 100000)')
        assert r.get("ok"), f"large output should still ok, got {r}"
        assert len(r.get("stdout", "")) <= 8200, f"stdout should be trimmed, got {len(r['stdout'])}"
        _safe_print("[OK]   large stdout trimmed")
    except UnicodeEncodeError:
        _safe_print("[OK]   large stdout trimmed (encoding skip)")

    # numpy（若安装则可用）
    r = await run_python("import numpy as np\nnp.array([1,2,3]).sum()")
    if r.get("ok"):
        _safe_print(f"[OK]   numpy available: result={r.get('result')}")
    else:
        # numpy 没装也算正常，确保是 ImportError 而非 SecurityError
        if "import not allowed" in r.get("error", ""):
            _safe_print(f"[FAIL] numpy import was blocked!")
            results.append(False)
        else:
            _safe_print(f"[OK]   numpy not installed (expected import error)")

    failed = [r for r in results if r is False]
    _safe_print(f"\n=== sandbox tests: {len(results) - len(failed)}/{len(results)} passed ===")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
