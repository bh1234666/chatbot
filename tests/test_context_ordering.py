import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core import context as ctx_build
from app.schemas.api import GroupEvent, HotMessage


def _pos(text: str, needle: str) -> int:
    pos = text.find(needle)
    assert pos >= 0, f"missing section: {needle}"
    return pos


def test_system_context_orders_stable_sections_before_dynamic_sections():
    now = datetime.now(timezone.utc)
    msgs = ctx_build.build_base_context(
        user_name="Bob",
        current_message="看下这些上下文",
        hot_user=[],
        hot_group=[
            GroupEvent(
                actor_user_id="u1",
                actor_name="Alice",
                narration="Alice 刚刚询问了机器人。",
                created_at=now,
            )
        ],
        warm_user_index=[],
        warm_group_index=[{"id": "w1", "headline": "群内温记忆"}],
        cold_user_topk=[{"id": "cu1", "headline": "用户长期记忆"}],
        cold_group_topk=[{"id": "cg1", "headline": "共享长期记忆"}],
        kb_topk=[{"id": "kb1", "headline": "共享知识库"}],
        file_index=[
            {
                "id": "f1",
                "filename": "a.txt",
                "headline": "文件摘要",
                "uploader_name": "Alice",
                "file_size": 10,
                "download_status": "done",
                "eff_salience": 1,
            }
        ],
        in_flight_others=[("u2", "Carol")],
    )
    sys_text = msgs[0]["content"]

    assert _pos(sys_text, "## Context And Safety Contract") < _pos(sys_text, "## Shared Long-Term Memory")
    assert _pos(sys_text, "## Shared Long-Term Memory") < _pos(sys_text, "## Current Speaker Long-Term Memory")
    assert _pos(sys_text, "## Current Speaker Long-Term Memory") < _pos(sys_text, "## Shared Knowledge Base")
    assert _pos(sys_text, "## Shared Knowledge Base") < _pos(sys_text, "## Shared Warm Memory Index")
    assert "## Current Time" not in sys_text
    assert _pos(sys_text, "## Shared Warm Memory Index") < _pos(sys_text, "## Shared Files")
    assert _pos(sys_text, "## Shared Files") < _pos(sys_text, "## Other Participants Still Interacting")
    assert _pos(sys_text, "## Other Participants Still Interacting") < _pos(sys_text, "## Recent Activity")

    user_text = msgs[1]["content"]
    assert "## Current Time" in user_text
    assert _pos(user_text, "## Current Time") < _pos(user_text, "## Current Message To Answer")


def test_shared_files_window_overflow_points_model_to_search_historical_files():
    file_index = [
        {
            "id": f"visible_{idx:02d}",
            "filename": f"visible_{idx:02d}.txt",
            "headline": f"visible file {idx}",
            "content": f"visible summary {idx}",
            "uploader_name": "Alice",
            "uploader_uin": "u1",
            "file_size": 1024,
            "download_status": "done",
            "eff_salience": 10,
            "upload_time": 0,
        }
        for idx in range(50)
    ]
    file_index.append({
        "id": "historical_report",
        "filename": "historical_report.docx",
        "headline": "old calibration report",
        "content": "Summary mentions calibration tables, error analysis, and final conclusion.",
        "uploader_name": "Bob",
        "uploader_uin": "u2",
        "file_size": 2048,
        "download_status": "done",
        "eff_salience": 0,
        "upload_time": 0,
    })

    msgs = ctx_build.build_base_context(
        user_name="Bob",
        current_user_id="u2",
        current_message="Find the older calibration report.",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[],
        file_index=file_index,
    )
    sys_text = msgs[0]["content"]

    assert "historical_report.docx" not in sys_text
    assert "1 more files are not shown" in sys_text
    assert "Use search_files/list_files by filename" in sys_text


