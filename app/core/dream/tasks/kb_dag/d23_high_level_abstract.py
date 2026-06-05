"""
D23: 高级抽象节点生成 ⭐ (v7 核心创新)

目标: 从多个低级 fact/preference 节点 → 提炼上层 topic 节点
价值: 主线程查 "用户对什么有兴趣" → 一次命中 topic, 不用拼 5 个 fact

工作流:
1. 等 kb_nodes_added 事件累积到 threshold (10 个新 fact)
2. 找 cluster (用 cold_edges 或 source_refs 重叠找 community)
3. 对每个 cluster 调 LLM 生成 topic 节点
4. INSERT 新 topic 节点 + 边连接
5. A/B 验证: 24h 内未被查 → 删 (Phase 2 实施, 先做不删)

设计:
- Class A (不打断): 用 schedule 模式跑
- lite_first=False: 后台用 main+reasoning="max" 保质量
- 共享 KB lock (与 reactive 串行)
- 不更新已存在的 topic (避免重做)
"""
from __future__ import annotations

from app.core.dream.prompt_catalog import (
    D23_HIGH_LEVEL_ABSTRACT_SYSTEM,
)
_LLM_PROMPT_SYSTEM = D23_HIGH_LEVEL_ABSTRACT_SYSTEM


import asyncio
import json
import time
import ulid
from typing import Any

from app.config import settings
from app.core.dream.dream_log import dream_log
from app.core.dream.event_bus import event_bus
from app.core.dream.registry import register_dream_task
from app.core.dream.task_base import InfoDrivenTask


# 触发阈值: 新增 10 个 fact/preference 节点才尝试抽象
D23_THRESHOLD = 10
# 单次处理 cluster 上限 (防 LLM 调用爆炸)
D23_MAX_CLUSTERS_PER_RUN = 5
# 单个 cluster 最少节点数 (太少无法抽象)
D23_MIN_CLUSTER_SIZE = 5
# 单个 cluster 最大节点数 (LLM 输入截断)
D23_MAX_CLUSTER_SIZE = 20




