"""L1-2: processes 心跳字段测试."""
import pytest
import time
from app.core.core_processes import (
    _extract_progress_pct, _compute_what_doing,
    _compute_remaining_estimate, _compute_wait_or_continue,
)


def test_extract_progress_pct_empty():
    assert _extract_progress_pct("") is None


def test_extract_progress_pct_percent():
    assert _extract_progress_pct("完成 75%") == 75


def test_extract_progress_pct_ignores_numeric_tolerance():
    assert _extract_progress_pct("28 of 31 claims matched within 50% tolerance") is None


def test_extract_progress_pct_fraction():
    pct = _extract_progress_pct("done 3/4 tasks")
    assert pct is not None and pct > 0


def test_compute_what_doing_empty():
    assert _compute_what_doing([], "") == "idle"


def test_compute_what_doing_with_thought():
    result = _compute_what_doing(["gcc", "workspace"], "正在编译测试文件")
    assert "编译" in result or "gcc" in result.lower()


def test_compute_remaining_estimate_fresh():
    est = _compute_remaining_estimate(iter=2, elapsed=4.0, last_thought="")
    assert est is not None


def test_compute_wait_or_continue_stale():
    verdict = _compute_wait_or_continue(
        last_heartbeat_age_sec=400, iter=3, recent_tools=["ls", "dir", "ls", "dir"],
    )
    assert verdict in ("wait", "check", "intervene")


def test_compute_wait_or_continue_fresh():
    verdict = _compute_wait_or_continue(
        last_heartbeat_age_sec=10, iter=8, recent_tools=["gcc", "python", "workspace"],
    )
    assert verdict == "wait"
