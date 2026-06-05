"""
冷记忆。

三种作用域共表（cold_nodes.scope）：
  - user:  来自用户温记忆压缩
  - group: 来自群组温记忆压缩
  - kb:    来自群消息压缩（在 app/memory/kb.py 处理）

数据结构：
  - 节点：id / type(fact|preference|event|relationship|topic) / headline / content / salience
  - 边：DAG 中的节点关联（同一对话/同一实体/时间相邻）

呈现：
  - load_cold_user_index / load_cold_group_index 按 effective salience 取 Top-N，
    返回 [{id, type, headline, salience}]，注入 system 后由模型自决展开。
  - effective salience = stored salience * exp(-Δt / half_life)，
    查询时实时计算，避免周期任务。

展开：
  - expand_cold(ids, depth=1|2)：返回节点 + 邻居 headline。
    depth=1：直接邻居；depth=2：二跳。
    访问后更新 access_count、last_access、salience。

压缩：
  - compress_user_warm_to_cold：用户温→用户冷
  - compress_group_warm_to_cold：群组温→群组冷
  - LLM 输出节点 + 边，节点可引用既有冷节点 ID 建立跨节点关联。
"""
from __future__ import annotations

from app.memory.prompt_catalog import (
    COLD_COMPRESS_SYSTEM,
    COLD_AVOID_MATCH_SYSTEM,
)
_COMPRESS_SYSTEM = COLD_COMPRESS_SYSTEM
_AVOID_MATCH_SYSTEM = COLD_AVOID_MATCH_SYSTEM


import json
import logging
from typing import Optional

import ulid

from app.config import settings
from app.db.pool import pool
from app.llm import client as llm
from app.core.sanitize import sanitize_headline, sanitize_summary
from app.memory import warm as warm_mem


log = logging.getLogger(__name__)


# Effective salience with time decay.
# 2026-05-15 PG support (Item 8): SQLite 用 julianday();  PG 用 EXTRACT(EPOCH).
# 二者计算"自上次访问到现在经过的秒数",然后做指数衰减。
def _eff_sql() -> str:
    from app.db.pool import db_kind as _kind
    sec = settings.salience_half_life_days * 86400.0
    if _kind() == "sqlite":
        # julianday('now') - julianday(ts) = 天数, * 86400 = 秒
        return (
            f"(salience * exp(-(julianday('now') - julianday(COALESCE(last_access, created_at)))"
            f" * 86400.0 / {sec}))"
        )
    # postgres: NOW() - ts 返回 interval, EXTRACT(EPOCH FROM ...) 返回秒
    return (
        f"(salience * exp(-(EXTRACT(EPOCH FROM (NOW() - COALESCE(last_access, created_at)))) / {sec}))"
    )


def _eff_sql_aliased(alias: str) -> str:
    from app.db.pool import db_kind as _kind
    sec = settings.salience_half_life_days * 86400.0
    if _kind() == "sqlite":
        return (
            f"({alias}.salience * exp(-(julianday('now') - julianday(COALESCE({alias}.last_access, {alias}.created_at)))"
            f" * 86400.0 / {sec}))"
        )
    return (
        f"({alias}.salience * exp(-(EXTRACT(EPOCH FROM (NOW() - COALESCE({alias}.last_access, {alias}.created_at)))) / {sec}))"
    )


# ── 索引读取（注入 system） ──────────────────────────────────
async def load_cold_user_index(
    archive_id: str, group_id: str, user_id: str,
    limit: Optional[int] = None,
) -> list[dict]:
    """用户冷记忆索引。avoid_mention 直接在节点字段（用户冷天然按用户隔离）。"""
    n = limit or settings.cold_user_index_topn
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, node_type, headline, salience,
                   avoid_mention, avoid_reason,
                   {_eff_sql()} AS eff_salience
            FROM cold_nodes
            WHERE archive_id = $1 AND group_id = $2 AND user_id = $3
              AND scope = 'user'
            ORDER BY eff_salience DESC, last_access DESC NULLS LAST
            LIMIT $4
            """,
            archive_id, group_id, user_id, n,
        )
    return [_index_row(r) for r in rows]


async def load_cold_group_index(
    archive_id: str, group_id: str,
    *,
    viewer_user_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """
    群组冷记忆索引。viewer_user_id 用于 LEFT JOIN node_user_avoid，
    呈现"该用户视角"下的 avoid_mention 标记。viewer_user_id=None 时不做遮罩。
    """
    n = limit or settings.cold_group_index_topn
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT cn.id, cn.node_type, cn.headline, cn.salience,
                   (nua.node_id IS NOT NULL) AS avoid_mention,
                   nua.reason AS avoid_reason,
                   {_eff_sql_aliased('cn')} AS eff_salience
            FROM cold_nodes cn
            LEFT JOIN node_user_avoid nua
              ON nua.archive_id = cn.archive_id
             AND nua.node_id = cn.id
             AND nua.user_id = $4
            WHERE cn.archive_id = $1 AND cn.group_id = $2 AND cn.scope = 'group'
            ORDER BY eff_salience DESC, cn.last_access DESC NULLS LAST
            LIMIT $3
            """,
            archive_id, group_id, n, viewer_user_id or "",
        )
    return [_index_row(r) for r in rows]


