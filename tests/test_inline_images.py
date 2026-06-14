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


def test_annotate_inline_images_marks_current_user_match():
    from app.core.inline_images import annotate_inline_images

    images = [
        {"name": "mine.png", "size": 1, "mtime": 2, "mtime_str": "06-13 10:00:00"},
        {"name": "other.png", "size": 1, "mtime": 1, "mtime_str": "06-13 09:59:00"},
    ]
    messages = [
        {
            "id": 1,
            "user_id": "u1",
            "user_name": "Alice",
            "content": "[本地image: other.png]",
        },
        {
            "id": 2,
            "user_id": "u2",
            "user_name": "Bob",
            "content": "[本地image: mine.png]",
        },
    ]

    annotated = annotate_inline_images(
        images,
        messages,
        current_user_id="u2",
        current_user_name="Bob",
    )

    by_name = {item["name"]: item for item in annotated}
    assert by_name["mine.png"]["current_user_match"] is True
    assert by_name["mine.png"]["uploader_user_id"] == "u2"
    assert by_name["other.png"]["current_user_match"] is False
    assert by_name["other.png"]["uploader_name"] == "Alice"


def test_annotate_inline_images_same_nickname_missing_owner_id_is_unknown():
    from app.core.inline_images import annotate_inline_images

    images = [
        {"name": "ambiguous.png", "size": 1, "mtime": 2, "mtime_str": "06-13 10:00:00"},
    ]
    messages = [
        {
            "id": 1,
            "user_name": "SameNick",
            "content": "[本地image: ambiguous.png]",
        },
    ]

    annotated = annotate_inline_images(
        images,
        messages,
        current_user_id="u2",
        current_user_name="SameNick",
    )

    assert annotated[0]["current_user_match"] is None
