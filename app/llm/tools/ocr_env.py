"""OCR 桥接的环境/配置/通用工具:环境变量取 int/float/bool、flag 解析、临时 environ 上下文、
子进程 creationflags、文件 sha256、文本规范化。

2026-05-20 重构: 从 llm/tools/ocr_bridge.py 原样抽出。closure 自包含(8 函数, 0 unsafe),
仅依赖 stdlib。ocr_bridge.py re-export 兼容,调用点零改动。
"""
import hashlib
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _temporary_environ(overrides: dict[str, str]):
    old: dict[str, str | None] = {}
    for key, value in overrides.items():
        old[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _subprocess_creationflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _flag(signal: str, **data) -> dict:
    return {"signal": signal, **data}