def _index_row(r) -> dict:
    return {
        "id": r["id"],
        "type": r["node_type"],
        "headline": r["headline"],
        "avoid_mention": bool(r["avoid_mention"]),
        "avoid_reason": r["avoid_reason"] or "",
    }


# ── 展开 ────────────────────────────────────────────────────
async def expand_cold(
    archive_id: str, ids: list[str], depth: int = 1,
    *, viewer_user_id: Optional[str] = None,
) -> list[dict]:
    """
    返回每个节点的 content + 邻居信息，含 avoid_mention 标记。
    viewer_user_id 用于解析群组冷/KB 节点的用户级遮罩。
    depth=1：直接邻居 headline
    depth=2：邻居 + 二跳邻居 headline
    """
    if not ids:
        return []
    depth = max(1, min(int(depth), 2))

    async with pool().acquire() as conn:
        async with conn.transaction():
            # 取主节点（带遮罩计算）
            rows = await conn.fetch(
                """
                SELECT cn.id, cn.scope, cn.node_type, cn.headline, cn.content,
                       cn.salience, cn.access_count, cn.source_refs,
                       cn.created_at, cn.last_access,
                       cn.user_id, cn.file_metadata,
                       CASE
                         WHEN cn.scope = 'user' THEN cn.avoid_mention
                         ELSE (nua.node_id IS NOT NULL)
                       END AS avoid_mention,
                       CASE
                         WHEN cn.scope = 'user' THEN cn.avoid_reason
                         ELSE nua.reason
                       END AS avoid_reason
                FROM cold_nodes cn
                LEFT JOIN node_user_avoid nua
                  ON nua.archive_id = cn.archive_id
                 AND nua.node_id = cn.id
                 AND nua.user_id = $3
                WHERE cn.archive_id = $1 AND cn.id = ANY($2::text[])
                """,
                archive_id, ids, viewer_user_id or "",
            )
            if not rows:
                return []

            main_ids = [r["id"] for r in rows]

            # 取一跳邻居（双向）
            edge_rows = await conn.fetch(
                """
                SELECT src_id, dst_id, weight FROM cold_edges
                WHERE archive_id = $1
                  AND (src_id = ANY($2::text[]) OR dst_id = ANY($2::text[]))
                """,
                archive_id, main_ids,
            )
            main_set = set(main_ids)
            neighbor_ids = set()
            adj: dict[str, list[tuple[str, float]]] = {i: [] for i in main_ids}
            for er in edge_rows:
                src, dst, w = er["src_id"], er["dst_id"], er["weight"]
                if src in main_set and dst not in main_set:
                    neighbor_ids.add(dst)
                    adj.setdefault(src, []).append((dst, w))
                elif dst in main_set and src not in main_set:
                    neighbor_ids.add(src)
                    adj.setdefault(dst, []).append((src, w))

            # 二跳
            second_hop_ids: set[str] = set()
            second_adj: dict[str, list[str]] = {}
            if depth >= 2 and neighbor_ids:
                hop2 = await conn.fetch(
                    """
                    SELECT src_id, dst_id FROM cold_edges
                    WHERE archive_id = $1
                      AND (src_id = ANY($2::text[]) OR dst_id = ANY($2::text[]))
                    """,
                    archive_id, list(neighbor_ids),
                )
                neighbor_set = neighbor_ids | main_set
                for er in hop2:
                    s, d = er["src_id"], er["dst_id"]
                    if s in neighbor_ids and d not in neighbor_set:
                        second_hop_ids.add(d)
                        second_adj.setdefault(s, []).append(d)
                    elif d in neighbor_ids and s not in neighbor_set:
                        second_hop_ids.add(s)
                        second_adj.setdefault(d, []).append(s)

            # 邻居元数据（带遮罩）
            all_aux_ids = list(neighbor_ids | second_hop_ids)
            aux_meta: dict[str, dict] = {}
            if all_aux_ids:
                aux_rows = await conn.fetch(
                    """
                    SELECT cn.id, cn.scope, cn.node_type, cn.headline,
                           CASE
                             WHEN cn.scope = 'user' THEN cn.avoid_mention
                             ELSE (nua.node_id IS NOT NULL)
                           END AS avoid_mention
                    FROM cold_nodes cn
                    LEFT JOIN node_user_avoid nua
                      ON nua.archive_id = cn.archive_id
                     AND nua.node_id = cn.id
                     AND nua.user_id = $3
                    WHERE cn.archive_id = $1 AND cn.id = ANY($2::text[])
                    """,
                    archive_id, all_aux_ids, viewer_user_id or "",
                )
                for ar in aux_rows:
                    aux_meta[ar["id"]] = {
                        "id": ar["id"],
                        "type": ar["node_type"],
                        "scope": ar["scope"],
                        "headline": ar["headline"],
                        "avoid_mention": bool(ar["avoid_mention"]),
                    }

            # 主节点访问反馈
            await conn.execute(
                """
                UPDATE cold_nodes
                SET access_count = access_count + 1,
                    last_access = NOW(),
                    salience = LEAST(1.0, salience + $3)
                WHERE archive_id = $1 AND id = ANY($2::text[])
                """,
                archive_id, main_ids, settings.salience_access_boost,
            )

    out = []
    for r in rows:
        nid = r["id"]
        neighbors_1 = []
        for n_id, w in adj.get(nid, []):
            meta = aux_meta.get(n_id)
            if meta:
                neighbors_1.append({**meta, "weight": w})
        item: dict = {
            "id": nid,
            "scope": r["scope"],
            "type": r["node_type"],
            "headline": r["headline"],
            "content": r["content"],
            "salience": r["salience"],
            "avoid_mention": bool(r["avoid_mention"]),
            "avoid_reason": r["avoid_reason"] or "",
            "file_metadata": r["file_metadata"] or "",
            "neighbors": neighbors_1,
        }
        if depth >= 2:
            second_for_node: list[dict] = []
            for n_id, _ in adj.get(nid, []):
                for s_id in second_adj.get(n_id, []):
                    meta = aux_meta.get(s_id)
                    if meta:
                        second_for_node.append({"via": n_id, **meta})
            item["neighbors_2hop"] = second_for_node
        out.append(item)
    return out


