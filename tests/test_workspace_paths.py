"""
workspace_paths 特征测试。从 workspace.py 抽出的路径安全/可回收判定,仅依赖 stdlib。
可离线运行。
"""
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.tools.workspace_paths import (
    _safe_resolve,
    _is_safe_to_wipe,
    _is_known_reclaimable_workspace_dir,
    _is_known_reclaimable_workspace_file,
)


def test_safe_resolve_stays_within_ws():
    ws = tempfile.mkdtemp()
    resolved = _safe_resolve(ws, "sub/file.txt")
    assert isinstance(resolved, str)
    # 解析结果应位于 ws 之内
    assert os.path.abspath(resolved).startswith(os.path.abspath(ws))


def test_reclaimable_predicates_return_bool():
    assert isinstance(_is_known_reclaimable_workspace_dir("__pycache__"), bool)
    assert isinstance(_is_known_reclaimable_workspace_file(".session_tag"), bool)


def test_is_safe_to_wipe_returns_bool():
    d = tempfile.mkdtemp()
    assert isinstance(_is_safe_to_wipe(d), bool)
    # 系统根目录之类绝不应被判为可清空
    assert _is_safe_to_wipe("/") is False
