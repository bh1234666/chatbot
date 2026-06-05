"""
workspace_text 特征测试。从 workspace.py 抽出的结果/提示文本 helper,仅依赖 stdlib(os/re)。
可离线运行。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.tools.workspace_text import (
    _same_pattern,
    _extract_test_summary,
    _helper_missing_file_fetch_hint,
    _structured_read_file_rejection,
)


def test_same_pattern_identical():
    assert _same_pattern("ls -la", "ls -la") is True


def test_same_pattern_differs():
    assert isinstance(_same_pattern("ls -la", "rm -rf /"), bool)


def test_extract_test_summary_returns_str_or_none():
    out = _extract_test_summary("3 passed, 1 failed", "")
    assert out is None or isinstance(out, str)


def test_extract_test_summary_recognizes_quiet_pytest_success():
    out = _extract_test_summary(". [100%]\n1 passed in 0.01s\n", "")
    assert out == "[pytest] 1 passed in 0.01s"


def test_helper_missing_file_fetch_hint_returns_str():
    hint = _helper_missing_file_fetch_hint("/tmp/ws", "missing.docx")
    # 仅在特定条件下给出非空提示;此处只断言返回类型稳定为 str
    assert isinstance(hint, str)


def test_helper_missing_nested_path_points_to_result_maps():
    hint = _helper_missing_file_fetch_hint(
        "/tmp/ws/.temp/_delegate_user_benchmark",
        "_env/implementations/rb_tree/rb_tree.py",
    )
    assert "file_map" in hint
    assert "main_available_files" in hint
    assert "_helpers_shared" in hint


def test_structured_read_file_rejection_returns_dict_or_none():
    out = _structured_read_file_rejection("data.bin")
    assert out is None or isinstance(out, dict)
