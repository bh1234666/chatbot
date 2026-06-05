"""
本会话从 workspace.py 抽出的若干模块的特征测试(能离线跑的部分):
session_manifest / process_utils / todo_handlers。file_inspect 的多数函数需第三方
解析库或真实样本文件,留待本地补。
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.tools.session_manifest import _atomic_write_json, _peek_edit_count
from app.llm.tools.process_utils import _detect_git_bash
from app.llm.tools.todo_handlers import handle_todo_read, handle_todo_write


def test_atomic_write_json_roundtrip():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "data.json")
    _atomic_write_json(p, {"a": 1, "b": [1, 2, 3]})
    assert json.load(open(p, encoding="utf-8")) == {"a": 1, "b": [1, 2, 3]}


def test_peek_edit_count_missing_returns_default():
    d = tempfile.mkdtemp()
    # 不存在记录时应返回一个整数(默认 0),不抛
    n = _peek_edit_count(d, os.path.join(d, "some_file.txt"))
    assert isinstance(n, int)


def test_detect_git_bash_returns_str_or_none():
    r = _detect_git_bash()
    assert r is None or isinstance(r, str)


def test_todo_read_empty_returns_dict():
    d = tempfile.mkdtemp()
    r = asyncio.new_event_loop().run_until_complete(handle_todo_read(d))
    assert isinstance(r, dict)


def test_todo_write_then_read():
    d = tempfile.mkdtemp()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(handle_todo_write(d, [
        {"content": "任务1", "status": "in_progress"},
        {"content": "任务2", "status": "pending"},
    ]))
    r = loop.run_until_complete(handle_todo_read(d))
    assert isinstance(r, dict)
