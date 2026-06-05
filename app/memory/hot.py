"""
热记忆：用户最近若干轮 + 群组最近若干事件。

用户热记忆按 (archive_id, group_id, user_id) 隔离。
群组热记忆按 (archive_id, group_id) 共享，所有成员可见。

群组事件中"他人发言"以第三人称转写存储（防注入），同时保留原文供审计。
"""
from __future__ import annotations

from typing import Optional
from datetime import datetime
import ulid

from app.db.pool import pool
from app.config import settings
from app.schemas.api import HotMessage, GroupEvent


# ── 用户热记忆 ────────────────────────────────────────────────
async def append_user_turn(
    archive_id: str,
    group_id: str,
    user_id: str,
    user_content: str,
    assistant_content: str,
) -> str:
    """写入一对用户/助手消息。返回 turn_id。"""
    turn_id = str(ulid.ULID())
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO hot_user_turns
                    (archive_id, group_id, user_id, role, content, turn_id)
                VALUES ($1, $2, $3, 'user', $4, $5),
                       ($1, $2, $3, 'assistant', $6, $5)
                """,
                archive_id, group_id, user_id,
                user_content, turn_id, assistant_content,
            )
    # 2026-05-16 Dream: emit 事件唤醒 supervisor (信息量驱动)
    try:
        from app.core.dream import event_bus
        await event_bus.emit("hot_turn_added",
                              archive_id=archive_id, group_id=group_id, user_id=user_id)
    except Exception:
        pass  # event_bus 失败不影响主流程
    return turn_id


async def load_user_hot(
    archive_id: str,
    group_id: str,
    user_id: str,
    limit_turns: Optional[int] = None,
) -> list[HotMessage]:
    """加载用户最近 N 轮（每轮含 user+assistant 两条）。"""
    n = limit_turns or settings.hot_user_turns
    # 先取最近 N 个 turn_id，再按时间正序展开
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            WITH recent_turns AS (
                SELECT DISTINCT turn_id, MAX(created_at) AS ts
                FROM hot_user_turns
                WHERE archive_id = $1 AND group_id = $2 AND user_id = $3
                GROUP BY turn_id
                ORDER BY ts DESC
                LIMIT $4
            )
            SELECT t.role, t.content, t.turn_id, t.created_at
            FROM hot_user_turns t
            JOIN recent_turns r ON t.turn_id = r.turn_id
            WHERE t.archive_id = $1 AND t.group_id = $2 AND t.user_id = $3
            ORDER BY t.created_at ASC
            """,
            archive_id, group_id, user_id, n,
        )
    return [
        HotMessage(
            role=r["role"],
            content=r["content"],
            turn_id=r["turn_id"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


async def get_user_hot_overflow(
    archive_id: str,
    group_id: str,
    user_id: str,
) -> list[HotMessage]:
    """
    返回超出热记忆压缩阈值的最旧若干轮（待压缩）。

    压缩阈值 = hot_user_turns × 0.8 (默认 80 × 0.8 = 64 轮)。
    之前 50% 触发意味着每两轮对话就压缩一次,每次跑完整 LLM。
    改为 80% 大幅减少不必要的压缩调用(减少 LLM 调用 60%+)。
    设计权衡:延后压缩 → 内存占用稍高,但 hot 容量本就有冗余设计。
    hot_user_turns 是硬容量上限(load 时最多拉 80 轮),
    overflow 实际触发于 80% 处(64 轮)。
    """
    # 超过 hot_user_turns*0.8 的旧轮定义为 overflow(待压缩)
    threshold = max(1, int(settings.hot_user_turns * 0.8))
    n = threshold
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            WITH all_turns AS (
                SELECT turn_id, MAX(created_at) AS ts
                FROM hot_user_turns
                WHERE archive_id = $1 AND group_id = $2 AND user_id = $3
                GROUP BY turn_id
            ),
            ranked AS (
                SELECT turn_id, ts,
                       ROW_NUMBER() OVER (ORDER BY ts DESC) AS rn
                FROM all_turns
            ),
            overflow_turns AS (
                SELECT turn_id FROM ranked WHERE rn > $4
            )
            SELECT t.role, t.content, t.turn_id, t.created_at
            FROM hot_user_turns t
            JOIN overflow_turns o ON t.turn_id = o.turn_id
            WHERE t.archive_id = $1 AND t.group_id = $2 AND t.user_id = $3
            ORDER BY t.created_at ASC
            """,
            archive_id, group_id, user_id, n,
        )
    return [
        HotMessage(
            role=r["role"], content=r["content"],
            turn_id=r["turn_id"], created_at=r["created_at"],
        )
        for r in rows
    ]


async def delete_user_hot_turns(
    archive_id: str,
    group_id: str,
    user_id: str,
    turn_ids: list[str],
) -> None:
    if not turn_ids:
        return
    async with pool().acquire() as conn:
        await conn.execute(
            """
            DELETE FROM hot_user_turns
            WHERE archive_id = $1 AND group_id = $2 AND user_id = $3
              AND turn_id = ANY($4::text[])
            """,
            archive_id, group_id, user_id, turn_ids,
        )


# ── 群组热记忆 ────────────────────────────────────────────────
async def append_group_event(
    archive_id: str,
    group_id: str,
    actor_user_id: Optional[str],
    actor_name: str,
    narration: str,
    raw_content: Optional[str] = None,
    kind: str = "narration",
) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO group_events
                (archive_id, group_id, actor_user_id, actor_name, narration, raw_content, kind)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            archive_id, group_id, actor_user_id, actor_name, narration, raw_content, kind,
        )


async def load_group_hot(
    archive_id: str,
    group_id: str,
    limit: Optional[int] = None,
) -> list[GroupEvent]:
    n = limit or settings.hot_group_events
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT actor_user_id, actor_name, narration, created_at
            FROM (
                SELECT actor_user_id, actor_name, narration, created_at
                FROM group_events
                WHERE archive_id = $1 AND group_id = $2
                  AND kind = 'narration'
                  AND narration NOT LIKE '（进度报告）%'
                ORDER BY created_at DESC
                LIMIT $3
            ) sub
            ORDER BY created_at ASC
            """,
            archive_id, group_id, n,
        )
    return [
        GroupEvent(
            actor_user_id=r["actor_user_id"],
            actor_name=r["actor_name"],
            narration=r["narration"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


async def get_group_events_overflow(
    archive_id: str,
    group_id: str,
) -> list[dict]:
    """返回超出群组热记忆容量的最旧若干事件（待压缩）。

    #8 修:从 50% 触发改为 80% 触发(同 user hot)。
    """
    n = max(1, int(settings.hot_group_events * 0.8))
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, actor_user_id, actor_name, narration, raw_content, created_at
            FROM group_events
            WHERE archive_id = $1 AND group_id = $2
              AND id NOT IN (
                  SELECT id FROM group_events
                  WHERE archive_id = $1 AND group_id = $2
                  ORDER BY created_at DESC
                  LIMIT $3
              )
            ORDER BY created_at ASC
            """,
            archive_id, group_id, n,
        )
    return [dict(r) for r in rows]


async def delete_group_events(
    archive_id: str,
    group_id: str,
    event_ids: list[int],
) -> None:
    if not event_ids:
        return
    async with pool().acquire() as conn:
        await conn.execute(
            """
            DELETE FROM group_events
            WHERE archive_id = $1 AND group_id = $2
              AND id = ANY($3::bigint[])
            """,
            archive_id, group_id, event_ids,
        )