def test_shared_files_show_relation_upload_time_and_summary_for_multi_user_context():
    file_index = [
        {
            "id": "old_same_user",
            "filename": "task.docx",
            "headline": "当前用户较早上传的作业文档",
            "content": "摘要：包含课堂作业题目、参数和需要完成的题号。",
            "uploader_name": "Alice",
            "uploader_uin": "u1",
            "file_size": 2048,
            "download_status": "done",
            "eff_salience": 5,
            "upload_time": 2_000_000_000,
        },
        {
            "id": "new_other_user",
            "filename": "task.docx",
            "headline": "其他用户较新上传的同名文档",
            "content": "摘要：包含另一组截图和不同题号。",
            "uploader_name": "Bob",
            "uploader_uin": "u2",
            "file_size": 4096,
            "download_status": "done",
            "eff_salience": 5,
            "upload_time": 2_000_000_060,
        },
    ]

    msgs = ctx_build.build_base_context(
        user_name="Alice",
        current_user_id="u1",
        current_message="看一下这个文件",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[],
        file_index=file_index,
    )
    sys_text = msgs[0]["content"]

    assert "old_same_user" in sys_text
    assert "new_other_user" in sys_text
    assert "relation same-speaker" in sys_text
    assert "relation other-user" in sys_text
    assert "uploaded 2033-05-18 03:33 UTC" in sys_text
    assert "uploaded 2033-05-18 03:34 UTC" in sys_text
    assert "摘要：包含课堂作业题目、参数和需要完成的题号。" in sys_text
    assert "摘要：包含另一组截图和不同题号。" in sys_text
    assert "older same-speaker files remain valid historical candidates" in sys_text
    assert "Newer uploads from other users may be the active shared context" in sys_text


def test_shared_files_do_not_treat_same_nickname_as_same_speaker_when_ids_missing_on_one_side():
    file_index = [
        {
            "id": "unknown_id_same_nick",
            "filename": "task.docx",
            "headline": "same nickname but missing uploader id",
            "content": "summary: ambiguous owner",
            "uploader_name": "SameNick",
            "file_size": 2048,
            "download_status": "done",
            "eff_salience": 5,
            "upload_time": 2_000_000_000,
        },
    ]

    msgs = ctx_build.build_base_context(
        user_name="SameNick",
        current_user_id="u2",
        current_message="看一下这个文件",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[],
        file_index=file_index,
    )
    sys_text = msgs[0]["content"]

    assert "unknown_id_same_nick" in sys_text
    assert "relation unknown-uploader-relation" in sys_text
    assert "same-speaker" not in sys_text.split("unknown_id_same_nick", 1)[1].splitlines()[0]


def test_shared_files_marks_same_uploader_same_name_versions():
    file_index = [
        {
            "id": "alice_latest",
            "filename": "task.docx",
            "headline": "Alice latest task document",
            "content": "summary: latest requirements",
            "uploader_name": "Alice",
            "uploader_uin": "u1",
            "file_size": 2048,
            "download_status": "done",
            "eff_salience": 5,
            "upload_time": 2_000_000_100,
            "same_name_version_rank": 1,
            "same_name_version_count": 2,
        },
        {
            "id": "alice_old",
            "filename": "task.docx",
            "headline": "Alice older task document",
            "content": "summary: older chapter 6 requirements",
            "uploader_name": "Alice",
            "uploader_uin": "u1",
            "file_size": 2048,
            "download_status": "done",
            "eff_salience": 4,
            "upload_time": 2_000_000_000,
            "same_name_version_rank": 2,
            "same_name_version_count": 2,
        },
    ]

    msgs = ctx_build.build_base_context(
        user_name="Alice",
        current_user_id="u1",
        current_message="看一下之前那个 task.docx",
        hot_user=[],
        hot_group=[],
        warm_user_index=[],
        warm_group_index=[],
        cold_user_topk=[],
        cold_group_topk=[],
        kb_topk=[],
        file_index=file_index,
    )
    sys_text = msgs[0]["content"]

    assert "[alice_latest]" in sys_text
    assert "same-name newest-version/2 same-speaker" in sys_text
    assert "[alice_old]" in sys_text
    assert "same-name older-version 2/2 same-speaker" in sys_text
    assert "summary: latest requirements" in sys_text
    assert "summary: older chapter 6 requirements" in sys_text


