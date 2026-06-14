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


def test_extract_recent_bot_logs_returns_sanitized_briefs():
    base = [
        {"role": "system", "content": "system"},
        {
            "role": "user",
            "content": (
                "## 对话历史\n"
                "[机器人] 已完成<bot_log>complexity=medium | intent=inspect page | "
                "deliverables=[report.md] | helpers={done:[fetch_page]} | "
                "note=read helper report from _helpers_shared/fetch/page.txt; "
                "copied from internal_shared/fetch/page.txt and _delegate_fetch; "
                "producer-owned output from main process is producer_self_verified</bot_log>"
            ),
        },
    ]

    out = _extract_recent_bot_logs(base)

    assert "<bot_log_brief>" in out
    assert "intent=inspect page" in out
    assert "deliverables=[report.md]" in out
    assert "work_status=done:1" in out
    assert "processing_records" not in out
    assert "fetch_page" not in out
    assert "background_work" not in out
    assert "work unit" not in out.lower()
    assert "producer evidence" not in out.lower()
    assert "producer" not in out.lower()
    assert "main process" not in out.lower()
    assert "main thread" not in out.lower()
    assert "output_self_verified" in out
    assert "helper" not in out.lower()
    assert "_helpers_shared" not in out
    assert "internal_shared" not in out
    assert "_delegate_" not in out
    assert "internal_run" not in out
    assert "work material" in out
