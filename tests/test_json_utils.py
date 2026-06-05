"""
json_utils 特征测试。从 client.py 抽出的 LLM 输出 JSON 容错解析/修复族,纯 stdlib。
断言基于函数当前真实行为(行为快照),可离线运行。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json

from app.llm.client import _try_extract_json_locally
from app.llm.json_utils import (
    _try_parse_json,
    _parse_json_strict,
    _complete_truncated_json_suffix,
    _escape_control_chars_in_json_strings,
    _escape_inner_quotes_in_json_strings,
)


def test_try_parse_valid_object():
    assert _try_parse_json('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}


def test_parse_strict_valid_object():
    assert _parse_json_strict('{"x": 2}') == {"x": 2}


def test_extract_json_locally_ignores_prefix_and_suffix():
    content = 'Sure, here is the object:\n{"ok": true, "items": [1, 2]}\nDone.'

    assert _try_extract_json_locally(content) == '{"ok": true, "items": [1, 2]}'


def test_complete_truncated_suffix_makes_parseable():
    # 函数返回"补全后缀";原串 + 后缀应能被 json 解析
    truncated = '{"a": 1, "b": 2'
    suffix = _complete_truncated_json_suffix(truncated)
    assert isinstance(suffix, str)
    parsed = json.loads(truncated + suffix)
    assert parsed == {"a": 1, "b": 2}


def test_complete_truncated_nested():
    truncated = '{"items": [1, 2, {"k": "v"'
    suffix = _complete_truncated_json_suffix(truncated)
    parsed = json.loads(truncated + suffix)
    assert parsed["items"][2] == {"k": "v"}


def test_escape_control_chars_returns_str():
    out = _escape_control_chars_in_json_strings('{"a": "line1\nline2"}')
    assert isinstance(out, str)
    # 转义后应可被严格 json 解析(裸控制字符已被处理)
    json.loads(out)


def test_escape_inner_quotes_returns_str():
    out = _escape_inner_quotes_in_json_strings('{"a": "x"}')
    assert isinstance(out, str)
