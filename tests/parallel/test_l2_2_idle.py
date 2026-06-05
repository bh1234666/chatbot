"""L2-2: idle detector 状态机测试."""
import pytest
from app.llm.client import _IdleDetector


def _make_tc(name, action="list"):
    """Make a minimal tool_call-like tuple for recording."""
    return ("tc", name, "{}", {"action": action})


def test_idle_detector_resets_on_productive():
    d = _IdleDetector()
    for _ in range(2):
        d.record_iter(0, [("tc", "processes", '{"ok":true,"processes":[]}', {"action": "list"})])
    assert d.consecutive_idle_iters == 2

    d.record_iter(2, [("tc", "office", "{}", {"action": "write"})])
    assert d.consecutive_idle_iters == 0


def test_idle_detector_soft_warning_at_3():
    d = _IdleDetector()
    for _ in range(3):
        d.record_iter(0, [("tc", "processes", "{}", {"action": "list"})])
    w = d.should_inject_warning(3)
    assert w is not None
    assert "Idle Penalty" in w


def test_idle_detector_no_spam():
    d = _IdleDetector()
    for _ in range(3):
        d.record_iter(0, [("tc", "processes", "{}", {"action": "list"})])
    w1 = d.should_inject_warning(3)
    assert w1 is not None

    w2 = d.should_inject_warning(3)
    assert w2 is None

    d.record_iter(0, [("tc", "processes", "{}", {"action": "list"})])
    w3 = d.should_inject_warning(4)
    assert w3 is None


def test_delegate_spawn_not_idle():
    d = _IdleDetector()
    d.record_iter(0, [("tc", "delegate", "{}", {"action": "spawn"})])
    d.record_iter(1, [("tc", "delegate", "{}", {"action": "spawn_async"})])
    assert d.consecutive_idle_iters == 0


def test_workspace_locate_not_idle():
    d = _IdleDetector()
    for _ in range(3):
        d.record_iter(0, [("tc", "workspace", "{}", {"action": "locate", "pattern": "*.png"})])
    assert d.consecutive_idle_iters == 0


def test_dir_command_is_idle():
    d = _IdleDetector()
    for _ in range(3):
        d.record_iter(0, [("tc", "workspace", "{}", {"action": "run", "command": "dir /b"})])
    w = d.should_inject_warning(3)
    assert w is not None
