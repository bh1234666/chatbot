"""
ocr_env 特征测试。从 ocr_bridge.py 抽出的环境/配置/工具 helper,仅依赖 stdlib。
含 _temporary_environ 上下文管理器(回归:抽取曾丢失 @contextmanager,已修复并锁定)。
可离线运行。
"""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.tools.ocr_env import (
    _env_int,
    _env_float,
    _env_bool,
    _temporary_environ,
    _file_sha256,
    _normalize_text,
)


def test_env_int_parses_and_defaults():
    os.environ["_OCR_T_INT"] = "42"
    assert _env_int("_OCR_T_INT", 0) == 42
    assert _env_int("_OCR_T_MISSING_XYZ", 7) == 7


def test_env_float_parses():
    os.environ["_OCR_T_FLOAT"] = "1.5"
    assert _env_float("_OCR_T_FLOAT", 0.0) == 1.5


def test_env_bool_parses():
    os.environ["_OCR_T_BOOL"] = "1"
    assert _env_bool("_OCR_T_BOOL", False) is True


def test_temporary_environ_is_context_manager():
    os.environ.pop("_OCR_T_CTX", None)
    with _temporary_environ({"_OCR_T_CTX": "99"}):
        assert os.environ.get("_OCR_T_CTX") == "99"
    # 退出后还原(原本不存在 → 应被移除)
    assert os.environ.get("_OCR_T_CTX") is None


def test_file_sha256():
    import tempfile
    d = Path(tempfile.mkdtemp())
    fp = d / "x.txt"
    fp.write_bytes(b"hello")
    digest = _file_sha256(fp)
    assert isinstance(digest, str) and len(digest) >= 16


def test_normalize_text_returns_str():
    assert isinstance(_normalize_text("  a  b  "), str)
