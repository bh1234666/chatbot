"""
code_outline 特征测试。从 workspace.py 抽出的代码大纲/文本读取工具,仅依赖 stdlib(re)。
断言基于函数当前真实行为。可离线运行。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.tools.code_outline import (
    _smart_decode,
    _truncate_head_tail,
    _tokenize_path,
    _extract_c_outline,
    _extract_generic_outline,
)


def test_smart_decode_utf8():
    assert _smart_decode(b"hello") == "hello"
    assert _smart_decode("中文".encode("utf-8")) == "中文"


def test_truncate_head_tail_short_passthrough():
    s = "abc"
    assert _truncate_head_tail(s, 100) == s


def test_truncate_head_tail_long_shrinks():
    s = "x" * 1000
    out = _truncate_head_tail(s, 100)
    assert isinstance(out, str)
    assert len(out) < len(s)


def test_tokenize_path_splits():
    toks = _tokenize_path("src/main.py")
    assert isinstance(toks, list)
    assert any("main" in t for t in toks)


def test_extract_c_outline_returns_dict():
    code = "int add(int a, int b) {\n    return a + b;\n}\n"
    out = _extract_c_outline(code)
    assert isinstance(out, dict)


def test_extract_generic_outline_returns_dict():
    code = "def foo():\n    pass\n\ndef bar():\n    pass\n"
    out = _extract_generic_outline(code, language_hint="python")
    assert isinstance(out, dict)
