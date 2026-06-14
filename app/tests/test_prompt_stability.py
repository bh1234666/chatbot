from __future__ import annotations

import time
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


def test_recent_visual_inputs_distinguish_same_user_from_shared_context():
    msgs = build_base_context(
        user_name="Bob",
        current_user_id="u2",
        current_message="看上面的图",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[],
        inline_images=[
            {
                "name": "other.png",
                "size": 10,
                "mtime": 2,
                "mtime_str": "06-13 10:00:00",
                "is_session": True,
                "uploader_user_id": "u1",
                "uploader_name": "Alice",
                "current_user_match": False,
            },
            {
                "name": "mine.png",
                "size": 10,
                "mtime": 1,
                "mtime_str": "06-13 09:59:00",
                "is_session": True,
                "uploader_user_id": "u2",
                "uploader_name": "Bob",
                "current_user_match": True,
            },
        ],
    )
    system_text = msgs[0]["content"]

    assert "Same-user entries are likely source material" in system_text
    assert "_downloaded_media/mine.png" in system_text
    assert "_downloaded_media/other.png" in system_text
    assert "mine.png` · 10B · 06-13 09:59:00 recent · same-user" in system_text
    assert "other.png` · 10B · 06-13 10:00:00 recent · other-user Alice" in system_text


def test_shared_files_recent_cap_preserves_same_speaker_uploads():
    now = time.time()
    current_file = {
        "id": "f_current",
        "filename": "current_user_task.docx",
        "headline": "current user's source",
        "uploader_name": "Bob",
        "uploader_uin": "u2",
        "file_size": 1024,
        "download_status": "done",
        "eff_salience": 1,
        "upload_time": now - 60,
    }
    other_files = [
        {
            "id": f"f_other_{i:02d}",
            "filename": f"other_{i:02d}.docx",
            "headline": "other user's shared context",
            "uploader_name": "Alice",
            "uploader_uin": "u1",
            "file_size": 1024,
            "download_status": "done",
            "eff_salience": 10,
            "upload_time": now - i,
        }
        for i in range(11)
    ]

    msgs = build_base_context(
        user_name="Bob",
        current_user_id="u2",
        current_message="看这个文件",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[],
        file_index=[*other_files, current_file],
    )
    system_text = msgs[0]["content"]

    current_line = "current_user_task.docx · 1KB · uploader Bob"
    other_line = "other_00.docx · 1KB · uploader Alice"
    assert "Recent uploads are source candidates" in system_text
    assert f"[f_current] recent same-speaker {current_line}" in system_text
    assert f"[f_other_00] recent other-user {other_line}" in system_text
    assert system_text.index(current_line) < system_text.index(other_line)


def test_shared_files_keep_same_nickname_same_filename_separate_by_uploader_id():
    now = time.time()
    files = [
        {
            "id": "f_same_name_other",
            "filename": "task.docx",
            "headline": "same nickname other uploader",
            "uploader_name": "SameNick",
            "uploader_uin": "u1",
            "file_size": 1024,
            "download_status": "done",
            "eff_salience": 10,
            "upload_time": now - 5,
        },
        {
            "id": "f_same_name_current",
            "filename": "task.docx",
            "headline": "same nickname current speaker",
            "uploader_name": "SameNick",
            "uploader_uin": "u2",
            "file_size": 1024,
            "download_status": "done",
            "eff_salience": 1,
            "upload_time": now - 60,
        },
    ]

    msgs = build_base_context(
        user_name="SameNick",
        current_user_id="u2",
        current_message="看我刚传的 task.docx",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[],
        file_index=files,
    )
    system_text = msgs[0]["content"]

    current_line = "[f_same_name_current] recent same-speaker task.docx · 1KB · uploader SameNick"
    other_line = "[f_same_name_other] recent other-user task.docx · 1KB · uploader SameNick"
    assert current_line in system_text
    assert other_line in system_text
    assert system_text.index(current_line) < system_text.index(other_line)


