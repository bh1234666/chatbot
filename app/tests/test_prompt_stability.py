from __future__ import annotations

from datetime import datetime, timezone

from app.core.context import (
    build_base_context,
    _format_recent_group_messages,
    _build_system_blocks,
    _format_hot_user_history,
)
from app.core.inline_images import scan_inline_images
from app.schemas.api import HotMessage


def test_recent_group_messages_order_is_stable():
    messages_a = [
        {"created_at": "2026-05-29T10:00:01", "user_name": "B", "content": "hello", "addressed_bot": False, "id": 2},
        {"created_at": "2026-05-29T10:00:01", "user_name": "A", "content": "hello", "addressed_bot": False, "id": 1},
        {"created_at": "2026-05-29T10:00:02", "user_name": "C", "content": "world", "addressed_bot": False, "id": 3},
    ]
    messages_b = list(reversed(messages_a))
    out_a = _format_recent_group_messages(messages_a)
    out_b = _format_recent_group_messages(messages_b)
    assert out_a == out_b


def test_system_block_order_is_stable_for_equal_inputs():
    warm = [{"id": "w2", "headline": "beta"}, {"id": "w1", "headline": "alpha"}]
    cold = [{"id": "c2", "headline": "beta", "type": "fact"}, {"id": "c1", "headline": "alpha", "type": "fact"}]
    kb = [{"id": "k2", "headline": "beta", "type": "file"}, {"id": "k1", "headline": "alpha", "type": "file"}]
    files = [{"id": "f2", "filename": "b.txt", "headline": "beta", "uploader_name": "U", "file_size": 2, "download_status": "done", "eff_salience": 1},
             {"id": "f1", "filename": "a.txt", "headline": "alpha", "uploader_name": "U", "file_size": 1, "download_status": "done", "eff_salience": 1}]
    block1 = _build_system_blocks(
        hot_group=[],
        warm_group_index=warm,
        cold_group_topk=cold,
        cold_user_topk=cold,
        kb_topk=kb,
        file_index=files,
        in_flight_others=[("u2", "B"), ("u1", "A")],
        inline_images=[{"name": "b.png", "size": 2, "mtime": 2, "mtime_str": "05-29 10:00:02", "is_session": True},
                       {"name": "a.png", "size": 1, "mtime": 2, "mtime_str": "05-29 10:00:02", "is_session": True}],
        recent_group_messages=[
            {"created_at": "2026-05-29T10:00:01", "user_name": "B", "content": "hello", "addressed_bot": False, "id": 2},
            {"created_at": "2026-05-29T10:00:01", "user_name": "A", "content": "hello", "addressed_bot": False, "id": 1},
        ],
    )
    block2 = _build_system_blocks(
        hot_group=[],
        warm_group_index=list(reversed(warm)),
        cold_group_topk=list(reversed(cold)),
        cold_user_topk=list(reversed(cold)),
        kb_topk=list(reversed(kb)),
        file_index=list(reversed(files)),
        in_flight_others=[("u1", "A"), ("u2", "B")],
        inline_images=[
            {"name": "a.png", "size": 1, "mtime": 2, "mtime_str": "05-29 10:00:02", "is_session": True},
            {"name": "b.png", "size": 2, "mtime": 2, "mtime_str": "05-29 10:00:02", "is_session": True},
        ],
        recent_group_messages=[
            {"created_at": "2026-05-29T10:00:01", "user_name": "A", "content": "hello", "addressed_bot": False, "id": 1},
            {"created_at": "2026-05-29T10:00:01", "user_name": "B", "content": "hello", "addressed_bot": False, "id": 2},
        ],
    )
    assert block1 == block2
    assert "## Current Time" not in block1


def test_current_time_is_user_dynamic_tail_not_system_prefix():
    common = dict(
        user_name="User",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[{"id": "w1", "headline": "stable warm"}],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[],
    )
    msgs_a = build_base_context(current_message="first request", **common)
    msgs_b = build_base_context(current_message="second request", **common)

    assert msgs_a[0]["role"] == "system"
    assert msgs_b[0]["role"] == "system"
    assert msgs_a[0]["content"] == msgs_b[0]["content"]
    assert "## Current Time" not in msgs_a[0]["content"]
    assert "## Current Time" in msgs_a[1]["content"]
    assert "## Current Time" in msgs_b[1]["content"]
    assert "first request" in msgs_a[1]["content"]
    assert "second request" in msgs_b[1]["content"]


def test_hot_history_existing_entries_are_append_only_stable():
    now = datetime.now(timezone.utc)
    first_two = [
        HotMessage(role="user", content="第一条需求", turn_id="t1", created_at=now),
        HotMessage(
            role="assistant",
            content="已处理<bot_log>intent=read files | note=checked README | deliverables=a.md,b.md</bot_log>",
            turn_id="t1",
            created_at=now,
        ),
    ]
    with_appended = [
        *first_two,
        HotMessage(role="user", content="追加的新问题", turn_id="t2", created_at=now),
    ]

    old_text = _format_hot_user_history(first_two)
    new_text = _format_hot_user_history(with_appended)
    old_entries = old_text.split("\n\nHistory entry count:", 1)[0]
    new_entries = new_text.split("\n\nHistory entry count:", 1)[0]

    assert "Total historical turns" not in old_text
    assert "<bot_log_brief>intent=read files | note=checked README</bot_log_brief>" in old_text
    assert new_entries.startswith(old_entries)


def test_inline_images_sorted_by_name_on_tie(monkeypatch, tmp_path):
    from pathlib import Path
    import os
    from app.llm.tools import workspace as ws_tool

    ws = tmp_path / "main"
    media = ws / "_downloaded_media"
    media.mkdir(parents=True)
    fixed_ts = 1710000000
    for name in ("b.png", "a.png"):
        p = media / name
        p.write_bytes(b"x")
        os.utime(p, (fixed_ts, fixed_ts))

    monkeypatch.setattr(ws_tool, "create_workspace", lambda archive_id, group_id: str(ws))
    images = scan_inline_images("arch", "group")
    assert [img["name"] for img in images] == ["a.png", "b.png"]