# ── 压缩：温 → 冷 ───────────────────────────────────────────


# #7 修:warm→cold 压缩失败兜底
# 分级回退:第 2 次失败拆半重试;第 3 次失败直接 dump 为 cold 节点(低 salience 0.15)
# 信息不丢,只是没 LLM 结构化,仍可通过 expand_cold 检索。
_warm2cold_failures: dict[tuple, int] = {}
_WARM2COLD_MAX_FAILURES = 3


async def _compress_warm_to_cold(
    *,
    archive_id: str,
    group_id: str,
    scope: str,                     # "user" | "group"
    user_id: Optional[str],         # scope=user 时填，scope=group 时 None
    overflow: list[dict],
    existing_index: list[dict],
) -> int:
    """通用压缩：温记忆 → 冷节点 + 边。返回新建节点数。"""
    if not overflow:
        return 0

    overflow_text = _format_warm_for_compress(overflow)
    existing_text = _format_existing_index(existing_index)

    user_text = (
        f"## Warm Memories To Consolidate ({len(overflow)} oldest entries)\n{overflow_text}\n\n"
        f"## Existing Cold Nodes (headline only)\n{existing_text}\n\n"
        "以上是待沉淀温记忆和已有冷节点参考。"
    )

    msgs = [
        {"role": "system", "content": _COMPRESS_SYSTEM},
        {"role": "user", "content": user_text},
    ]

    input_warm_ids = {w["id"] for w in overflow}

    def _validate(raw):
        if not isinstance(raw, dict):
            return False
        new_nodes = raw.get("nodes") or []
        refs_existing = raw.get("references_existing") or []
        consumed = set(str(x) for x in (raw.get("consumed_warm_ids") or []))
        # 完整性：consumed_warm_ids 必须严格等于输入
        if consumed != input_warm_ids:
            return False
        # 至少要有产出
        if not new_nodes and not refs_existing:
            return False
        # source_warm_ids 必须 ⊂ input
        for n in new_nodes:
            if not all(s in input_warm_ids for s in (n.get("source_warm_ids") or [])):
                return False
        for ref in refs_existing:
            if not all(s in input_warm_ids for s in (ref.get("source_warm_ids") or [])):
                return False
        return True

    raw = await llm.chat_json_with_upgrade(msgs, validate=_validate, label="cold_warm2cold")
    if raw is None:
        # 分级回退策略(避免 warm 表无限增长且不丢信息):
        #   第 2 次失败 → 拆半重试(降低单次复杂度)
        #   第 3 次失败 → 直接 dump 为 cold 节点(不用 LLM,保留原文)
        sig = (archive_id, group_id, scope, user_id or "", frozenset(str(x) for x in input_warm_ids))
        _warm2cold_failures[sig] = _warm2cold_failures.get(sig, 0) + 1
        fail_count = _warm2cold_failures[sig]

        if fail_count == _WARM2COLD_MAX_FAILURES - 1 and len(overflow) > 1:
            # 第 2 次失败:拆分重试 — 大 batch 可能太杂,拆半降低 LLM 负担
            _warm2cold_failures.pop(sig, None)
            mid = len(overflow) // 2
            log.info(
                "compress warm->cold: failed twice, splitting %d entries into %d+%d",
                len(overflow), mid, len(overflow) - mid,
            )
            c1 = await _compress_warm_to_cold(
                archive_id=archive_id, group_id=group_id, scope=scope,
                user_id=user_id, overflow=overflow[:mid], existing_index=existing_index,
            )
            c2 = await _compress_warm_to_cold(
                archive_id=archive_id, group_id=group_id, scope=scope,
                user_id=user_id, overflow=overflow[mid:], existing_index=existing_index,
            )
            return c1 + c2

        if fail_count >= _WARM2COLD_MAX_FAILURES:
            # 第 3 次失败:直接 dump — 信息不丢,只是没 LLM 结构化(低 salience)
            log.warning(
                "compress warm->cold: failed %d times for archive=%s group=%s scope=%s; "
                "fallback raw dump %d warm entries to cold (low salience)",
                fail_count, archive_id, group_id, scope, len(overflow),
            )
            count = 0
            try:
                async with pool().acquire() as conn:
                    async with conn.transaction():
                        for w in overflow:
                            cid = f"c_{ulid.ULID()}"
                            text = (w.get("text", "") or w.get("content", "") or "")
                            headline = text[:200] if text else "(no content)"
                            src_id = str(w.get("id", ""))
                            await conn.execute(
                                """
                                INSERT INTO cold_nodes
                                    (id, archive_id, group_id, user_id, scope,
                                     node_type, headline, content,
                                     salience, source_refs)
                                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
                                """,
                                cid, archive_id, group_id, user_id, scope,
                                "fact", sanitize_headline(headline),
                                sanitize_summary(text),
                                0.15,
                                json.dumps([src_id] if src_id else []),
                            )
                            count += 1
                # 2026-05-15 deadlock fix: warm_mem.delete_warm_by_ids 走的是另一条
                # pool 连接,必须在事务释放写锁之后再调用。挪到 `with` 块外。
                # 旧版"看似原子"是假象:两条不同连接的两个事务,SQLite 没分布式事务,
                # 中间崩了一样会半完成。挪到外面只改变 lock 释放顺序,不改变原子性。
                # 标记原 warm 条目已处理
                await warm_mem.delete_warm_by_ids(
                    archive_id, list(input_warm_ids),
                )
            except Exception:
                log.exception("fallback dump warm->cold failed, entries preserved")
                return 0
            _warm2cold_failures.pop(sig, None)
            return count
        log.warning(
            "compress warm->cold: lite+main both failed (attempt %d/%d); warm entries kept",
            fail_count, _WARM2COLD_MAX_FAILURES,
        )
        return 0

    # 成功:清掉计数
    sig = (archive_id, group_id, scope, user_id or "", frozenset(str(x) for x in input_warm_ids))
    _warm2cold_failures.pop(sig, None)

    new_nodes = raw.get("nodes") or []
    refs_existing = raw.get("references_existing") or []
    edges = raw.get("edges") or []

    # tmp_id → real id 映射
    tmp_to_real: dict[str, str] = {}
    valid_node_types = {"fact", "preference", "event", "relationship", "topic"}

    async with pool().acquire() as conn:
        async with conn.transaction():
            # 1. 创建新节点
            count = 0
            for n in new_nodes:
                tmp = str(n.get("tmp_id", "")).strip()
                if not tmp or tmp in tmp_to_real:
                    continue
                ntype = str(n.get("type", "fact"))
                if ntype not in valid_node_types:
                    ntype = "fact"
                cid = f"c_{ulid.ULID()}"
                tmp_to_real[tmp] = cid
                sal = float(n.get("salience_init", 0.5))
                sal = max(0.1, min(1.0, sal))
                await conn.execute(
                    """
                    INSERT INTO cold_nodes
                        (id, archive_id, group_id, user_id, scope,
                         node_type, headline, content,
                         salience, source_refs)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
                    """,
                    cid, archive_id, group_id, user_id, scope,
                    ntype,
                    sanitize_headline(str(n.get("headline", ""))),
                    sanitize_summary(str(n.get("content", ""))),
                    sal,
                    json.dumps(list(n.get("source_warm_ids") or [])),
                )
                count += 1

            # 2. 强化既有节点（salience += boost；source_refs 追加；access_count + 1）
            for ref in refs_existing:
                node_ref = str(ref.get("node_ref", "")).strip()
                if not node_ref.startswith("c_"):
                    continue
                # 校验该节点存在且属于本 archive 同一 scope/隔离
                exists = await conn.fetchval(
                    """
                    SELECT 1 FROM cold_nodes
                    WHERE archive_id = $1 AND id = $2 AND scope = $3
                      AND group_id = $4
                      AND ($5::text IS NULL OR user_id = $5)
                    """,
                    archive_id, node_ref, scope, group_id, user_id,
                )
                if not exists:
                    continue
                new_srcs = list(ref.get("source_warm_ids") or [])
                # Merge new source_warm_ids into source_refs (SQLite: manual merge)
                cur = await conn.fetchval(
                    "SELECT source_refs FROM cold_nodes WHERE archive_id = $1 AND id = $2",
                    archive_id, node_ref,
                )
                existing = json.loads(cur) if cur and isinstance(cur, str) else (cur if isinstance(cur, list) else [])
                merged = existing + [x for x in new_srcs if x not in existing]
                await conn.execute(
                    """
                    UPDATE cold_nodes
                    SET salience = LEAST(1.0, salience + $3),
                        access_count = access_count + 1,
                        last_access = NOW(),
                        source_refs = $4,
                        updated_at = NOW()
                    WHERE archive_id = $1 AND id = $2
                    """,
                    archive_id, node_ref, settings.salience_access_boost,
                    json.dumps(merged),
                )

            # 3. 边
            for e in edges:
                src = _resolve_edge_id(e.get("src"), tmp_to_real)
                dst = _resolve_edge_id(e.get("dst"), tmp_to_real)
                if not src or not dst or src == dst:
                    continue
                w = float(e.get("weight", 1.0) or 1.0)
                w = max(0.0, min(1.0, w))
                # 两端必须都已存在
                ok = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM cold_nodes
                    WHERE archive_id = $1 AND id IN ($2, $3)
                    """,
                    archive_id, src, dst,
                )
                if ok != 2:
                    continue
                await conn.execute(
                    """
                    INSERT INTO cold_edges (archive_id, src_id, dst_id, weight)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (archive_id, src_id, dst_id)
                    DO UPDATE SET weight = COALESCE(GREATEST(cold_edges.weight, EXCLUDED.weight), cold_edges.weight, EXCLUDED.weight)
                    """,
                    archive_id, src, dst, w,
                )

            # 4. 删除已消化的温记忆
            await conn.execute(
                """
                DELETE FROM warm_memories
                WHERE archive_id = $1 AND id = ANY($2::text[])
                """,
                archive_id, list(input_warm_ids),
            )
    return count


def _resolve_edge_id(x, tmp_to_real: dict[str, str]) -> Optional[str]:
    if not isinstance(x, str):
        return None
    x = x.strip()
    if x in tmp_to_real:
        return tmp_to_real[x]
    if x.startswith("c_"):
        return x
    return None


def _format_warm_for_compress(overflow: list[dict]) -> str:
    lines = []
    for w in overflow:
        ts = w.get("timestamp", "")
        ents = ",".join(w.get("entities") or [])
        line = (
            f"[{w['id']}] ({ts}) "
            f"{w.get('headline', '')}\n"
            f"  summary: {w.get('summary', '')}\n"
            f"  hint: {w.get('internal_hint', '')}\n"
            f"  entities: {ents}"
        )
        lines.append(line)
    return "\n\n".join(lines)


def _format_existing_index(index: list[dict]) -> str:
    if not index:
        return "（无）"
    lines = []
    for n in index[:200]:  # 太多就截断；仅供参考
        lines.append(f"- [{n['id']}] ({n.get('type','')}) {n['headline']}")
    return "\n".join(lines)


# ── 用户温→用户冷 / 群组温→群组冷 入口 ──────────────────────
async def compress_user_warm_to_cold(
    archive_id: str, group_id: str, user_id: str,
) -> int:
    overflow = await warm_mem.get_user_warm_overflow(archive_id, group_id, user_id)
    if not overflow:
        return 0
    existing = await load_cold_user_index(archive_id, group_id, user_id, limit=200)
    return await _compress_warm_to_cold(
        archive_id=archive_id, group_id=group_id,
        scope="user", user_id=user_id,
        overflow=overflow, existing_index=existing,
    )


async def compress_group_warm_to_cold(
    archive_id: str, group_id: str,
) -> int:
    overflow = await warm_mem.get_group_warm_overflow(archive_id, group_id)
    if not overflow:
        return 0
    existing = await load_cold_group_index(archive_id, group_id, limit=200)
    return await _compress_warm_to_cold(
        archive_id=archive_id, group_id=group_id,
        scope="group", user_id=None,
        overflow=overflow, existing_index=existing,
    )


# ── 兼容 M1/M2 stub 的接口名 ───────────────────────────────
async def topk_cold_user(
    archive_id: str, group_id: str, user_id: str,
    query_embedding=None, k: int = 100,
    *, query_keywords: Optional[list[str]] = None,
) -> list[dict]:
    """Top-N 冷记忆索引。

    历史:这个函数老接受 query_embedding 但完全忽略,只走 salience+recency 排序。
    后果:Round 2 的 system prompt 收到的是"全局最热门 100 条记忆",和当前 query
    无关,模型必须用 expand_cold 工具自己再查一次,多一个 round trip。

    #5 修:加 query_keywords 参数。给定时:
    1. 先按 keyword(headline/content 任一 ILIKE)粗筛(取多 3x 候选)
    2. 在粗筛结果内按 effective salience 排序取 k
    3. 不足 k 时,补充全局热门项(避免空 index)
    query_keywords=None 时退化为原行为(纯 salience 排序)。
    """
    n = k or settings.cold_user_index_topn
    if not query_keywords:
        return await load_cold_user_index(archive_id, group_id, user_id, limit=n)

    # 关键词粗筛 + salience 排序
    where_extra = []
    sql_args: list = [archive_id, group_id, user_id]
    for kw in query_keywords[:5]:  # 最多 5 个关键词,避免 SQL 过长
        kw = kw.strip()
        if len(kw) < 2:
            continue
        idx = len(sql_args) + 1
        where_extra.append(f"(headline ILIKE ${idx} OR content ILIKE ${idx})")
        sql_args.append(f"%{kw}%")

    if not where_extra:
        return await load_cold_user_index(archive_id, group_id, user_id, limit=n)

    sql_args.append(n)
    limit_idx = len(sql_args)
    sql = f"""
        SELECT id, node_type, headline, salience,
               avoid_mention, avoid_reason,
               {_eff_sql()} AS eff_salience
        FROM cold_nodes
        WHERE archive_id = $1 AND group_id = $2 AND user_id = $3
          AND scope = 'user'
          AND ({' OR '.join(where_extra)})
        ORDER BY eff_salience DESC, last_access DESC NULLS LAST
        LIMIT ${limit_idx}
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(sql, *sql_args)

    matched = [_index_row(r) for r in rows]

    # 不足 k 时补全局热门(去重)
    if len(matched) < n:
        rest = await load_cold_user_index(
            archive_id, group_id, user_id, limit=n,
        )
        seen = {m["id"] for m in matched}
        for r in rest:
            if r["id"] not in seen:
                matched.append(r)
                if len(matched) >= n:
                    break

    return matched


