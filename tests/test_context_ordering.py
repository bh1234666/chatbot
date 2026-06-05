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

