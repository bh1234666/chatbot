import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class DebugRecorder:
    def __init__(self):
        self.events = []

    def log(self, category, message, payload=None):
        self.events.append((category, message, payload))


def _tag(user_id: str) -> str:
    from app.llm.tools.delegate import _user_workspace_tag

    return _user_workspace_tag(user_id)


def test_cleanup_cross_user_delegate_dirs_preserves_current_user(tmp_path):
    from app.core.delegate_cleanup import cleanup_cross_user_delegate_dirs

    current = _tag("u1")
    other = _tag("u2")
    keep = tmp_path / f"_delegate_{current}_task"
    remove = tmp_path / f"_delegate_{other}_task"
    keep.mkdir()
    remove.mkdir()
    (tmp_path / "normal").mkdir()
    old_time = 1_000_000_000
    os.utime(remove, (old_time, old_time))
    debug = DebugRecorder()

    removed = cleanup_cross_user_delegate_dirs(str(tmp_path), "u1", debug=debug)

    assert removed == 1
    assert keep.exists()
    assert not remove.exists()
    assert debug.events[-1][0] == "workspace.cross_user_cleanup"


def test_cleanup_cross_user_delegate_dirs_preserves_recent_other_user(tmp_path):
    from app.core.delegate_cleanup import cleanup_cross_user_delegate_dirs

    current = _tag("u1")
    other = _tag("u2")
    keep = tmp_path / f"_delegate_{current}_task"
    recent_other = tmp_path / f"_delegate_{other}_active_task"
    old_other = tmp_path / f"_delegate_{other}_old_task"
    keep.mkdir()
    recent_other.mkdir()
    old_other.mkdir()
    old_time = 1_000_000_000
    os.utime(old_other, (old_time, old_time))
    debug = DebugRecorder()

    removed = cleanup_cross_user_delegate_dirs(str(tmp_path), "u1", debug=debug)

    assert removed == 1
    assert keep.exists()
    assert recent_other.exists()
    assert not old_other.exists()
    assert "recent=1" in debug.events[-1][1]


def test_cleanup_old_same_user_delegate_dirs_only_removes_old_current_user(tmp_path):
    from app.core.delegate_cleanup import cleanup_old_same_user_delegate_dirs

    current = _tag("u1")
    other = _tag("u2")
    old_current = tmp_path / f"_delegate_{current}_old"
    new_current = tmp_path / f"_delegate_{current}_new"
    old_other = tmp_path / f"_delegate_{other}_old"
    old_current.mkdir()
    new_current.mkdir()
    old_other.mkdir()

    old_time = 1_000_000_000
    os.utime(old_current, (old_time, old_time))
    os.utime(old_other, (old_time, old_time))
    debug = DebugRecorder()

    removed = cleanup_old_same_user_delegate_dirs(str(tmp_path), "u1", max_age_days=1, debug=debug)

    assert removed == 1
    assert not old_current.exists()
    assert new_current.exists()
    assert old_other.exists()
    assert debug.events[-1][0] == "workspace.old_delegate_cleanup"


def test_cleanup_inactive_delegate_dirs_preserves_active_and_recent(tmp_path):
    from app.core.delegate_cleanup import cleanup_inactive_delegate_dirs

    current = _tag("u1")
    active = tmp_path / f"_delegate_{current}_active"
    newest = tmp_path / f"_delegate_{current}_newest"
    old = tmp_path / f"_delegate_{current}_old"
    for path in (active, newest, old):
        path.mkdir()
    os.utime(old, (1_000_000_000, 1_000_000_000))
    os.utime(newest, (2_000_000_000, 2_000_000_000))
    debug = DebugRecorder()

    removed = cleanup_inactive_delegate_dirs(
        str(tmp_path),
        "u1",
        active_task_ids={"active"},
        max_keep=1,
        debug=debug,
    )

    assert removed == 1
    assert active.exists()
    assert newest.exists()
    assert not old.exists()
    assert debug.events[-1][0] == "workspace.inactive_delegate_cleanup"


def test_enforce_workspace_capacity_removes_only_reclaimable_files(tmp_path):
    from app.llm.tools.workspace import enforce_workspace_capacity

    keep = tmp_path / "deliverable.txt"
    keep.write_bytes(b"x" * 80)
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "module.pyc").write_bytes(b"x" * 80)
    temp = tmp_path / "scratch.tmp"
    temp.write_bytes(b"x" * 80)

    result = enforce_workspace_capacity(str(tmp_path), max_bytes=100, label="test")

    assert result["ok"] is True
    assert keep.exists()
    assert not cache.exists()
    assert not temp.exists()
    assert result["removed_files"] == 1
    assert result["removed_dirs"] == 1


def test_enforce_workspace_capacity_deletes_duplicate_deliverables_after_temp(tmp_path):
    from app.llm.tools.workspace import enforce_workspace_capacity

    src = tmp_path / "main.py"
    src.write_bytes(b"x" * 80)
    newest = tmp_path / "chart.png"
    copy1 = tmp_path / "chart_1.png"
    copy2 = tmp_path / "chart (2).png"
    newest.write_bytes(b"x" * 80)
    copy1.write_bytes(b"x" * 80)
    copy2.write_bytes(b"x" * 80)
    os.utime(copy1, (1_000_000_000, 1_000_000_000))
    os.utime(copy2, (1_100_000_000, 1_100_000_000))
    os.utime(newest, (2_000_000_000, 2_000_000_000))

    result = enforce_workspace_capacity(str(tmp_path), max_bytes=170, label="test")

    assert result["ok"] is True
    assert src.exists()
    assert newest.exists()
    assert not copy1.exists()
    assert not copy2.exists()
    assert result["removed_duplicates"] == 2


def test_enforce_workspace_capacity_deletes_old_deliverables_to_start(tmp_path):
    from app.llm.tools.workspace import enforce_workspace_capacity

    src = tmp_path / "main.py"
    old_pdf = tmp_path / "old_result.pdf"
    new_pdf = tmp_path / "new_result.pdf"
    src.write_bytes(b"x" * 80)
    old_pdf.write_bytes(b"x" * 80)
    new_pdf.write_bytes(b"x" * 80)
    os.utime(old_pdf, (1_000_000_000, 1_000_000_000))
    os.utime(new_pdf, (2_000_000_000, 2_000_000_000))

    result = enforce_workspace_capacity(str(tmp_path), max_bytes=170, label="test")

    assert result["ok"] is True
    assert src.exists()
    assert not old_pdf.exists()
    assert new_pdf.exists()
    assert result["removed_deliverables"] == 1