async def topk_cold_group(
    archive_id: str, group_id: str,
    query_embedding=None, k: int = 100,
    *, viewer_user_id: Optional[str] = None,
    query_keywords: Optional[list[str]] = None,
) -> list[dict]:
    """同 topk_cold_user,加 query_keywords 关键词粗筛(详见上)。"""
    n = k or settings.cold_group_index_topn
    if not query_keywords:
        return await load_cold_group_index(
            archive_id, group_id,
            viewer_user_id=viewer_user_id, limit=n,
        )

    where_extra = []
    sql_args: list = [archive_id, group_id, viewer_user_id or ""]
    for kw in query_keywords[:5]:
        kw = kw.strip()
        if len(kw) < 2:
            continue
        idx = len(sql_args) + 1
        where_extra.append(f"(cn.headline ILIKE ${idx} OR cn.content ILIKE ${idx})")
        sql_args.append(f"%{kw}%")

    if not where_extra:
        return await load_cold_group_index(
            archive_id, group_id,
            viewer_user_id=viewer_user_id, limit=n,
        )

    sql_args.append(n)
    limit_idx = len(sql_args)
    sql = f"""
        SELECT cn.id, cn.node_type, cn.headline, cn.salience,
               (nua.node_id IS NOT NULL) AS avoid_mention,
               nua.reason AS avoid_reason,
               {_eff_sql_aliased('cn')} AS eff_salience
        FROM cold_nodes cn
        LEFT JOIN node_user_avoid nua
          ON nua.archive_id = cn.archive_id
         AND nua.node_id = cn.id
         AND nua.user_id = $3
        WHERE cn.archive_id = $1 AND cn.group_id = $2 AND cn.scope = 'group'
          AND ({' OR '.join(where_extra)})
        ORDER BY eff_salience DESC, cn.last_access DESC NULLS LAST
        LIMIT ${limit_idx}
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(sql, *sql_args)

    matched = [_index_row(r) for r in rows]
    if len(matched) < n:
        rest = await load_cold_group_index(
            archive_id, group_id,
            viewer_user_id=viewer_user_id, limit=n,
        )
        seen = {m["id"] for m in matched}
        for r in rest:
            if r["id"] not in seen:
                matched.append(r)
                if len(matched) >= n:
                    break
    return matched


# ── 删除（运营） ──
async def delete_cold(archive_id: str, group_id: str, ids: list[str]) -> int:
    if not ids:
        return 0
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id FROM cold_nodes
            WHERE archive_id = $1 AND group_id = $2 AND id = ANY($3::text[])
            """,
            archive_id, group_id, ids,
        )
        if not rows:
            return 0
        delete_ids = [r["id"] for r in rows]
        await conn.execute(
            """
            DELETE FROM cold_nodes
            WHERE archive_id = $1 AND group_id = $2 AND id = ANY($3::text[])
            """,
            archive_id, group_id, delete_ids,
        )
        return len(delete_ids)


