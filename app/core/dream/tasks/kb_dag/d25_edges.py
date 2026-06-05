"""
D25: Edge 重组 (KB DAG 整理收尾)

问题: 现有 edge 创建后不动 (KB compress 时一次性写), 不修错 / 不加缺失
目标: dream 重新评估子图的 edge 合理性, LLM 决定增删

工作流:
1. 找一个 "topic 节点 + 周围 fact" 子图 (~10-20 节点)
2. LLM 看子图 → 评估每条边 + 提议缺失边
3. UPDATE 边权重 / INSERT 新边 (不 DELETE 旧边, 保守)

阈值: 30 个新节点 (新节点会引入新关系)
A 类不打断
"""
from __future__ import annotations

from app.core.dream.prompt_catalog import (
    D25_EDGES_PROMPT,
)
_LLM_PROMPT = D25_EDGES_PROMPT


import asyncio
import json
from typing import Any

from app.core.dream.dream_log import dream_log
from app.core.dream.event_bus import event_bus
from app.core.dream.registry import register_dream_task
from app.core.dream.task_base import InfoDrivenTask


D25_THRESHOLD = 30
D25_MAX_SUBGRAPHS_PER_RUN = 2
D25_MIN_SUBGRAPH_SIZE = 5
D25_MAX_SUBGRAPH_SIZE = 15




def _normalize_d25_output(raw: Any, valid_ids: set[str]) -> dict | None:
    if not isinstance(raw, dict):
        return None
    new_edges = raw.get("new_edges", [])
    boost_edges = raw.get("boost_edges", [])
    if not isinstance(new_edges, list) or not isinstance(boost_edges, list):
        return None

    normalized_new: list[dict] = []
    normalized_boost: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def normalize_one(e: Any, *, kind: str) -> dict | None:
        if not isinstance(e, dict):
            return None
        s = str(e.get("src_id", "")).strip()
        d = str(e.get("dst_id", "")).strip()
        if s == d:
            return None
        if s not in valid_ids or d not in valid_ids:
            return None
        w = e.get("weight") if kind == "new" else e.get("new_weight", e.get("weight"))
        try:
            weight = float(w)
        except (TypeError, ValueError):
            return None
        if not (0.05 <= weight <= 1.0):
            return None
        key = (kind, s, d)
        if key in seen:
            return None
        seen.add(key)
        out = {
            "src_id": s,
            "dst_id": d,
            "reason": str(e.get("reason", ""))[:240],
        }
        if kind == "new":
            out["weight"] = max(0.05, min(1.0, weight))
        else:
            out["new_weight"] = max(0.05, min(1.0, weight))
        return out

    for e in new_edges:
        item = normalize_one(e, kind="new")
        if item is not None:
            normalized_new.append(item)
    for e in boost_edges:
        item = normalize_one(e, kind="boost")
        if item is not None:
            normalized_boost.append(item)

    # Treat all-invalid suggestions as a valid no-op; this prevents one bad edge
    # from making D25 retry the same topic forever.
    return {
        "new_edges": normalized_new[:10],
        "boost_edges": normalized_boost[: max(0, 10 - len(normalized_new))],
    }


def _validate_d25_output(raw: Any, valid_ids: set[str]) -> bool:
    return _normalize_d25_output(raw, valid_ids) is not None


async def _find_subgraph_with_topic(archive_id: str, group_id: str) -> list[dict] | None:
    """找一个 topic 节点 + 周围 fact 形成子图."""
    from app.db.pool import pool
    
    async with pool().acquire() as conn:
        # 找一个未处理过 d25 的 topic
        topic_row = await conn.fetchrow("""
            SELECT id FROM cold_nodes
            WHERE archive_id = $1 AND group_id = $2
              AND scope = 'kb' AND node_type = 'topic'
              AND (file_metadata->>'d25_processed' IS NULL
                   OR file_metadata->>'d25_processed' = 'false')
            ORDER BY salience DESC
            LIMIT 1
        """, archive_id, group_id)
        
        if not topic_row:
            return None
        
        topic_id = topic_row["id"]
        
        # 取 topic 周围 (1-2 跳)
        nodes = await conn.fetch("""
            WITH neighborhood AS (
                -- topic 节点
                SELECT $1::text AS id
                UNION
                -- 1 跳: 跟 topic 有边的
                SELECT src_id AS id FROM cold_edges
                WHERE archive_id = $2 AND dst_id = $1
                UNION
                SELECT dst_id AS id FROM cold_edges
                WHERE archive_id = $2 AND src_id = $1
            )
            SELECT cn.id, cn.node_type, cn.headline, cn.content, cn.salience
            FROM cold_nodes cn
            JOIN neighborhood n ON cn.id = n.id
            WHERE (cn.file_metadata->>'merged_to' IS NULL)
            ORDER BY 
                CASE WHEN cn.node_type = 'topic' THEN 0 ELSE 1 END,
                cn.salience DESC
            LIMIT $3
        """, topic_id, archive_id, D25_MAX_SUBGRAPH_SIZE)
    
    if len(nodes) < D25_MIN_SUBGRAPH_SIZE:
        return None
    
    return [dict(r) for r in nodes]


