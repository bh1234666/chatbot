"""
存档与人设的简单 DAO。
"""
from __future__ import annotations

from typing import Optional
import ulid

from app.db.pool import pool


# ── 存档 ────────────────────────────────────────────────────
async def create_archive(name: str) -> dict:
    archive_id = f"arch_{ulid.ULID()}"
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO archives (archive_id, name)
            VALUES ($1, $2)
            RETURNING archive_id, name, created_at
            """,
            archive_id, name,
        )
    return dict(row)


async def get_archive(archive_id: str) -> Optional[dict]:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT archive_id, name, created_at
            FROM archives
            WHERE archive_id = $1 AND deleted_at IS NULL
            """,
            archive_id,
        )
    return dict(row) if row else None


async def list_archives() -> list[dict]:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT archive_id, name, created_at
            FROM archives
            WHERE deleted_at IS NULL
            ORDER BY created_at DESC
            """
        )
    return [dict(r) for r in rows]


async def soft_delete_archive(archive_id: str) -> bool:
    # 2026-05-11 F7 修: 不再依赖 conn.execute() 返回的状态串 "UPDATE N" 解析,
    # 因为字符串契约写在 db_pool 注释里、跨实现脆弱(将来切 asyncpg 或换 DB 层
    # 都可能破)。改成 RETURNING + fetchrow,语义干净:有行返回 = 真删了。
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE archives SET deleted_at = NOW()
            WHERE archive_id = $1 AND deleted_at IS NULL
            RETURNING archive_id
            """,
            archive_id,
        )
    return row is not None


# ── 人设 ────────────────────────────────────────────────────
def _default_persona() -> str:
    from app.memory.persona_files import get_default_persona_content
    return get_default_persona_content()


async def upsert_persona(archive_id: str, content: str) -> dict:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO personas (archive_id, content)
            VALUES ($1, $2)
            ON CONFLICT (archive_id) DO UPDATE
                SET content = EXCLUDED.content, updated_at = NOW()
            RETURNING archive_id, content, updated_at
            """,
            archive_id, content,
        )
    return dict(row)


async def reload_personas_from_files() -> int:
    """Refresh stored archive personas from matching personas/*.md files."""
    from app.memory.persona_files import (
        resolve_persona_file_by_content,
        resolve_persona_file_by_label,
    )

    updated = 0
    async with pool().acquire() as conn:
        label_rows = await conn.fetch(
            """
            SELECT archive_id, persona_label
            FROM bot_group_personas
            WHERE persona_label <> ''
            ORDER BY created_at DESC
            """
        )
        labels_by_archive: dict[str, list[str]] = {}
        for label_row in label_rows:
            labels_by_archive.setdefault(label_row["archive_id"], []).append(
                label_row["persona_label"] or ""
            )
        rows = await conn.fetch(
            "SELECT archive_id, content FROM personas"
        )
        for row in rows:
            pf = None
            for label in labels_by_archive.get(row["archive_id"], []):
                pf = resolve_persona_file_by_label(label)
                if pf:
                    break
            if not pf:
                pf = resolve_persona_file_by_content(row["content"] or "")
            if not pf or pf.content == row["content"]:
                continue
            await conn.execute(
                """
                UPDATE personas
                SET content = $1, updated_at = NOW()
                WHERE archive_id = $2
                """,
                pf.content, row["archive_id"],
            )
            updated += 1
    return updated


async def get_persona(archive_id: str) -> str:
    """返回人设内容；不存在则返回默认人设。"""
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT content FROM personas WHERE archive_id = $1",
            archive_id,
        )
    return row["content"] if row else _default_persona()


async def get_persona_full(archive_id: str) -> Optional[dict]:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT archive_id, content, updated_at FROM personas WHERE archive_id = $1",
            archive_id,
        )
    return dict(row) if row else None
