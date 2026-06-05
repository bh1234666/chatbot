"""
群消息原始日志。所有群消息（含机器人未参与的）进此表，
是知识库（cold_nodes scope='kb'）的源数据。M3 处理 KB 压缩。
"""
from __future__ import annotations

from typing import Optional
from app.db.pool import pool


async def append_message(
    archive_id: str,
    group_id: str,
    user_id: Optional[str],
    user_name: str,
    content: str,
    addressed_bot: bool,
) -> int:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO group_messages
                (archive_id, group_id, user_id, user_name, content, addressed_bot)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            archive_id, group_id, user_id, user_name, content, addressed_bot,
        )
    # 2026-05-16 Dream: emit 事件 (群里持续接收消息 → 信息量增长)
    try:
        from app.core.dream import event_bus
        await event_bus.emit("group_msg_added",
                              archive_id=archive_id, group_id=group_id,
                              addressed_bot=addressed_bot)
    except Exception:
        pass
    return row["id"]


async def load_recent(
    archive_id: str,
    group_id: str,
    limit: int = 30,
) -> list[dict]:
    """加载最近 N 条群消息（含已被 KB 消化的）按时间正序返回。

    用途: per-user 并行模式下,机器人需要看到群里"实际发生了什么",而不仅是
    自己参与过的 group_events。否则用户 B 问"用户 A 刚才说什么"时,B 的机器人
    上下文里可能根本没出现 A 的消息(group_events 只记录机器人参与的事件;A 还在
    跟机器人交互中,event 还没写入)。

    返回字段精简: id / user_id / user_name / content / addressed_bot / created_at
    每条消息原文截断到 800 字符,防止上下文炸。
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, user_name, content, addressed_bot, created_at
            FROM group_messages
            WHERE archive_id = $1 AND group_id = $2
            ORDER BY created_at DESC, id DESC
            LIMIT $3
            """,
            archive_id, group_id, limit,
        )
    out = []
    for r in rows:
        d = dict(r)
        c = d["content"] or ""
        if len(c) > 800:
            c = c[:800] + "…[截断]"
        d["content"] = c
        out.append(d)
    out.reverse()  # 倒序取最近 N,再反转为正序展示
    return out


async def load_unprocessed(
    archive_id: str,
    group_id: str,
    limit: int = 200,
) -> list[dict]:
    """加载尚未被 KB 消化的群消息（M3 用）。"""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, user_name, content, addressed_bot, created_at
            FROM group_messages
            WHERE archive_id = $1 AND group_id = $2
              AND kb_processed = FALSE AND COALESCE(kb_processing, 0) = 0
            ORDER BY created_at ASC, id ASC
            LIMIT $3
            """,
            archive_id, group_id, limit,
        )
    return [dict(r) for r in rows]


async def claim_unprocessed(
    archive_id: str,
    group_id: str,
    limit: int = 200,
) -> list[dict]:
    """Claim unprocessed group messages for KB compression."""
    async with pool().acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT id, user_id, user_name, content, addressed_bot, created_at
                FROM group_messages
                WHERE archive_id = $1 AND group_id = $2
                  AND kb_processed = FALSE AND COALESCE(kb_processing, 0) = 0
                ORDER BY created_at ASC, id ASC
                LIMIT $3
                """,
                archive_id, group_id, limit,
            )
            ids = [int(r["id"]) for r in rows]
            if ids:
                await conn.execute(
                    """
                    UPDATE group_messages SET kb_processing = TRUE
                    WHERE archive_id = $1 AND group_id = $2
                      AND id = ANY($3::bigint[])
                    """,
                    archive_id, group_id, ids,
                )
    return [dict(r) for r in rows]


async def release_processing(
    archive_id: str,
    group_id: str,
    ids: list[int],
) -> None:
    if not ids:
        return
    async with pool().acquire() as conn:
        await conn.execute(
            """
            UPDATE group_messages SET kb_processing = FALSE
            WHERE archive_id = $1 AND group_id = $2
              AND kb_processed = FALSE
              AND id = ANY($3::bigint[])
            """,
            archive_id, group_id, ids,
        )


async def mark_processed(
    archive_id: str,
    group_id: str,
    ids: list[int],
) -> None:
    if not ids:
        return
    async with pool().acquire() as conn:
        await conn.execute(
            """
            UPDATE group_messages SET kb_processed = TRUE, kb_processing = FALSE
            WHERE archive_id = $1 AND group_id = $2
              AND id = ANY($3::bigint[])
            """,
            archive_id, group_id, ids,
        )