async def _get_existing_edges(archive_id: str, node_ids: list[str]) -> list[dict]:
    from app.db.pool import pool
    
    async with pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT src_id, dst_id, weight FROM cold_edges
            WHERE archive_id = $1
              AND src_id = ANY($2) AND dst_id = ANY($2)
        """, archive_id, node_ids)
    return [dict(r) for r in rows]


async def _llm_evaluate_edges(nodes: list[dict], edges: list[dict]) -> dict | None:
    from app.llm import client as llm
    
    nodes_text = "\n".join(
        f"[{n['id']}] type={n['node_type']} salience={float(n.get('salience', 0)):.2f}\n"
        f"  headline: {n['headline']}\n"
        f"  content: {(n['content'] or '')[:200]}"
        for n in nodes
    )
    
    edges_text = "\n".join(
        f"  {e['src_id']} → {e['dst_id']} (weight={float(e.get('weight', 0)):.2f})"
        for e in edges
    ) or "(no existing edges)"
    
    user_text = (
        f"## Subgraph ({len(nodes)} nodes)\n{nodes_text}\n\n"
        f"## Existing Edges ({len(edges)} edges)\n{edges_text}\n\n"
        f"## Task\nEvaluate edge quality and propose high-confidence new or boosted edges.\n\n评估边关系。"
    )
    
    valid_ids = {n["id"] for n in nodes}
    
    normalized: dict | None = None

    def _validate(raw):
        nonlocal normalized
        normalized = _normalize_d25_output(raw, valid_ids)
        return normalized is not None
    
    raw = await llm.chat_json_with_upgrade(
        [
            {"role": "system", "content": _LLM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        validate=_validate,
        label="dream_d25_edges",
        lite_first=False,
    )
    return normalized or raw


async def _apply_edge_changes(
    archive_id: str,
    topic_id: str,
    new_edges: list[dict],
    boost_edges: list[dict],
) -> tuple[int, int]:
    from app.db.pool import pool
    
    added = 0
    boosted = 0
    
    try:
        async with pool().acquire() as conn:
            async with conn.transaction():
                # 1. INSERT 新边 (冲突时取 max weight)
                for e in new_edges:
                    w = float(e.get("weight", 0.5))
                    result = await conn.execute("""
                        INSERT INTO cold_edges (archive_id, src_id, dst_id, weight)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (archive_id, src_id, dst_id)
                        DO UPDATE SET weight = GREATEST(cold_edges.weight, EXCLUDED.weight)
                    """, archive_id, e["src_id"], e["dst_id"], w)
                    if "INSERT 1" in str(result) or "UPDATE 1" in str(result):
                        added += 1
                
                # 2. UPDATE 边权
                for e in boost_edges:
                    w = float(e.get("new_weight") or e.get("weight", 0.5))
                    await conn.execute("""
                        UPDATE cold_edges
                        SET weight = GREATEST(weight, $1)
                        WHERE archive_id = $2 AND src_id = $3 AND dst_id = $4
                    """, w, archive_id, e["src_id"], e["dst_id"])
                    boosted += 1
                
                # 3. 标记 topic 已 d25 处理
                # 2026-05-17 Round 14m: jsonb_set → SQLite json_set; '{}'::jsonb cast 移除
                await conn.execute("""
                    UPDATE cold_nodes
                    SET file_metadata = json_set(
                        COALESCE(file_metadata, '{}'),
                        '$.d25_processed',
                        'true'
                    )
                    WHERE id = $1
                """, topic_id)
        return (added, boosted)
    except Exception as e:
        dream_log.error("dream.task.d25_edges.tx_failed", repr(e)[:200])
        return (0, 0)


@register_dream_task
class D25EdgeReorg(InfoDrivenTask):
    """D25: Edge 重组 (KB DAG 收尾)."""
    
    name = "d25_edges"
    threshold = D25_THRESHOLD
    uses_llm = True
    
    async def info_fn(self) -> float:
        return float(event_bus.total_count("kb_nodes_added"))
    
    async def _do_work(self) -> None:
        from app.db.pool import pool
        
        async with pool().acquire() as conn:
            active = await conn.fetch("""
                SELECT DISTINCT archive_id, group_id
                FROM cold_nodes
                WHERE scope = 'kb' AND node_type = 'topic'
                  AND (file_metadata->>'d25_processed' IS NULL
                       OR file_metadata->>'d25_processed' = 'false')
                LIMIT 10
            """)
        
        total_added = 0
        total_boosted = 0
        processed = 0
        
        for row in active:
            if processed >= D25_MAX_SUBGRAPHS_PER_RUN:
                break
            
            subgraph = await _find_subgraph_with_topic(
                row["archive_id"], row["group_id"]
            )
            if not subgraph:
                continue
            
            # topic 节点 id (第一个, 按 ORDER BY 排在前)
            topic_node = next(
                (n for n in subgraph if n["node_type"] == "topic"), None
            )
            if not topic_node:
                continue
            
            edges = await _get_existing_edges(
                row["archive_id"], [n["id"] for n in subgraph]
            )
            
            try:
                decision = await _llm_evaluate_edges(subgraph, edges)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                dream_log.warn("dream.task.d25_edges.llm_failed", repr(e)[:200])
                continue
            
            if decision is None:
                continue
            
            new_edges = decision.get("new_edges", [])
            boost_edges = decision.get("boost_edges", [])
            
            if not new_edges and not boost_edges:
                # 2026-05-17 Round 14m: jsonb_set → SQLite json_set; '{}'::jsonb cast 移除
                async with pool().acquire() as conn:
                    await conn.execute("""
                        UPDATE cold_nodes
                        SET file_metadata = json_set(
                            COALESCE(file_metadata, '{}'),
                            '$.d25_processed',
                            'true'
                        )
                        WHERE id = $1
                    """, topic_node["id"])
                continue
            
            added, boosted = await _apply_edge_changes(
                row["archive_id"], topic_node["id"], new_edges, boost_edges
            )
            total_added += added
            total_boosted += boosted
            processed += 1
        
        if total_added or total_boosted:
            dream_log.log(
                "dream.task.d25_edges.cycle_done",
                f"added={total_added} boosted={total_boosted} subgraphs={processed}",
            )