def test_hot_user_history_is_append_only_for_existing_entries():
    now = datetime.now(timezone.utc)
    first_entries = [
        HotMessage(role="user", content="第一轮问题", turn_id="t1", created_at=now),
        HotMessage(
            role="assistant",
            content="第一轮回答<bot_log>status=done;artifact=a.txt;details=verified</bot_log>",
            turn_id="t1",
            created_at=now,
        ),
    ]
    appended_entries = first_entries + [
        HotMessage(role="user", content="第二轮问题", turn_id="t2", created_at=now),
        HotMessage(role="assistant", content="第二轮回答", turn_id="t2", created_at=now),
    ]

    before = ctx_build._format_hot_user_history(first_entries)
    after = ctx_build._format_hot_user_history(appended_entries)

    stable_prefix_before_count = before.split("\n\nHistory entry count:", 1)[0].rstrip()
    stable_prefix_after_count = after.split("\n\n[User] 第二轮问题", 1)[0].rstrip()

    assert stable_prefix_after_count == stable_prefix_before_count
    assert "details=verified" not in before


def test_historical_visible_tool_markup_is_sanitized_but_bot_log_is_kept():
    now = datetime.now(timezone.utc)
    history = [
        HotMessage(
            role="assistant",
            content=(
                '我先读文件 <read file="app/core/context.py" start="1" end="80">'
                "<bot_log>intent=checked files | note=kept evidence</bot_log>"
            ),
            turn_id="t1",
            created_at=now,
        ),
    ]

    text = ctx_build._format_hot_user_history(history)

    assert '<read file="app/core/context.py"' not in text
    assert "[internal tool/action markup omitted from historical visible text]" in text
    assert "bot_log_brief" in text
    assert "intent=checked files" in text


def test_round3_recent_bot_log_history_uses_neutral_work_record_label():
    from app.schemas.api import ResponsePlan

    now = datetime.now(timezone.utc)
    hot = [
        HotMessage(
            role="assistant",
            content=(
                "已处理"
                "<bot_log>intent=inspect page | helpers={done:[fetch_page]} | "
                "note=read helper report from _helpers_shared/fetch/page.txt and _delegate_fetch</bot_log>"
            ),
            turn_id="t1",
            created_at=now,
        ),
    ]
    msgs = ctx_build.round3_messages(
        persona="你是助手",
        plan=ResponsePlan(intent="回答追问", key_points=["说明刚才处理了什么"], tone="自然", length_hint="短"),
        user_name="Alice",
        current_message="刚才查了什么",
        hot_user=hot,
        light=False,
    )
    user_text = "\n\n".join(m["content"] for m in msgs if m.get("role") == "user")

    assert "previous work record" in user_text
    assert "private work note" not in user_text.lower()
    assert "fetch_page" not in user_text
    assert "_helpers_shared" not in user_text
    assert "_delegate_" not in user_text
    assert "helper" not in user_text.lower()


def test_long_historical_assistant_report_is_folded_out_of_current_context():
    now = datetime.now(timezone.utc)
    stale_report = (
        "## 旧审计报告\n\n"
        "### O1：`id(tools)` 缓存键脆弱\n"
        + ("这个旧结论不应作为当前工程事实。\n" * 120)
    )
    hot = [
        HotMessage(role="assistant", content=stale_report, turn_id="t1", created_at=now),
    ]

    history_text = ctx_build._format_hot_user_history(hot)
    round1_user = ctx_build.round1_messages_light("用户", "检查当前工程", hot)[1]["content"]

    assert "historical assistant long reply omitted" in history_text
    assert "历史 assistant 长回复已折叠" in history_text
    assert "id(tools)" not in history_text
    assert "Historical assistant long replies may be stale" in round1_user
    assert "id(tools)" not in round1_user


def test_recent_group_messages_sanitize_old_internal_markup():
    text = ctx_build._format_recent_group_messages([
        {
            "created_at": "2026-06-07T12:00:00",
            "user_name": "Assistant",
            "content": '<env_read path="app/core/context.py" start_line="1" end_line="80" /> 已检查',
            "addressed_bot": False,
        }
    ])

    assert "<env_read" not in text
    assert "[internal tool/action markup omitted from historical visible text]" in text

