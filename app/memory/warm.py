"""
温记忆。

读取：
  - load_user_warm_index / load_group_warm_index：返回 headline 列表，
    用于在 ctx_base 中作为索引呈现。

写入（压缩）：
  - compress_user_overflow：把溢出的用户热记忆（成对 turn）压缩为
    若干温记忆条目，由 LLM 自决话题边界。
  - compress_group_overflow：把溢出的群组事件（仅机器人参与的）压缩。

展开：
  - expand_warm：根据 id 列表返回 headline+summary+internal_hint。
    M3 起被 Round2 工具调用机制使用；M2 起对外 API 即可调。

设计原则：
  - LLM 自决话题边界：每条压缩条目可覆盖 1~多个 turn/event；
    要求所有输入必须被覆盖（防止信息丢失）。
  - 消毒：summary/headline/internal_hint 经 sanitize 处理。
  - 一致性：压缩成功 → 删除源 hot 记录；压缩失败 → 保留源记录下次重试。
"""
from __future__ import annotations

from app.memory.prompt_catalog import (
    WARM_USER_COMPRESS_SYSTEM,
    WARM_GROUP_COMPRESS_SYSTEM,
)
_USER_COMPRESS_SYSTEM = WARM_USER_COMPRESS_SYSTEM
_GROUP_COMPRESS_SYSTEM = WARM_GROUP_COMPRESS_SYSTEM


import json
import logging
from typing import Optional

import ulid

from app.config import settings
from app.db.pool import pool
from app.llm import client as llm
from app.core.sanitize import sanitize_headline, sanitize_summary, sanitize_hint
from app.schemas.api import HotMessage


log = logging.getLogger(__name__)


# ── 索引读取 ────────────────────────────────────────────────
async def load_user_warm_index(
    archive_id: str, group_id: str, user_id: str,
) -> list[dict]:
    """按时间倒序返回用户温记忆 headline 列表。"""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, headline, tendencies, created_at
            FROM warm_memories
            WHERE archive_id = $1 AND group_id = $2 AND user_id = $3
              AND scope = 'user'
            ORDER BY created_at DESC
            LIMIT $4
            """,
            archive_id, group_id, user_id, settings.warm_user_max,
        )
    return [
        {
            "id": r["id"],
            "headline": r["headline"],
            "timestamp": _to_datetime(r["created_at"]).strftime("%Y-%m-%d %H:%M"),
            "tendencies": _decode_jsonb(r["tendencies"]),
        }
        for r in rows
    ]


async def load_group_warm_index(
    archive_id: str, group_id: str,
) -> list[dict]:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, headline, tendencies, created_at
            FROM warm_memories
            WHERE archive_id = $1 AND group_id = $2 AND scope = 'group'
            ORDER BY created_at DESC
            LIMIT $3
            """,
            archive_id, group_id, settings.warm_group_max,
        )
    return [
        {
            "id": r["id"],
            "headline": r["headline"],
            "timestamp": _to_datetime(r["created_at"]).strftime("%Y-%m-%d %H:%M"),
            "tendencies": _decode_jsonb(r["tendencies"]),
        }
        for r in rows
    ]


# ── 溢出查询：温→冷压缩用 ───────────────────────────────────
async def get_user_warm_overflow(
    archive_id: str, group_id: str, user_id: str,
) -> list[dict]:
    """
    若用户温记忆数超过 80% 容量,返回最旧的 warm_to_cold_batch 条用于压缩。
    返回完整字段（含 summary、internal_hint），供 LLM 压缩。

    #8 修(2026-05-01): 触发阈值从 50% 改成 80%(对齐 hot 层的 #8 修)。
    旧版 50% 触发 → 每两轮就压一次 warm→cold,LLM 调用量 ~60% 是浪费;
    且 docstring/注释/实际行为三处不一致(docstring 说">warm_user_max",
    注释说"half capacity",实际是 ">max/2")。现统一为 80%。
    """
    threshold = max(1, int(settings.warm_user_max * 0.8))
    async with pool().acquire() as conn:
        # 先看总数；不够阈值则返回空
        total = await conn.fetchval(
            """
            SELECT COUNT(*) FROM warm_memories
            WHERE archive_id = $1 AND group_id = $2 AND user_id = $3
              AND scope = 'user'
            """,
            archive_id, group_id, user_id,
        )
        if total <= threshold:
            return []
        rows = await conn.fetch(
            """
            SELECT id, headline, summary, internal_hint,
                   tendencies, entities, source_refs, created_at
            FROM warm_memories
            WHERE archive_id = $1 AND group_id = $2 AND user_id = $3
              AND scope = 'user'
            ORDER BY created_at ASC
            LIMIT $4
            """,
            archive_id, group_id, user_id, settings.warm_to_cold_batch,
        )
    return [_warm_row_to_dict(r) for r in rows]


