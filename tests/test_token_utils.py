"""特征测试: _estimate_msgs_tokens / _stall_threshold_for(2026-05-20 从 _run_one_helper 上提)。
隔离 exec 抽出的函数源码(不 import delegate, 避开第三方依赖)。"""
from app.llm.tools.delegate_stuck import _estimate_msgs_tokens, _stall_threshold_for

_est = _estimate_msgs_tokens
_stall = _stall_threshold_for


def test_estimate_empty():
    assert _est([]) == 0

def test_estimate_str_content():
    # 30 bytes ascii // 3 = 10
    assert _est([{"content": "a" * 30}]) == 10

def test_estimate_list_content():
    msgs = [{"content": [{"text": "x" * 12}, {"text": "y" * 6}]}]
    assert _est(msgs) == (12 + 6) // 3

def test_estimate_mixed_and_none():
    msgs = [{"content": "ab"}, {"content": None}, {"role": "x"}]
    assert _est(msgs) == 2 // 3  # =0

def test_stall_thresholds():
    # 控制 token 数落入各档: token = bytes//3
    def msg(nbytes): return [{"content": "a" * nbytes}]
    assert _stall(msg(30)) == 90.0          # 10 tok
    assert _stall(msg(50_000 * 3 + 3)) == 150.0   # >50K
    assert _stall(msg(100_000 * 3 + 3)) == 240.0  # >100K
    assert _stall(msg(200_000 * 3 + 3)) == 360.0  # >200K

def test_stall_boundary_exact():
    # 恰好 50_000 tok 不触发 (>50_000 才升档)
    def msg_tok(t): return [{"content": "a" * (t * 3)}]
    assert _stall(msg_tok(50_000)) == 90.0
    assert _stall(msg_tok(50_001)) == 150.0