# ── 软遗忘：标记节点为不主动提及（不删除） ────────────────────


async def apply_avoid_mention(
    archive_id: str,
    group_id: str,
    user_id: str,
    topics: list[str],
    reason: str = "",
) -> dict:
    """
    LLM 在所有相关冷节点中匹配 topics，标记 avoid_mention：
      - 用户冷节点（scope=user）：直接更新 cold_nodes.avoid_mention
      - 群组冷/KB（共享）节点：写入 node_user_avoid（按 user_id 遮罩）

    返回 {"user_marked": int, "shared_masked": int}。
    标记永远不删除节点，仅影响"机器人不主动提及"。
    """
    topics = [str(t)[:200] for t in (topics or []) if str(t).strip()][:10]
    reason = (reason or "")[:300]
    if not topics:
        return {"user_marked": 0, "shared_masked": 0}

    # 加载所有候选节点（用户冷 + 群组冷 + KB），各取 Top-N
    async with pool().acquire() as conn:
        user_rows = await conn.fetch(
            """
            SELECT id, scope, node_type, headline FROM cold_nodes
            WHERE archive_id = $1 AND group_id = $2 AND user_id = $3
              AND scope = 'user' AND avoid_mention = FALSE
            ORDER BY salience DESC, created_at DESC
            LIMIT 300
            """,
            archive_id, group_id, user_id,
        )
        shared_rows = await conn.fetch(
            """
            SELECT cn.id, cn.scope, cn.node_type, cn.headline
            FROM cold_nodes cn
            LEFT JOIN node_user_avoid nua
              ON nua.archive_id = cn.archive_id
             AND nua.node_id = cn.id
             AND nua.user_id = $3
            WHERE cn.archive_id = $1 AND cn.group_id = $2
              AND cn.scope IN ('group', 'kb')
              AND nua.node_id IS NULL
            ORDER BY cn.salience DESC, cn.created_at DESC
            LIMIT 500
            """,
            archive_id, group_id, user_id,
        )

    candidates = list(user_rows) + list(shared_rows)
    if not candidates:
        return {"user_marked": 0, "shared_masked": 0}

    # 构造 LLM 输入
    topic_text = "\n".join(f"- [{i}] {t}" for i, t in enumerate(topics))
    cand_lines = []
    for r in candidates:
        cand_lines.append(
            f"[{r['id']}] (scope={r['scope']}, type={r['node_type']}) "
            f"{r['headline']}"
        )
    user_text = (
        f"## User Topics To Avoid Proactively Mentioning\n{topic_text}\n\n"
        f"## Candidate Nodes\n" + "\n".join(cand_lines)
        + "\n\n以上是少主动提及的话题和候选记忆节点。"
    )

    msgs = [
        {"role": "system", "content": _AVOID_MATCH_SYSTEM},
        {"role": "user", "content": user_text},
    ]
    try:
        raw = await llm.chat_json(
            msgs,
            reasoning="disabled",
            lite=True,
            metrics_tag="json.memory_avoid_match",
        )
    except Exception:
        log.exception("apply_avoid_mention LLM failed")
        return {"user_marked": 0, "shared_masked": 0}

    matches = raw.get("matches") or []
    if not isinstance(matches, list):
        return {"user_marked": 0, "shared_masked": 0}

    # 拆分 user / shared
    cand_by_id = {r["id"]: r for r in candidates}
    user_ids: list[str] = []
    shared_ids: list[str] = []
    for m in matches:
        nid = str((m or {}).get("id", "")).strip()
        if nid not in cand_by_id:
            continue
        r = cand_by_id[nid]
        if r["scope"] == "user":
            user_ids.append(nid)
        elif r["scope"] in ("group", "kb"):
            shared_ids.append(nid)

    async with pool().acquire() as conn:
        async with conn.transaction():
            if user_ids:
                await conn.execute(
                    """
                    UPDATE cold_nodes
                    SET avoid_mention = TRUE,
                        avoid_reason = $3,
                        updated_at = NOW()
                    WHERE archive_id = $1 AND id = ANY($2::text[])
                    """,
                    archive_id, user_ids, reason,
                )
            if shared_ids:
                # 批量插入；冲突更新 reason
                rows = [
                    (archive_id, user_id, nid, reason)
                    for nid in shared_ids
                ]
                await conn.executemany(
                    """
                    INSERT INTO node_user_avoid
                        (archive_id, user_id, node_id, reason)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (archive_id, user_id, node_id)
                    DO UPDATE SET reason = EXCLUDED.reason
                    """,
                    rows,
                )

    log.info(
        "avoid_mention applied: user=%d shared=%d topics=%s",
        len(user_ids), len(shared_ids), topics,
    )
    return {"user_marked": len(user_ids), "shared_masked": len(shared_ids)}