async def get_group_warm_overflow(
    archive_id: str, group_id: str,
) -> list[dict]:
    """同 get_user_warm_overflow,触发阈值 = warm_group_max 的 80%。"""
    threshold = max(1, int(settings.warm_group_max * 0.8))
    async with pool().acquire() as conn:
        total = await conn.fetchval(
            """
            SELECT COUNT(*) FROM warm_memories
            WHERE archive_id = $1 AND group_id = $2 AND scope = 'group'
            """,
            archive_id, group_id,
        )
        if total <= threshold:
            return []
        rows = await conn.fetch(
            """
            SELECT id, headline, summary, internal_hint,
                   tendencies, entities, source_refs, created_at
            FROM warm_memories
            WHERE archive_id = $1 AND group_id = $2 AND scope = 'group'
            ORDER BY created_at ASC
            LIMIT $3
            """,
            archive_id, group_id, settings.warm_to_cold_batch,
        )
    return [_warm_row_to_dict(r) for r in rows]


async def delete_warm_by_ids(archive_id: str, ids: list[str]) -> None:
    if not ids:
        return
    async with pool().acquire() as conn:
        await conn.execute(
            """
            DELETE FROM warm_memories
            WHERE archive_id = $1 AND id = ANY($2::text[])
            """,
            archive_id, ids,
        )


def _warm_row_to_dict(r) -> dict:
    return {
        "id": r["id"],
        "headline": r["headline"],
        "summary": r["summary"],
        "internal_hint": r["internal_hint"] or "",
        "tendencies": _decode_jsonb(r["tendencies"]),
        "entities": _decode_jsonb(r["entities"]),
        "source_refs": _decode_jsonb(r["source_refs"]),
        "timestamp": _to_datetime(r["created_at"]).strftime("%Y-%m-%d %H:%M"),
    }