def _normalize_d23_output(raw: Any, input_node_ids: set[str]) -> dict | None:
    """Validate and normalize D23 output.

    The LLM may choose a useful subset from a noisy cluster. We keep that
    topic if enough input nodes support it, and fill missing edge weights with
    a conservative default instead of rejecting the whole result.
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("skip"):
        return raw if isinstance(raw.get("reason", ""), str) else None
    
    headline = str(raw.get("headline", "")).strip()
    content = str(raw.get("content", "")).strip()
    topic_type = str(raw.get("topic_type", "")).strip()
    subset = raw.get("subset_node_ids", [])
    weights = raw.get("edge_weights", {})
    
    if not (5 <= len(headline) <= 60):  # 略宽容
        return None
    if not (50 <= len(content) <= 800):
        return None
    if topic_type not in ("interest", "pattern", "preference", "habit", "context"):
        return None
    if not isinstance(subset, list) or not subset:
        return None
    try:
        subset_ids = {str(x) for x in subset}
    except TypeError:
        return None
    subset_ids &= input_node_ids
    if len(subset_ids) < D23_MIN_CLUSTER_SIZE:
        return None
    # A topic should be backed by a meaningful portion of the cluster; this
    # allows noisy clusters without accepting one-off facts as a topic.
    if len(subset_ids) < max(D23_MIN_CLUSTER_SIZE, len(input_node_ids) // 2):
        return None

    if not isinstance(weights, dict):
        weights = {}
    norm_weights: dict[str, float] = {}
    for node_id in subset_ids:
        raw_weight = weights.get(node_id, 0.5)
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError):
            weight = 0.5
        norm_weights[node_id] = max(0.1, min(1.0, weight))

    normalized = dict(raw)
    normalized.update({
        "skip": False,
        "headline": headline,
        "content": content,
        "topic_type": topic_type,
        "subset_node_ids": sorted(subset_ids),
        "edge_weights": norm_weights,
    })
    return normalized


def _validate_d23_output(raw: Any, input_node_ids: set[str]) -> bool:
    """验证 LLM 输出。"""
    return _normalize_d23_output(raw, input_node_ids) is not None


async def _find_candidate_clusters(archive_id: str, group_id: str) -> list[list[dict]]:
    """找抽象候选 cluster.
    
    简化版: 用 cold_edges 找 community (BFS).
    返回每个 cluster 的节点列表 (每节点含 id/headline/content/type/source_refs).
    """
    from app.db.pool import pool
    
    async with pool().acquire() as conn:
        # 取所有 fact/preference 节点 (非 topic, 非 file)
        nodes_rows = await conn.fetch("""
            SELECT id, node_type, headline, content, salience, source_refs
            FROM cold_nodes
            WHERE archive_id = $1 AND group_id = $2
              AND scope = 'kb'
              AND node_type IN ('fact', 'preference', 'event')
              AND (file_metadata->>'merged_to' IS NULL)  -- 排除被合并的
            ORDER BY salience DESC, created_at DESC
            LIMIT 500
        """, archive_id, group_id)
        
        if len(nodes_rows) < D23_MIN_CLUSTER_SIZE:
            return []
        
        # 取所有相关边
        node_ids = [r["id"] for r in nodes_rows]
        edges_rows = await conn.fetch("""
            SELECT src_id, dst_id, weight FROM cold_edges
            WHERE archive_id = $1 AND src_id = ANY($2) AND dst_id = ANY($2)
        """, archive_id, node_ids)
        
        # 检查已存在 topic 节点关联了哪些低级节点 (避免重做)
        existing_topic_rows = await conn.fetch("""
            SELECT ce.dst_id as topic_id, ce.src_id as node_id
            FROM cold_edges ce
            JOIN cold_nodes cn ON cn.id = ce.dst_id
            WHERE ce.archive_id = $1 AND cn.node_type = 'topic'
        """, archive_id)
    
    nodes = {r["id"]: dict(r) for r in nodes_rows}
    
    # 排除已被 topic 关联的节点
    nodes_in_topic = {r["node_id"] for r in existing_topic_rows}
    nodes_available = {nid: n for nid, n in nodes.items() if nid not in nodes_in_topic}
    
    if len(nodes_available) < D23_MIN_CLUSTER_SIZE:
        return []
    
    # 用 edges 构建无向图找 community (简化: connected components)
    adj: dict[str, set[str]] = {nid: set() for nid in nodes_available}
    for e in edges_rows:
        if e["src_id"] in adj and e["dst_id"] in adj:
            # 只连权重 > 0.3 的强边
            if (e["weight"] or 0) > 0.3:
                adj[e["src_id"]].add(e["dst_id"])
                adj[e["dst_id"]].add(e["src_id"])
    
    visited: set[str] = set()
    clusters: list[list[dict]] = []
    for start in adj:
        if start in visited:
            continue
        # BFS
        component = []
        queue = [start]
        while queue:
            cur = queue.pop()
            if cur in visited:
                continue
            visited.add(cur)
            component.append(cur)
            queue.extend(adj[cur] - visited)
        if D23_MIN_CLUSTER_SIZE <= len(component) <= D23_MAX_CLUSTER_SIZE:
            clusters.append([nodes_available[nid] for nid in component])
    
    # 按 cluster 内总 salience 排序 (高的优先)
    clusters.sort(
        key=lambda cl: -sum(float(n.get("salience", 0)) for n in cl)
    )
    return clusters[:D23_MAX_CLUSTERS_PER_RUN]


def _format_cluster_for_llm(cluster: list[dict]) -> str:
    """LLM 输入: cluster 节点的简短表示。"""
    lines = []
    for n in cluster:
        sal = float(n.get("salience", 0))
        lines.append(
            f"[id={n['id']} type={n['node_type']} salience={sal:.2f}]\n"
            f"  headline: {n['headline']}\n"
            f"  content: {(n['content'] or '')[:300]}"
        )
    return "\n\n".join(lines)


async def _generate_topic_node(cluster: list[dict]) -> dict | None:
    """对一个 cluster 调 LLM 生成 topic 节点."""
    from app.llm import client as llm
    
    cluster_text = _format_cluster_for_llm(cluster)
    input_node_ids = {n["id"] for n in cluster}
    
    user_text = (
        f"## Candidate Related Nodes ({len(cluster)} nodes)\n\n"
        f"{cluster_text}\n\n"
        f"## Task\nDecide whether this group has a useful shared theme. If yes, create one topic node.\n\n判断是否存在共同主题。"
    )
    
    messages = [
        {"role": "system", "content": _LLM_PROMPT_SYSTEM},
        {"role": "user", "content": user_text},
    ]
    
    normalized: dict | None = None

    def _validate(raw):
        nonlocal normalized
        normalized = _normalize_d23_output(raw, input_node_ids)
        return normalized is not None
    
    # 关键: lite_first=False (后台用 main+max 保质量)
    raw = await llm.chat_json_with_upgrade(
        messages,
        validate=_validate,
        label="dream_d23_abstract",
        lite_first=False,
    )
    return normalized or raw


async def _insert_topic_node(
    archive_id: str,
    group_id: str,
    cluster: list[dict],
    topic: dict,
) -> str | None:
    """写入 topic 节点 + 边。返回新节点 id。"""
    from app.db.pool import pool
    
    new_id = f"c_topic_{ulid.ULID()}"
    
    file_meta = json.dumps({
        "dream_generated": True,
        "generator": "d23_abstract",
        "topic_type": topic["topic_type"],
        "subset_count": len(topic.get("subset_node_ids") or cluster),
        "generated_at": time.time(),
    })
    
    # 累加 source_refs (合并 cluster 所有源)
    combined_refs = []
    allowed_source_nodes = {str(x) for x in (topic.get("subset_node_ids") or [])}
    source_cluster = [
        n for n in cluster
        if not allowed_source_nodes or str(n.get("id")) in allowed_source_nodes
    ]
    for n in source_cluster:
        refs = n.get("source_refs")
        if refs:
            if isinstance(refs, str):
                try:
                    refs = json.loads(refs)
                except Exception:
                    refs = []
            combined_refs.extend(refs)
    # 去重
    combined_refs = list(set(combined_refs))[:200]  # 限上限
    
    # topic 的 salience = cluster 平均 salience + 0.1 bonus (因为它是高级抽象)
    avg_sal = sum(float(n.get("salience", 0.3)) for n in cluster) / len(cluster)
    topic_salience = min(1.0, avg_sal + 0.1)
    
    try:
        async with pool().acquire() as conn:
            async with conn.transaction():
                # 1. 写 topic 节点 (复用现有 sanitize_headline / sanitize_summary)
                from app.memory.kb import sanitize_headline, sanitize_summary
                
                await conn.execute("""
                    INSERT INTO cold_nodes
                        (id, archive_id, group_id, user_id, scope,
                         node_type, headline, content,
                         salience, source_refs, file_metadata)
                    VALUES ($1, $2, $3, NULL, 'kb', 'topic', $4, $5, $6, $7::jsonb, $8)
                """,
                    new_id, archive_id, group_id,
                    sanitize_headline(topic["headline"]),
                    sanitize_summary(topic["content"]),
                    topic_salience,
                    json.dumps(combined_refs),
                    file_meta,
                )
                
                # 2. 写边 (低级 → topic), weight 来自 LLM 输出
                allowed_subset = {str(x) for x in (topic.get("subset_node_ids") or [])}
                for low_node_id, w in topic["edge_weights"].items():
                    if allowed_subset and low_node_id not in allowed_subset:
                        continue
                    weight = max(0.1, min(1.0, float(w)))
                    await conn.execute("""
                        INSERT INTO cold_edges
                            (archive_id, src_id, dst_id, weight)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (archive_id, src_id, dst_id)
                        DO UPDATE SET weight = COALESCE(
                            GREATEST(cold_edges.weight, EXCLUDED.weight),
                            cold_edges.weight, EXCLUDED.weight
                        )
                    """, archive_id, low_node_id, new_id, weight)
        return new_id
    except Exception as e:
        dream_log.error(
            "dream.task.d23_abstract.insert_failed",
            f"err={e!r}",
        )
        return None


@register_dream_task
class D23HighLevelAbstract(InfoDrivenTask):
    """D23: KB 高级抽象节点 (核心创新)."""
    
    name = "d23_abstract"
    threshold = D23_THRESHOLD
    uses_llm = True
    
    async def info_fn(self) -> float:
        """信息量 = 累计 kb_nodes_added 事件数."""
        return float(event_bus.total_count("kb_nodes_added"))
    
    async def _do_work(self) -> None:
        """对每个 archive 跑一次 cluster 抽象."""
        from app.db.pool import pool
        
        # 取最近活跃的 (archive_id, group_id)
        # 简化: 取最近 24h 有 fact 节点变化的
        async with pool().acquire() as conn:
            active_rows = await conn.fetch("""
                SELECT DISTINCT archive_id, group_id
                FROM cold_nodes
                WHERE scope = 'kb'
                  AND node_type IN ('fact', 'preference')
                  AND created_at > NOW() - INTERVAL '24 hours'
                LIMIT 10
            """)
        
        if not active_rows:
            return
        
        total_topics_created = 0
        
        for row in active_rows:
            archive_id = row["archive_id"]
            group_id = row["group_id"]
            
            # 复用现有 KB compress lock (与 reactive 串行)
            try:
                from app.memory.kb import _get_kb_compress_lock
                lock = await _get_kb_compress_lock(archive_id, group_id)
            except Exception:
                lock = None
            
            async def _do_for_one():
                nonlocal total_topics_created
                clusters = await _find_candidate_clusters(archive_id, group_id)
                if not clusters:
                    return
                
                dream_log.log(
                    "dream.task.d23_abstract.cluster_found",
                    f"archive={archive_id} group={group_id} clusters={len(clusters)}",
                )
                
                for cluster in clusters:
                    try:
                        topic = await _generate_topic_node(cluster)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        dream_log.error(
                            "dream.task.d23_abstract.llm_failed",
                            f"err={e!r}",
                        )
                        continue
                    
                    if topic is None or topic.get("skip"):
                        if topic and topic.get("skip"):
                            dream_log.log(
                                "dream.task.d23_abstract.skipped",
                                f"reason={topic.get('reason', 'no_topic')[:80]}",
                            )
                        continue
                    
                    # 写入
                    new_id = await _insert_topic_node(archive_id, group_id, cluster, topic)
                    if new_id:
                        total_topics_created += 1
                        dream_log.log(
                            "dream.task.d23_abstract.topic_created",
                            f"id={new_id} headline={topic['headline'][:50]} "
                            f"subset={len(cluster)}",
                        )
            
            if lock:
                if lock.locked():
                    # reactive 在跑 → 跳过, 等下次 dream cycle
                    dream_log.log(
                        "dream.task.d23_abstract.skip_locked",
                        f"archive={archive_id} group={group_id}",
                    )
                    continue
                async with lock:
                    await _do_for_one()
            else:
                await _do_for_one()
        
        if total_topics_created:
            dream_log.log(
                "dream.task.d23_abstract.cycle_done",
                f"created {total_topics_created} topic nodes",
            )
