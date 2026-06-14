"""
workspace_utils 特征(characterization)测试。

这些函数从 delegate.py 原样抽出,均为纯 stdlib(os/subprocess/pathlib)。本测试用
临时目录构造真实输入,断言其当前行为——既作为抽取正确性的护栏,也为后续若需修改
这些函数提供行为快照。可离线运行:`pytest tests/test_workspace_utils.py`。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import os
import tempfile

from app.llm.tools.workspace_utils import (
    _dir_size,
    _list_workspace_files,
    _match_path_pattern,
    _has_office_document_output,
    _is_internal_helper_artifact,
    _derive_permanent_root,
    _extract_declared_files,
    take_workspace_snapshot,
)


def _make_ws():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "a.txt"), "w") as f:
        f.write("hello")
    os.makedirs(os.path.join(d, "sub"), exist_ok=True)
    with open(os.path.join(d, "sub", "b.py"), "w") as f:
        f.write("print(1)\n")
    return d


def test_dir_size_counts_bytes():
    d = _make_ws()
    size = _dir_size(d)
    assert isinstance(size, int)
    assert size >= len("hello") + len("print(1)\n")


def test_list_workspace_files_finds_nested():
    d = _make_ws()
    files = _list_workspace_files(d)
    assert isinstance(files, list)
    joined = "\n".join(files)
    assert "a.txt" in joined and "b.py" in joined


def test_take_workspace_snapshot_maps_files():
    d = _make_ws()
    snap = take_workspace_snapshot(d)
    assert isinstance(snap, dict)
    # 仅拍顶层文件(+ _helpers_shared/),不递归普通子目录:
    assert "a.txt" in snap
    assert "b.py" not in snap          # sub/b.py 是嵌套普通目录,不入快照
    # 值为 (mtime, size) 元组
    for v in snap.values():
        assert isinstance(v, tuple) and len(v) == 2


def test_match_path_pattern_glob():
    assert _match_path_pattern("a.txt", "*.txt") is True
    assert _match_path_pattern("a.txt", "*.py") is False
    assert _match_path_pattern("sub/b.py", "sub/*.py") is True


def test_has_office_document_output():
    assert _has_office_document_output("请生成 report.docx") is True
    assert _has_office_document_output("", ["slides.pptx"]) is True
    assert _has_office_document_output("读取这些 source/report.docx 文件内容") is False
    assert _has_office_document_output("just a .txt note") is False
    assert _has_office_document_output("") is False


def test_is_internal_helper_artifact():
    assert _is_internal_helper_artifact(".session_tag") is True
    assert _is_internal_helper_artifact(".read_history.json") is True
    assert _is_internal_helper_artifact("foo_history.json") is True
    assert _is_internal_helper_artifact("report.docx") is False
    assert _is_internal_helper_artifact("") is False


def test_derive_permanent_root():
    base = tempfile.mkdtemp()
    temp_dir = os.path.join(base, ".temp")
    os.makedirs(temp_dir, exist_ok=True)
    assert _derive_permanent_root(temp_dir) == base
    session_dir = os.path.join(temp_dir, "_sessions", "s_abc")
    helper_dir = os.path.join(session_dir, "_delegate_user_task")
    os.makedirs(helper_dir, exist_ok=True)
    assert _derive_permanent_root(session_dir) == base
    assert _derive_permanent_root(helper_dir) == base
    # 非 .temp 路径返回 None
    assert _derive_permanent_root(base) is None
    assert _derive_permanent_root("") is None


def test_extract_declared_files_returns_set():
    out = _extract_declared_files("产出文件:`main.py`、`README.md`")
    assert isinstance(out, set)
    assert _extract_declared_files("") == set()


def test_extract_declared_files_preserves_project_relative_paths():
    report = '```json\n{"files": ["_env/src/package/__init__.py", "reports/final.md"]}\n```'
    assert _extract_declared_files(report) == {
        "_env/src/package/__init__.py",
        "reports/final.md",
    }
