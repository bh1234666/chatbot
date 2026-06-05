import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_scan_inline_images_lists_images_sorted(monkeypatch, tmp_path):
    from app.core.inline_images import scan_inline_images
    from app.llm.tools import workspace as ws_tool

    media_dir = tmp_path / "_downloaded_media"
    media_dir.mkdir()
    old_img = media_dir / "old.png"
    new_img = media_dir / "new.jpg"
    ignored = media_dir / "note.txt"
    old_img.write_bytes(b"old")
    new_img.write_bytes(b"newer")
    ignored.write_text("ignore", encoding="utf-8")

    now = time.time()
    os.utime(old_img, (now - 7200, now - 7200))
    os.utime(new_img, (now - 60, now - 60))
    monkeypatch.setattr(ws_tool, "create_workspace", lambda archive_id, group_id: str(tmp_path))

    images = scan_inline_images("a1", "g1")

    assert [item["name"] for item in images] == ["new.jpg", "old.png"]
    assert images[0]["is_session"] is True
    assert images[1]["is_session"] is False


def test_scan_inline_images_returns_empty_when_missing(monkeypatch, tmp_path):
    from app.core.inline_images import scan_inline_images
    from app.llm.tools import workspace as ws_tool

    monkeypatch.setattr(ws_tool, "create_workspace", lambda archive_id, group_id: str(tmp_path))

    assert scan_inline_images("a1", "g1") == []
