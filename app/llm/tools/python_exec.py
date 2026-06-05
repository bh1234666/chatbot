"""
Python 沙箱异步执行器。

职责：
- 启动 _python_worker.py 子进程
- 通过 stdin 喂代码，stdout 拿结果
- 设置墙钟超时（CPU 限制由 worker 内部 setrlimit 处理；这里加墙钟兜底）
- 空环境（PATH/PYTHONPATH 全清）

注意：
- worker 输出的 JSON 在单行 stdout 中
- AST 校验在 worker 内做（双重防御：调用方也可在外层做一次预检以快速失败）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


_WORKER_PATH = Path(__file__).parent / "_python_worker.py"

# 平台无关的临时目录（Windows 不能用 /tmp，且 /tmp 不存在时会抛 FileNotFoundError）
_SANDBOX_CWD = tempfile.gettempdir() if sys.platform == "win32" else "/tmp"

WALL_CLOCK_SEC = 12  # CPU 限 10s，墙钟略宽
MAX_OUTPUT_BYTES = 64 * 1024


async def run_python(code: str) -> dict[str, Any]:
    """
    在沙箱子进程执行 Python 代码。
    返回：
      成功:  {"ok": True,  "stdout": str, "result": str | None}
      失败:  {"ok": False, "stdout": str, "error": str}
    """
    # 启动子进程：空环境，禁站点初始化（-I = isolated mode）
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",                       # isolated: 不读 PYTHONPATH，不读用户 site
        "-S",                       # 不自动 import site
        str(_WORKER_PATH),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            "PATH": "",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            # Generic code execution is CPU-only. Dedicated OCR/MinerU tools use
            # separate subprocess environments and are not affected by this sandbox.
            "CUDA_VISIBLE_DEVICES": "",
            "NVIDIA_VISIBLE_DEVICES": "none",
            "HIP_VISIBLE_DEVICES": "",
            "ROCR_VISIBLE_DEVICES": "",
            "PYTORCH_ENABLE_MPS_FALLBACK": "0",
            "MINERU_DEVICE_MODE": "cpu",
            # 2026-05-17 P155: Windows GBK 默认会让 emoji/\u2705/\u2713 等
            # 触发 UnicodeEncodeError, 即便父进程是 utf-8 子进程也用 cp936.
            # 强制 PYTHONIOENCODING=utf-8 让 sys.stdout/stderr 用 utf-8.
            "PYTHONIOENCODING": "utf-8",
            # 不继承任何环境变量
            "LANG": "C.UTF-8",
        },
        # 切到一个不存在敏感文件的目录
        cwd=_SANDBOX_CWD,
    )

    try:
        # Sanitize: replace lone surrogates (LLM sometimes emits broken Unicode like \udc87)
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=code.encode("utf-8", errors="replace")),
            timeout=WALL_CLOCK_SEC,
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass
        return {
            "ok": False,
            "stdout": "",
            "error": f"timeout: code did not finish within {WALL_CLOCK_SEC}s",
        }

    out = stdout_b[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    err = stderr_b[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")

    # worker 用单行 JSON 输出。但 stderr/异常崩溃时 out 可能为空。
    if not out.strip():
        return {
            "ok": False,
            "stdout": "",
            "error": f"worker produced no output. exit={proc.returncode}\nstderr: {err[:1000]}",
        }
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # worker 崩溃中途，输出不是合法 JSON
        return {
            "ok": False,
            "stdout": out[:2000],
            "error": f"worker output not JSON. exit={proc.returncode}\nstderr: {err[:1000]}",
        }
