"""验证 Round2.5 已合并打断消息的去重兜底(2026-05-21 修复)。

直接构造 GroupGuard,测 mark_abort_injected / consume_abort_injected:
  - mark 后同一条消息(含大小写/空白差异)consume 命中且只命中一次(消费式)
  - 未登记的消息 consume 不误杀
  - 指纹归一化:@ 前缀差异/多空白视为同一条
纯 stdlib,可离线运行。
"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.core.locks import GroupGuard

A, G, U = "arch1", "grp1", "usr1"


def _fresh():
    g = GroupGuard.__new__(GroupGuard)  # 跳过 __init__ 的 asyncio.Lock(测试无 loop)
    g._abort_injected_msgs = {}
    return g


def test_mark_then_consume_hits_once():
    g = _fresh()
    g.mark_abort_injected(A, G, U, ["继续写第五题"])
    assert g.consume_abort_injected(A, G, U, "继续写第五题") is True
    # 消费式:第二次同消息不再命中
    assert g.consume_abort_injected(A, G, U, "继续写第五题") is False


def test_fingerprint_normalizes_case_and_space():
    g = _fresh()
    g.mark_abort_injected(A, G, U, ["  Continue The Task  "])
    assert g.consume_abort_injected(A, G, U, "continue the task") is True


def test_unregistered_message_not_killed():
    g = _fresh()
    g.mark_abort_injected(A, G, U, ["消息A"])
    assert g.consume_abort_injected(A, G, U, "完全不同的消息B") is False
    # 原登记仍在(没被误消费)
    assert g.consume_abort_injected(A, G, U, "消息A") is True


def test_multiple_messages_independent():
    g = _fresh()
    g.mark_abort_injected(A, G, U, ["问题1", "问题2"])
    assert g.consume_abort_injected(A, G, U, "问题2") is True
    assert g.consume_abort_injected(A, G, U, "问题1") is True
    assert g.consume_abort_injected(A, G, U, "问题1") is False


def test_ttl_expiry():
    g = _fresh()
    g.mark_abort_injected(A, G, U, ["旧消息"])
    # 手工把时间戳改成 121s 前
    key = (A, G, U)
    g._abort_injected_msgs[key] = [(g._abort_msg_fingerprint("旧消息"), time.monotonic() - 121.0)]
    assert g.consume_abort_injected(A, G, U, "旧消息") is False


def test_empty_message_safe():
    g = _fresh()
    g.mark_abort_injected(A, G, U, [""])
    assert g.consume_abort_injected(A, G, U, "") is False  # 空指纹不登记/不命中


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    p = 0
    for fn in fns:
        fn(); p += 1
    print(f"test_abort_injected: {p} passed")
