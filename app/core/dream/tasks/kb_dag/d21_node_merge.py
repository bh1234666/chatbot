"""
D21: 节点合并 (减 KB 冗余)

问题: KB compress 只新增节点不合并, 多次相似讨论 → 多个相似 fact 节点
目标: 检测语义重复簇, LLM 合并到一个新节点, 旧节点软删除

工作流:
1. 找候选重复簇 (source_refs 重叠率高 + headline 相似)
2. 对每簇调 LLM: 合并到一个新节点 (内容不丢失)
3. INSERT 新节点 + 重连相关边 + 旧节点软删除 (file_metadata.merged_to=新id)

风险/保护:
- salience > 0.7 不合并 (重要节点保留)
- LLM 严格 validate: 合并后内容必须包含所有输入信息要点 (字符数下限)
- 软删除 (file_metadata.merged_to), 不真删 → 可审计
- 共享 KB lock 与 reactive 串行
- A 类不打断

阈值: 30 (累计 30 新 kb 节点才检测重复, 太少没必要)
"""
from __future__ import annotations

from app.core.dream.prompt_catalog import (
    D21_NODE_MERGE_SYSTEM,
)
_LLM_PROMPT_SYSTEM = D21_NODE_MERGE_SYSTEM


import asyncio
import json
import time
import ulid
from typing import Any

from app.core.dream.dream_log import dream_log
from app.core.dream.event_bus import event_bus
from app.core.dream.registry import register_dream_task
from app.core.dream.task_base import InfoDrivenTask


D21_THRESHOLD = 30
D21_MAX_CLUSTERS_PER_RUN = 3  # 单次最多合并 3 簇
D21_MIN_CLUSTER_SIZE = 2
D21_MAX_CLUSTER_SIZE = 6
D21_PROTECT_SALIENCE = 0.7  # 高 salience 不合并




def _validate_d21_output(raw: Any, input_ids: set[str]) -> bool:
    if not isinstance(raw, dict):
        return False
    if not raw.get("merge"):
        return isinstance(raw.get("reason", ""), str)
    
    headline = raw.get("headline", "")
    content = raw.get("content", "")
    merged_ids = raw.get("merged_node_ids", [])
    
    if not (5 <= len(headline) <= 60):
        return False
    if not (30 <= len(content) <= 800):
        return False
    if not isinstance(merged_ids, list):
        return False
    if set(merged_ids) != input_ids:
        return False
    return True