async def list_avoided_for_user(
    archive_id: str, group_id: str, user_id: str,
) -> list[dict]:
    """运营接口：列出对该用户视角下被遮罩的所有节点。"""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT cn.id, cn.scope, cn.node_type, cn.headline, cn.created_at,
                   CASE
                     WHEN cn.scope = 'user' THEN cn.avoid_reason
                     ELSE nua.reason
                   END AS reason
            FROM cold_nodes cn
            LEFT JOIN node_user_avoid nua
              ON nua.archive_id = cn.archive_id
             AND nua.node_id = cn.id
             AND nua.user_id = $3
            WHERE cn.archive_id = $1 AND cn.group_id = $2
              AND (
                (cn.scope = 'user' AND cn.user_id = $3 AND cn.avoid_mention = TRUE)
                OR (cn.scope IN ('group', 'kb') AND nua.node_id IS NOT NULL)
              )
            ORDER BY cn.created_at DESC
            """,
            archive_id, group_id, user_id,
        )
    return [dict(r) for r in rows]


async def unmark_avoid_for_user(
    archive_id: str, group_id: str, user_id: str, node_id: str,
) -> bool:
    """
    取消某用户视角下对单个节点的 avoid 标记（运营用）。
    用户冷节点：清 avoid_mention；
    群组冷/KB：删除 node_user_avoid 行。
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            # 先看节点 scope
            row = await conn.fetchrow(
                """
                SELECT scope FROM cold_nodes
                WHERE archive_id = $1 AND id = $2 AND group_id = $3
                """,
                archive_id, node_id, group_id,
            )
            if not row:
                return False
            if row["scope"] == "user":
                result = await conn.execute(
                    """
                    UPDATE cold_nodes
                    SET avoid_mention = FALSE,
                        avoid_reason = NULL,
                        updated_at = NOW()
                    WHERE archive_id = $1 AND id = $2 AND user_id = $3
                    """,
                    archive_id, node_id, user_id,
                )
                return result.endswith(" 1")
            else:
                result = await conn.execute(
                    """
                    DELETE FROM node_user_avoid
                    WHERE archive_id = $1 AND node_id = $2 AND user_id = $3
                    """,
                    archive_id, node_id, user_id,
                )
                return result.endswith(" 1")
