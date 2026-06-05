"""
office_render 特征测试。从 office.py 抽出的 LaTeX 文本渲染/预处理族。
依赖 office_latex + office_pptx 的纯 helper(模块级仅 stdlib,可离线 import)。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.tools.office_render import (
    _normalize_latex_for_render,
    _preprocess_simple_latex_macros,
    _try_unicode_render,
    _GREEK_LETTERS,
)


def test_normalize_latex_returns_str():
    assert isinstance(_normalize_latex_for_render("x^2 + 1"), str)


def test_preprocess_simple_macros_returns_str():
    assert isinstance(_preprocess_simple_latex_macros("\\alpha + \\beta"), str)


def test_try_unicode_render_returns_str_or_none():
    out = _try_unicode_render("x^2")
    assert out is None or isinstance(out, str)


def test_greek_letters_mapping_nonempty():
    assert _GREEK_LETTERS  # 非空映射/集合
    blob = repr(_GREEK_LETTERS)
    assert "alpha" in blob or "α" in blob