async def _find_redundancy_clusters(archive_id: str, group_id: str) -> list[list[dict]]:
    """找重复候选簇.
    
    简化: 用 source_refs 重叠 + headline 前缀相似找候选.
    实际应用 vector embedding 但太重, 这里用启发.
    """
    from app.db.pool import pool
    
    async with pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, node_type, headline, content, salience, source_refs, file_metadata
            FROM cold_nodes
            WHERE archive_id = $1 AND group_id = $2
              AND scope = 'kb'
              AND node_type IN ('fact', 'preference')
              AND salience < $3   -- 保护高 salience
              AND (file_metadata->>'merged_to' IS NULL)  -- 排除已被合并
            ORDER BY created_at DESC
            LIMIT 300
        """, archive_id, group_id, D21_PROTECT_SALIENCE)
    
    if len(rows) < D21_MIN_CLUSTER_SIZE:
        return []
    
    # 把 nodes 按 headline 前 15 字分组 (粗略相似性)
    from collections import defaultdict
    by_prefix: dict[str, list] = defaultdict(list)
    
    for r in rows:
        headline = (r["headline"] or "").strip()
        if len(headline) < 6:
            continue
        # 取前 15 字作 prefix key
        prefix = headline[:15]
        by_prefix[prefix].append(dict(r))
    
    # 仅保留 size 2-6 的组 (太多可能 false positive)
    clusters = [
        nodes for nodes in by_prefix.values()
        if D21_MIN_CLUSTER_SIZE <= len(nodes) <= D21_MAX_CLUSTER_SIZE
    ]
    
    return clusters[:D21_MAX_CLUSTERS_PER_RUN]


async def _llm_merge_nodes(cluster: list[dict]) -> dict | None:
    from app.llm import client as llm
    
    cluster_text = "\n\n".join(
        f"[id={n['id']} salience={float(n.get('salience', 0)):.2f}]\n"
        f"  headline: {n['headline']}\n"
        f"  content: {(n['content'] or '')[:400]}"
        for n in cluster
    )
    
    input_ids = {n["id"] for n in cluster}
    
    user_text = (
        f"## Candidate Duplicate Nodes ({len(cluster)} nodes)\n\n"
        f"{cluster_text}\n\n"
        "## Task\nDecide whether these nodes are true duplicates that describe the same thing. Merge if yes; reject if not.\n\n判断是否真重复。"
    )
    
    messages = [
        {"role": "system", "content": _LLM_PROMPT_SYSTEM},
        {"role": "user", "content": user_text},
    ]
    
    def _validate(raw):
        return _validate_d21_output(raw, input_ids)
    
    raw = await llm.chat_json_with_upgrade(
        messages,
        validate=_validate,
        label="dream_d21_merge",
        lite_first=False,  # 后台用 main+max
    )
    return raw


async def _do_merge(archive_id: str, group_id: str,
                    cluster: list[dict], decision: dict) -> str | None:
    """执行合并: INSERT 新节点 + 软删除旧 + 重连边."""
    from app.db.pool import pool
    from app.memory.kb import sanitize_headline, sanitize_summary
    
    new_id = f"c_merged_{ulid.ULID()}"
    
    # 合并 source_refs
    combined_refs: list = []
    for n in cluster:
        refs = n.get("source_refs")
        if isinstance(refs, str):
            try:
                refs = json.loads(refs)
            except Exception:
                refs = []
        if isinstance(refs, list):
            combined_refs.extend(refs)
    combined_refs = list({str(r) for r in combined_refs})[:200]
    
    # 合并后 salience = max of inputs + 小 bonus (聚合多源)
    max_sal = max(float(n.get("salience", 0.3)) for n in cluster)
    new_salience = min(1.0, max_sal + 0.05)
    
    # node_type: 优先 fact > preference (cluster 中)
    types = [n["node_type"] for n in cluster]
    new_type = "fact" if "fact" in types else types[0]
    
    file_meta = json.dumps({
        "dream_generated": True,
        "generator": "d21_merge",
        "merged_from": [n["id"] for n in cluster],
        "merged_at": time.time(),
    })
    
    try:
        async with pool().acquire() as conn:
            async with conn.transaction():
                # 1. INSERT 新节点
                await conn.execute("""
                    INSERT INTO cold_nodes
                        (id, archive_id, group_id, user_id, scope,
                         node_type, headline, content,
                         salience, source_refs, file_metadata)
                    VALUES ($1, $2, $3, NULL, 'kb', $4, $5, $6, $7, $8::jsonb, $9)
                """,
                    new_id, archive_id, group_id, new_type,
                    sanitize_headline(decision["headline"]),
                    sanitize_summary(decision["content"]),
                    new_salience,
                    json.dumps(combined_refs),
                    file_meta,
                )
                
                # 2. 软删除旧节点 - 标记 merged_to (但不真删 → 留 audit)
                for old in cluster:
                    old_meta = old.get("file_metadata") or {}
                    if isinstance(old_meta, str):
                        try:
                            old_meta = json.loads(old_meta)
                        except Exception:
                            old_meta = {}
                    old_meta = dict(old_meta) if isinstance(old_meta, dict) else {}
                    old_meta["merged_to"] = new_id
                    old_meta["merged_at"] = time.time()
                    
                    await conn.execute("""
                        UPDATE cold_nodes
                        SET file_metadata = $1, salience = 0.05, updated_at = NOW()
                        WHERE id = $2
                    """, json.dumps(old_meta, ensure_ascii=False), old["id"])
                
                # 3. 重连边 (指向旧节点的边 → 转向新节点)
                old_ids = [n["id"] for n in cluster]
                # 入边
                await conn.execute("""
                    UPDATE cold_edges SET dst_id = $1
                    WHERE archive_id = $2 AND dst_id = ANY($3)
                      AND src_id != $1
                """, new_id, archive_id, old_ids)
                # 出边
                await conn.execute("""
                    UPDATE cold_edges SET src_id = $1
                    WHERE archive_id = $2 AND src_id = ANY($3)
                      AND dst_id != $1
                """, new_id, archive_id, old_ids)
                # 删除内部循环边 (新节点 → 自己)
                await conn.execute("""
                    DELETE FROM cold_edges
                    WHERE archive_id = $1 AND src_id = dst_id
                """, archive_id)
        
        return new_id
    except Exception as e:
        dream_log.error("dream.task.d21_merge.tx_failed", f"err={e!r}"[:200])
        return None


@register_dream_task
class D21NodeMerge(InfoDrivenTask):
    """D21: 节点合并 (减冗余)."""
    
    name = "d21_merge"
    threshold = D21_THRESHOLD
    uses_llm = True
    
    async def info_fn(self) -> float:
        return float(event_bus.total_count("kb_nodes_added"))
    
    async def _do_work(self) -> None:
        from app.db.pool import pool
        
        async with pool().acquire() as conn:
            active_rows = await conn.fetch("""
                SELECT DISTINCT archive_id, group_id
                FROM cold_nodes
                WHERE scope = 'kb' AND node_type IN ('fact', 'preference')
                  AND created_at > NOW() - INTERVAL '24 hours'
                LIMIT 10
            """)
        
        merged_total = 0
        
        for row in active_rows:
            archive_id = row["archive_id"]
            group_id = row["group_id"]
            
            try:
                from app.memory.kb import _get_kb_compress_lock
                lock = await _get_kb_compress_lock(archive_id, group_id)
            except Exception:
                lock = None
            
            async def _do_one():
                nonlocal merged_total
                clusters = await _find_redundancy_clusters(archive_id, group_id)
                if not clusters:
                    return
                
                for cluster in clusters:
                    try:
                        decision = await _llm_merge_nodes(cluster)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        dream_log.error(
                            "dream.task.d21_merge.llm_failed",
                            f"err={e!r}"[:200],
                        )
                        continue
                    
                    if decision is None or not decision.get("merge"):
                        if decision and not decision.get("merge"):
                            dream_log.log(
                                "dream.task.d21_merge.rejected",
                                f"reason={decision.get('reason', '')[:80]}",
                            )
                        continue
                    
                    new_id = await _do_merge(archive_id, group_id, cluster, decision)
                    if new_id:
                        merged_total += 1
                        dream_log.log(
                            "dream.task.d21_merge.done",
                            f"new_id={new_id} from {len(cluster)} nodes",
                        )
            
            if lock:
                if lock.locked():
                    continue
                async with lock:
                    await _do_one()
            else:
                await _do_one()
        
        if merged_total:
            dream_log.log(
                "dream.task.d21_merge.cycle_done",
                f"merged {merged_total} clusters",
            )
