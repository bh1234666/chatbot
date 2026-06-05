"""
context_helpers 特征测试。从 context.py 抽出的纯 helper,仅依赖 stdlib(datetime)。
可离线运行。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.context_helpers import (
    _ngrams,
    _jaccard,
    _current_time_info,
    _detect_failed_image_downloads,
    _extract_recent_bot_logs,
)


def test_ngrams_basic():
    grams = _ngrams("abcd", 2)
    assert "ab" in grams and "bc" in grams and "cd" in grams


def test_jaccard_range_and_identity():
    a = _ngrams("abcd", 2)
    assert _jaccard(a, a) == 1.0
    j = _jaccard(_ngrams("abcd", 2), _ngrams("abce", 2))
    assert 0.0 <= j <= 1.0


def test_jaccard_disjoint_is_zero():
    assert _jaccard(_ngrams("aaaa", 2), _ngrams("bbbb", 2)) == 0.0


def test_current_time_info_returns_str():
    assert isinstance(_current_time_info(), str)


def test_detect_failed_image_downloads_returns_collection():
    out = _detect_failed_image_downloads([])
    assert out is not None


def test_extract_recent_bot_logs_handles_empty():
    out = _extract_recent_bot_logs([])
    assert out is not None