# ── 展开 ────────────────────────────────────────────────────
async def expand_warm(archive_id: str, ids: list[str]) -> list[dict]:
    """
    展开指定温记忆条目，返回 headline + summary + internal_hint + 元数据。
    同时累加 access_count。
    """
    if not ids:
        return []
    async with pool().acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT id, scope, headline, summary, internal_hint,
                       tendencies, entities, refs_to_cold, created_at
                FROM warm_memories
                WHERE archive_id = $1 AND id = ANY($2::text[])
                """,
                archive_id, ids,
            )
            if rows:
                await conn.execute(
                    """
                    UPDATE warm_memories
                    SET access_count = access_count + 1,
                        last_access = NOW()
                    WHERE archive_id = $1 AND id = ANY($2::text[])
                    """,
                    archive_id, [r["id"] for r in rows],
                )
    return [
        {
            "id": r["id"],
            "scope": r["scope"],
            "headline": r["headline"],
            "summary": r["summary"],
            "internal_hint": r["internal_hint"] or "",
            "tendencies": _decode_jsonb(r["tendencies"]),
            "entities": _decode_jsonb(r["entities"]),
            "refs_to_cold": _decode_jsonb(r["refs_to_cold"]),
            "timestamp": _to_datetime(r["created_at"]).strftime("%Y-%m-%d %H:%M"),
        }
        for r in rows
    ]


# ── 压缩：用户温记忆 ─────────────────────────────────────────


async def compress_user_overflow(
    archive_id: str,
    group_id: str,
    user_id: str,
    overflow: list[HotMessage],
) -> int:
    """把溢出的用户热记忆压缩为温记忆。返回新建条目数。"""
    if not overflow:
        return 0

    convo_text = (
        "## Conversation Turns To Compress\n"
        + _format_user_turns(overflow)
        + "\n\n对话轮次待压缩。"
    )
    all_turn_ids = sorted({m.turn_id for m in overflow})

    msgs = [
        {"role": "system", "content": _USER_COMPRESS_SYSTEM},
        {"role": "user", "content": convo_text},
    ]

    # 校验函数：所有输入 turn_id 必须被覆盖（防止信息丢失）
    expected = set(all_turn_ids)
    def _validate(raw):
        if not isinstance(raw, dict):
            return False
        memories = raw.get("memories") or []
        if not isinstance(memories, list) or not memories:
            return False
        covered: set[str] = set()
        for m in memories:
            for tid in (m.get("turn_ids") or []):
                covered.add(str(tid))
        return expected.issubset(covered)

    raw = await llm.chat_json_with_upgrade(msgs, validate=_validate, label="warm_user")
    if raw is None:
        log.warning("compress_user_overflow: lite+main both failed; turns kept")
        return 0
    memories = raw.get("memories") or []

    # 写入 + 删除源 turn（同事务保证一致性）
    async with pool().acquire() as conn:
        async with conn.transaction():
            count = 0
            for m in memories:
                wid = f"w_{ulid.ULID()}"
                refs = [str(t) for t in (m.get("turn_ids") or [])]
                await conn.execute(
                    """
                    INSERT INTO warm_memories
                        (id, archive_id, group_id, user_id, scope,
                         headline, summary, internal_hint,
                         tendencies, entities, source_refs)
                    VALUES ($1, $2, $3, $4, 'user', $5, $6, $7,
                            $8::jsonb, $9::jsonb, $10::jsonb)
                    """,
                    wid, archive_id, group_id, user_id,
                    sanitize_headline(str(m.get("headline", ""))),
                    sanitize_summary(str(m.get("summary", ""))),
                    sanitize_hint(str(m.get("internal_hint", ""))),
                    json.dumps(_clean_tendencies(m.get("tendencies"))),
                    json.dumps(_clean_str_list(m.get("entities"), max_items=20)),
                    json.dumps(refs),
                )
                count += 1

            await conn.execute(
                """
                DELETE FROM hot_user_turns
                WHERE archive_id = $1 AND group_id = $2 AND user_id = $3
                  AND turn_id = ANY($4::text[])
                """,
                archive_id, group_id, user_id, all_turn_ids,
            )
    return count


def _format_user_turns(turns: list[HotMessage]) -> str:
    lines = []
    for hm in turns:
        ts = hm.created_at.strftime("%m-%d %H:%M")
        lines.append(f"[turn={hm.turn_id} {hm.role} {ts}] {hm.content}")
    return "\n".join(lines)


# ── 压缩：群组温记忆 ─────────────────────────────────────────


async def compress_group_overflow(
    archive_id: str,
    group_id: str,
    overflow: list[dict],
) -> int:
    if not overflow:
        return 0

    text = (
        "## Shared Events To Compress\n"
        + _format_group_events(overflow)
        + "\n\n共享事件待压缩。"
    )
    all_ids = sorted({int(e["id"]) for e in overflow})

    msgs = [
        {"role": "system", "content": _GROUP_COMPRESS_SYSTEM},
        {"role": "user", "content": text},
    ]

    expected_ids = set(all_ids)
    def _validate(raw):
        if not isinstance(raw, dict):
            return False
        memories = raw.get("memories") or []
        if not isinstance(memories, list) or not memories:
            return False
        covered: set[int] = set()
        for m in memories:
            for eid in (m.get("event_ids") or []):
                try:
                    covered.add(int(eid))
                except (ValueError, TypeError):
                    pass
        return expected_ids.issubset(covered)

    raw = await llm.chat_json_with_upgrade(msgs, validate=_validate, label="warm_group")
    if raw is None:
        log.warning("compress_group_overflow: lite+main both failed; events kept")
        return 0
    memories = raw.get("memories") or []

    async with pool().acquire() as conn:
        async with conn.transaction():
            count = 0
            for m in memories:
                wid = f"w_{ulid.ULID()}"
                refs = [str(int(e)) for e in (m.get("event_ids") or [])]
                await conn.execute(
                    """
                    INSERT INTO warm_memories
                        (id, archive_id, group_id, user_id, scope,
                         headline, summary, internal_hint,
                         tendencies, entities, source_refs)
                    VALUES ($1, $2, $3, NULL, 'group', $4, $5, $6,
                            $7::jsonb, $8::jsonb, $9::jsonb)
                    """,
                    wid, archive_id, group_id,
                    sanitize_headline(str(m.get("headline", ""))),
                    sanitize_summary(str(m.get("summary", ""))),
                    sanitize_hint(str(m.get("internal_hint", ""))),
                    json.dumps(_clean_tendencies(m.get("tendencies"))),
                    json.dumps(_clean_str_list(m.get("entities"), max_items=20)),
                    json.dumps(refs),
                )
                count += 1

            await conn.execute(
                """
                DELETE FROM group_events
                WHERE archive_id = $1 AND group_id = $2
                  AND id = ANY($3::bigint[])
                """,
                archive_id, group_id, all_ids,
            )
    return count


def _format_group_events(events: list[dict]) -> str:
    lines = []
    for e in events:
        ts = _fmt_ts(e["created_at"])
        lines.append(
            f"[event={e['id']} {ts} {e['actor_name']}] {e['narration']}"
        )
    return "\n".join(lines)


def _to_datetime(val):
    """Coerce a DB value (datetime or ISO string from SQLite) to datetime."""
    from datetime import datetime as dt
    if isinstance(val, str):
        return dt.fromisoformat(val.replace("Z", "+00:00"))
    return val


def _fmt_ts(val):
    """Format created_at as mm-dd HH:MM, accepting datetime or ISO string (SQLite)."""
    return _to_datetime(val).strftime("%m-%d %H:%M")


# ── 删除接口（运营） ──
async def delete_warm(archive_id: str, group_id: str, ids: list[str]) -> int:
    if not ids:
        return 0
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id FROM warm_memories
            WHERE archive_id = $1 AND group_id = $2 AND id = ANY($3::text[])
            """,
            archive_id, group_id, ids,
        )
        if not rows:
            return 0
        delete_ids = [r["id"] for r in rows]
        await conn.execute(
            """
            DELETE FROM warm_memories
            WHERE archive_id = $1 AND group_id = $2 AND id = ANY($3::text[])
            """,
            archive_id, group_id, delete_ids,
        )
        return len(delete_ids)