def test_shared_files_surface_newer_other_user_without_discarding_stale_same_speaker_upload():
    now = time.time()
    files = [
        {
            "id": "f_stale_current",
            "filename": "old_current_task.docx",
            "headline": "current speaker's old file",
            "uploader_name": "Bob",
            "uploader_uin": "u2",
            "file_size": 1024,
            "download_status": "done",
            "eff_salience": 100,
            "upload_time": now - 3600,
        },
        {
            "id": "f_fresh_other",
            "filename": "fresh_shared_task.docx",
            "headline": "newer other-user shared file",
            "uploader_name": "Alice",
            "uploader_uin": "u1",
            "file_size": 1024,
            "download_status": "done",
            "eff_salience": 1,
            "upload_time": now - 5,
        },
    ]

    msgs = build_base_context(
        user_name="Bob",
        current_user_id="u2",
        current_message="看这个文件",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[],
        file_index=files,
    )
    system_text = msgs[0]["content"]

    stale_line = "[f_stale_current] same-speaker old_current_task.docx · 1KB · uploader Bob"
    fresh_line = "[f_fresh_other] recent other-user fresh_shared_task.docx · 1KB · uploader Alice"
    assert "older same-speaker files remain valid historical candidates" in system_text
    assert "较早的同说话人文件仍是历史候选" in system_text
    assert fresh_line in system_text
    assert stale_line in system_text
    assert system_text.index(fresh_line) < system_text.index(stale_line)


def test_shared_files_show_kb_file_content_summary_for_historical_candidates():
    now = time.time()
    msgs = build_base_context(
        user_name="Bob",
        current_user_id="u2",
        current_message="查一下之前那个实验报告",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[],
        file_index=[{
            "id": "f_old_report",
            "filename": "experiment_report.docx",
            "headline": "实验报告摘要",
            "content": "这个文档包含实验报告正文、数据表、误差分析和最终结论。",
            "uploader_name": "Bob",
            "uploader_uin": "u2",
            "file_size": 2048,
            "download_status": "done",
            "eff_salience": 1,
            "upload_time": now - 86400,
        }],
    )
    system_text = msgs[0]["content"]

    assert "experiment_report.docx" in system_text
    assert "summary: 这个文档包含实验报告正文、数据表、误差分析和最终结论。" in system_text


def test_fresh_ocr_source_paths_use_same_user_implicit_images_only():
    from app.core.orchestrator import _extract_fresh_ocr_source_paths

    paths = _extract_fresh_ocr_source_paths(
        "重新识别这张图",
        [
            {"name": "other.png", "current_user_match": False},
            {"name": "mine.png", "current_user_match": True},
        ],
    )

    assert paths == ["_downloaded_media/mine.png"]


def test_fresh_ocr_source_paths_skip_unattributed_group_images():
    from app.core.orchestrator import _extract_fresh_ocr_source_paths

    paths = _extract_fresh_ocr_source_paths(
        "please OCR this image again",
        [
            {"name": "unknown.png", "current_user_match": None},
            {"name": "other.png", "current_user_match": False},
        ],
    )

    assert paths == []


def test_recent_visual_inputs_mark_same_user_without_internal_producer_terms():
    block = _build_system_blocks(
        hot_group=[],
        warm_group_index=[],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[],
        file_index=[],
        inline_images=[
            {
                "name": "mine.png",
                "size": 10,
                "mtime": 2,
                "mtime_str": "06-13 10:00:00",
                "is_session": True,
                "uploader_user_id": "u2",
                "uploader_name": "Bob",
                "current_user_match": True,
            },
            {
                "name": "other.png",
                "size": 10,
                "mtime": 1,
                "mtime_str": "06-13 09:59:00",
                "is_session": True,
                "uploader_user_id": "u1",
                "uploader_name": "Alice",
                "current_user_match": False,
            },
        ],
    )

    mine_line = "`_downloaded_media/mine.png`"
    other_line = "`_downloaded_media/other.png`"
    assert "same-user" in block
    assert "other-user Alice" in block
    assert block.index(mine_line) < block.index(other_line)
    assert "read producer" not in block


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
    assert "<bot_log_brief>intent=read files | note=checked README | deliverables=a.md,b.md</bot_log_brief>" in old_text
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
