"""
Bot group configuration DAO.
Manages which groups the bot participates in and which persona is active.
"""
from __future__ import annotations

from typing import Optional
from app.db.pool import pool


# ── Group config ──────────────────────────────────────────────

async def get_group_config(group_id: str) -> Optional[dict]:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT group_id, active_archive_id, participate, group_name, "
            "created_at, updated_at FROM bot_group_config WHERE group_id = $1",
            group_id,
        )
    return dict(row) if row else None


async def is_participating(group_id: str) -> bool:
    async with pool().acquire() as conn:
        val = await conn.fetchval(
            "SELECT participate FROM bot_group_config WHERE group_id = $1",
            group_id,
        )
    return bool(val)


async def get_active_archive(group_id: str) -> Optional[str]:
    async with pool().acquire() as conn:
        return await conn.fetchval(
            "SELECT active_archive_id FROM bot_group_config WHERE group_id = $1 AND participate = 1",
            group_id,
        )


async def join_group(group_id: str, archive_id: str, group_name: str = "", persona_label: str = "") -> dict:
    """Join a group: start participating with the given persona."""
    # 2026-05-15 PG support (Item 8):
    # - datetime('now') 改 NOW(): _translate_sql 会替 sqlite 翻译回去, PG 原生支持。
    # - INSERT OR IGNORE 改 INSERT ... ON CONFLICT DO NOTHING: 两个方言都支持
    #   (SQLite ≥ 3.24, PG 原生)。
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO bot_group_config (group_id, active_archive_id, participate, group_name, updated_at)
                VALUES ($1, $2, 1, $3, NOW())
                ON CONFLICT (group_id) DO UPDATE
                    SET active_archive_id = EXCLUDED.active_archive_id,
                        participate = 1,
                        group_name = EXCLUDED.group_name,
                        updated_at = NOW()
                """,
                group_id, archive_id, group_name,
            )
            await conn.execute(
                """
                INSERT INTO bot_group_personas (group_id, archive_id, persona_label)
                VALUES ($1, $2, $3)
                ON CONFLICT (group_id, archive_id) DO NOTHING
                """,
                group_id, archive_id, persona_label,
            )
        return await get_group_config(group_id)


async def leave_group(group_id: str) -> None:
    """Stop participating in a group (keep memory and persona config)."""
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE bot_group_config SET participate = 0, updated_at = NOW() WHERE group_id = $1",
            group_id,
        )


async def delete_group(group_id: str) -> list[str]:
    """彻底删除群: 删除群配置 + 所有群内人设关联。
    Returns: 该群关联过的所有 archive_id 列表 (用于后续清理)。
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT archive_id FROM bot_group_personas WHERE group_id = $1",
            group_id,
        )
        archive_ids = [r["archive_id"] for r in rows]
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM bot_group_personas WHERE group_id = $1",
                group_id,
            )
            await conn.execute(
                "DELETE FROM bot_group_config WHERE group_id = $1",
                group_id,
            )
    return archive_ids


async def set_participate(group_id: str, participate: bool) -> None:
    val = 1 if participate else 0
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE bot_group_config SET participate = $1, updated_at = NOW() WHERE group_id = $2",
            val, group_id,
        )


async def list_groups() -> list[dict]:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT g.group_id, g.active_archive_id, g.participate, g.group_name,
                   g.created_at, g.updated_at
            FROM bot_group_config g
            ORDER BY g.updated_at DESC
            """
        )
    out = []
    for r in rows:
        d = dict(r)
        d["participate"] = bool(d["participate"])
        d["personas"] = await list_personas(d["group_id"])
        out.append(d)
    return out


# ── Personas per group ───────────────────────────────────────

async def add_persona(group_id: str, archive_id: str, label: str = "") -> None:
    # 2026-05-15 PG support (Item 8): INSERT OR REPLACE 改 ON CONFLICT DO UPDATE,
    # 两个方言都支持(SQLite ≥ 3.24)。
    async with pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bot_group_personas (group_id, archive_id, persona_label)
            VALUES ($1, $2, $3)
            ON CONFLICT (group_id, archive_id) DO UPDATE
                SET persona_label = EXCLUDED.persona_label
            """,
            group_id, archive_id, label,
        )


async def remove_persona(group_id: str, archive_id: str) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM bot_group_personas WHERE group_id = $1 AND archive_id = $2",
            group_id, archive_id,
        )


async def activate_persona(group_id: str, archive_id: str) -> None:
    """Switch the active persona for a group."""
    async with pool().acquire() as conn:
        async with conn.transaction():
            # Ensure persona is registered
            await conn.execute(
                """
                INSERT INTO bot_group_personas (group_id, archive_id, persona_label)
                VALUES ($1, $2, '')
                ON CONFLICT (group_id, archive_id) DO NOTHING
                """,
                group_id, archive_id,
            )
            await conn.execute(
                """
                UPDATE bot_group_config
                SET active_archive_id = $1, updated_at = NOW()
                WHERE group_id = $2
                """,
                archive_id, group_id,
            )


async def list_personas(group_id: str) -> list[dict]:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.archive_id, a.name AS archive_name, p.persona_label,
                   p.created_at, p.last_summary, p.last_summary_at,
                   CASE WHEN g.active_archive_id = p.archive_id THEN 1 ELSE 0 END AS is_active
            FROM bot_group_personas p
            LEFT JOIN bot_group_config g ON g.group_id = p.group_id
            LEFT JOIN archives a ON a.archive_id = p.archive_id
            WHERE p.group_id = $1
            ORDER BY p.created_at DESC
            """,
            group_id,
        )
    return [dict(r) for r in rows]


async def update_last_summary(
    group_id: str, archive_id: str, summary: str
) -> None:
    """Store the latest conversation summary for a persona in a group."""
    async with pool().acquire() as conn:
        await conn.execute(
            """
            UPDATE bot_group_personas
            SET last_summary = $1, last_summary_at = NOW()
            WHERE group_id = $2 AND archive_id = $3
            """,
            summary, group_id, archive_id,
        )


# ── Bot settings (key-value) ─────────────────────────────────

async def get_setting(key: str) -> str | None:
    """Get a bot setting by key. Returns None if not set."""
    async with pool().acquire() as conn:
        val = await conn.fetchval(
            "SELECT value FROM bot_settings WHERE key = $1", key,
        )
    return val if val else None


async def set_setting(key: str, value: str) -> None:
    """Set a bot setting key-value pair."""
    async with pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bot_settings (key, value)
            VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            key, value,
        )
