"""
helper_text 特征测试。从 delegate.py 抽出的 helper 纯文本工具,无 import 依赖。可离线运行。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.tools.helper_text import (
    _prompt_similarity,
    _infer_expected_outputs_from_prompt,
    _helper_lang_hint,
    _PRODUCT_FILE_EXTS,
)


def test_prompt_similarity_range_and_self():
    assert _prompt_similarity("abc def", "abc def") >= _prompt_similarity("abc def", "abc xyz")
    s = _prompt_similarity("abc def", "abc xyz")
    assert isinstance(s, float) and 0.0 <= s <= 1.0


def test_infer_expected_outputs_picks_filename():
    out = _infer_expected_outputs_from_prompt("帮我写一个 report.docx 报告")
    assert isinstance(out, list)
    assert "report.docx" in out


def test_infer_expected_outputs_keeps_environment_paths():
    out = _infer_expected_outputs_from_prompt(
        "Edit `_env/src/taskboard/models.py` and `_env/tests/test_models.py`; "
        "deliverables: `_env/src/taskboard/models.py`, `_env/tests/test_models.py`."
    )
    assert "_env/src/taskboard/models.py" in out
    assert "_env/tests/test_models.py" in out


def test_infer_expected_outputs_empty_when_none():
    out = _infer_expected_outputs_from_prompt("随便聊聊天气")
    assert isinstance(out, list)


def test_helper_lang_hint_returns_str():
    assert isinstance(_helper_lang_hint("用中文回答"), str)


def test_product_file_exts_is_collection():
    assert _PRODUCT_FILE_EXTS  # 非空
    # 元组内为无点扩展名(如 'docx' / 'py')
    assert "docx" in _PRODUCT_FILE_EXTS
    assert "py" in _PRODUCT_FILE_EXTS