# ── 兼容旧 stub 接口名（orchestrator M1 调的是 compress_overflow） ──
compress_overflow = compress_user_overflow


# ── 工具 ────────────────────────────────────────────────────
def _clean_tendencies(t) -> dict[str, float]:
    if not isinstance(t, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in t.items():
        try:
            f = float(v)
            if 0.0 <= f <= 1.0 and isinstance(k, str) and k:
                out[k[:32]] = round(f, 3)
        except (ValueError, TypeError):
            continue
    if len(out) > 16:
        out = dict(sorted(out.items(), key=lambda x: -x[1])[:16])
    return out


def _clean_str_list(lst, max_items: int = 20) -> list[str]:
    if not isinstance(lst, list):
        return []
    out: list[str] = []
    for x in lst:
        if isinstance(x, str) and x.strip():
            out.append(x.strip()[:64])
        if len(out) >= max_items:
            break
    return out


def _decode_jsonb(v):
    """JSONB 字段解码兼容层。

    历史: 这套代码原本基于 asyncpg + Postgres,JSONB 列由驱动直接解码成
    dict/list。后切到 SQLite + aiosqlite 后,JSON 是以 TEXT 存的,读出来
    永远是 str,需要这里 json.loads。
    保留对 dict/list 直通的分支:万一未来切回 asyncpg 不用再改这层。
    """
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return {} if v.startswith("{") else []
    return v
