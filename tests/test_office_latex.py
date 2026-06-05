"""
office_latex 特征测试。从 office.py 抽出的 LaTeX/数学公式处理族,仅依赖 stdlib(re)。
可离线运行。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.tools.office_latex import (
    _latex_contains_cjk,
    _classify_latex_complexity,
    _strip_cjk_text_commands,
    _rewrite_choose_to_binom,
    _read_brace_group,
)


def test_latex_contains_cjk():
    assert _latex_contains_cjk("中文 x^2") is True
    assert _latex_contains_cjk("x^2 + 1") is False


def test_classify_latex_complexity_returns_tuple():
    kind, _detail = _classify_latex_complexity("\\frac{1}{2}")
    assert isinstance(kind, str)


def test_strip_cjk_text_commands_returns_tuple():
    stripped, cjk = _strip_cjk_text_commands("\\text{中文} x")
    assert isinstance(stripped, str)
    assert isinstance(cjk, list)
    assert "中文" in cjk


def test_rewrite_choose_to_binom_returns_str():
    out = _rewrite_choose_to_binom("n \\choose k")
    assert isinstance(out, str)


def test_read_brace_group_parses_balanced():
    # {abc}def → 读出第一个花括号组
    res = _read_brace_group("{abc}def", 0)
    assert res is not None
