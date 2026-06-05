"""
message_utils 特征测试。从 client.py 抽出的消息/格式化工具族,纯 stdlib + 自建 logger。
含 `from __future__ import annotations`,故标注惰性化、不耦合 client 内部类型。可离线运行。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.message_utils import (
    _now_iso,
    _estimate_msgs_token_size,
    _is_thinking_enabled,
    _build_continuation_prefix_message,
    _inject_tool_timestamps,
)
from app.llm.json_utils import stable_prompt_json


def test_stable_prompt_json_sorts_keys_and_compacts_whitespace():
    left = stable_prompt_json({"z": 2, "a": {"b": 1, "a": 0}})
    right = stable_prompt_json({"a": {"a": 0, "b": 1}, "z": 2})

    assert left == right
    assert left == '{"a":{"a":0,"b":1},"z":2}'


def test_now_iso_is_str():
    s = _now_iso()
    assert isinstance(s, str) and len(s) >= 10


def test_estimate_token_size_monotonic():
    small = _estimate_msgs_token_size([{"role": "user", "content": "hi"}])
    big = _estimate_msgs_token_size([{"role": "user", "content": "hi " * 500}])
    assert isinstance(small, int) and isinstance(big, int)
    assert big > small


class _FakeCollector:
    """鸭子对象,模拟 client._StreamCollector 的接口(标注惰性化使其可用)。"""
    def __init__(self, content="", reasoning_content="", tool_calls=None):
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls or []


def test_continuation_prefix_empty_returns_none():
    assert _build_continuation_prefix_message(_FakeCollector()) is None


def test_continuation_prefix_with_content_returns_message():
    msg = _build_continuation_prefix_message(_FakeCollector(content="已经写了一半"))
    assert isinstance(msg, dict)
    assert msg.get("role") == "assistant"


def test_is_thinking_enabled_returns_bool():
    # 不同入参下都应返回布尔且不抛(行为快照)
    assert isinstance(_is_thinking_enabled("deepseek-chat"), bool)


def test_inject_tool_timestamps_returns_str():
    out = _inject_tool_timestamps(
        "工具执行结果",
        started_at_iso="2026-05-20T09:00:00",
        finished_at_iso="2026-05-20T09:00:03",
        elapsed_sec=3.0,
    )
    assert isinstance(out, str)
    assert "工具执行结果" in out
