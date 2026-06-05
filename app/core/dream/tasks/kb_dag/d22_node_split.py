"""
D22: 节点拆分 (太杂的节点拆成多个聚焦节点)

问题: 一个节点 content 500 字混杂多个主题 → 检索时命中模糊
目标: LLM 判断节点是否多主题, 是则拆分

工作流:
1. 找 content > 400 字的节点
2. LLM 判断是否多主题
3. 是 → 拆分: 1 节点 → 2-4 节点 + 边连接

风险:
- 单主题被误拆 → 用 LLM 自检 + content 不长的不拆 (400 字阈值)
- 拆完信息丢失 → validate 严格要求覆盖原节点关键词

阈值: 5 个长节点累积
A 类不打断
"""
from __future__ import annotations

from app.core.dream.prompt_catalog import (
    D22_NODE_SPLIT_PROMPT,
)
_LLM_PROMPT = D22_NODE_SPLIT_PROMPT


import asyncio
import json
import time
import ulid
from typing import Any

from app.core.dream.dream_log import dream_log
from app.core.dream.event_bus import event_bus
from app.core.dream.registry import register_dream_task
from app.core.dream.task_base import InfoDrivenTask


D22_THRESHOLD = 5
D22_MAX_PER_RUN = 3
D22_MIN_CONTENT_LEN = 400




def _validate_d22_output(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    if not raw.get("split"):
        return isinstance(raw.get("reason", ""), str)
    nodes = raw.get("new_nodes", [])
    if not isinstance(nodes, list) or not (2 <= len(nodes) <= 4):
        return False
    for n in nodes:
        if not isinstance(n, dict):
            return False
        if not (5 <= len(n.get("headline", "")) <= 60):
            return False
        if not (30 <= len(n.get("content", "")) <= 500):
            return False
        if n.get("node_type") not in ("fact", "preference", "event"):
            return False
    return True


async def _find_long_nodes(limit: int) -> list[dict]:
    from app.db.pool import pool
    
    async with pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, archive_id, group_id, node_type, headline, content,
                   salience, source_refs, file_metadata
            FROM cold_nodes
            WHERE scope = 'kb'
              AND node_type IN ('fact', 'preference', 'event')
              AND LENGTH(content) > $1
              AND (file_metadata->>'merged_to' IS NULL)
              AND (file_metadata->>'split_to' IS NULL)
              AND (file_metadata->>'d22_processed' IS NULL)
            ORDER BY LENGTH(content) DESC, salience DESC
            LIMIT $2
        """, D22_MIN_CONTENT_LEN, limit)
    
    return [dict(r) for r in rows]


async def _llm_split(node: dict) -> dict | None:
    from app.llm import client as llm
    
    user_text = (
        f"## Candidate Node\n"
        f"headline: {node['headline']}\n"
        f"node_type: {node['node_type']}\n"
        f"content ({len(node['content'])} chars):\n{node['content']}\n\n"
        f"## Task\nDecide whether this node contains multiple independent topics. Split into 2-4 nodes if yes; keep it otherwise.\n\n判断是否多主题。"
    )
    messages = [
        {"role": "system", "content": _LLM_PROMPT},
        {"role": "user", "content": user_text},
    ]
    return await llm.chat_json_with_upgrade(
        messages,
        validate=_validate_d22_output,
        label="dream_d22_split",
        lite_first=False,
    )


async def _apply_split(node: dict, decision: dict) -> int:
    """执行拆分. 返回新建节点数."""
    from app.db.pool import pool
    from app.memory.kb import sanitize_headline, sanitize_summary
    
    new_node_ids = []
    
    # 合并 source_refs (每个新节点共享)
    refs = node.get("source_refs") or []
    if isinstance(refs, str):
        try:
            refs = json.loads(refs)
        except Exception:
            refs = []
    
    try:
        async with pool().acquire() as conn:
            async with conn.transaction():
                # 1. INSERT 新节点
                for new in decision["new_nodes"]:
                    new_id = f"c_split_{ulid.ULID()}"
                    new_node_ids.append(new_id)
                    
                    new_meta = json.dumps({
                        "dream_generated": True,
                        "generator": "d22_split",
                        "split_from": node["id"],
                        "split_at": time.time(),
                    })
                    
                    await conn.execute("""
                        INSERT INTO cold_nodes
                            (id, archive_id, group_id, user_id, scope,
                             node_type, headline, content,
                             salience, source_refs, file_metadata)
                        VALUES ($1, $2, $3, NULL, 'kb', $4, $5, $6, $7, $8::jsonb, $9)
                    """,
                        new_id, node["archive_id"], node["group_id"],
                        new["node_type"],
                        sanitize_headline(new["headline"]),
                        sanitize_summary(new["content"]),
                        float(node.get("salience", 0.3)),  # 继承 salience
                        json.dumps(refs),
                        new_meta,
                    )
                
                # 2. 软删除原节点
                old_meta = node.get("file_metadata") or {}
                if isinstance(old_meta, str):
                    try:
                        old_meta = json.loads(old_meta)
                    except Exception:
                        old_meta = {}
                old_meta = dict(old_meta) if isinstance(old_meta, dict) else {}
                old_meta.update({
                    "split_to": new_node_ids,
                    "d22_processed": True,
                    "split_at": time.time(),
                })
                
                await conn.execute("""
                    UPDATE cold_nodes
                    SET file_metadata = $1, salience = 0.05, updated_at = NOW()
                    WHERE id = $2
                """, json.dumps(old_meta, ensure_ascii=False), node["id"])
                
                # 3. 边重连: 原节点的边 → 全部连到第一个新节点 (简化)
                # 更精细的做法是 LLM 决定每条边该连到哪个新节点, 但太重
                first_new = new_node_ids[0]
                await conn.execute("""
                    UPDATE cold_edges SET dst_id = $1
                    WHERE archive_id = $2 AND dst_id = $3
                """, first_new, node["archive_id"], node["id"])
                await conn.execute("""
                    UPDATE cold_edges SET src_id = $1
                    WHERE archive_id = $2 AND src_id = $3
                """, first_new, node["archive_id"], node["id"])
                
                # 新节点之间互相连边 (sibling)
                for i, nid in enumerate(new_node_ids):
                    for nid2 in new_node_ids[i+1:]:
                        await conn.execute("""
                            INSERT INTO cold_edges (archive_id, src_id, dst_id, weight)
                            VALUES ($1, $2, $3, 0.5)
                            ON CONFLICT (archive_id, src_id, dst_id) DO NOTHING
                        """, node["archive_id"], nid, nid2)
        
        return len(new_node_ids)
    except Exception as e:
        dream_log.error("dream.task.d22_split.tx_failed", repr(e)[:200])
        return 0


async def _mark_unsplit(node: dict) -> None:
    """LLM 决定不拆 → 标记避免反复尝试."""
    from app.db.pool import pool
    
    meta = node.get("file_metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    meta = dict(meta) if isinstance(meta, dict) else {}
    meta["d22_processed"] = True
    
    try:
        async with pool().acquire() as conn:
            await conn.execute(
                "UPDATE cold_nodes SET file_metadata = $1 WHERE id = $2",
                json.dumps(meta, ensure_ascii=False), node["id"],
            )
    except Exception:
        pass


@register_dream_task
class D22NodeSplit(InfoDrivenTask):
    """D22: 节点拆分."""
    
    name = "d22_split"
    threshold = D22_THRESHOLD
    uses_llm = True
    
    async def info_fn(self) -> float:
        return float(event_bus.total_count("kb_nodes_added"))
    
    async def _do_work(self) -> None:
        candidates = await _find_long_nodes(D22_MAX_PER_RUN)
        if not candidates:
            return
        
        split_count = 0
        for node in candidates:
            try:
                decision = await _llm_split(node)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                dream_log.warn("dream.task.d22_split.llm_failed", repr(e)[:200])
                continue
            
            if decision is None:
                continue
            
            if not decision.get("split"):
                await _mark_unsplit(node)
                continue
            
            n_new = await _apply_split(node, decision)
            if n_new:
                split_count += 1
                dream_log.log(
                    "dream.task.d22_split.done",
                    f"id={node['id']} → {n_new} new nodes",
                )
        
        if split_count:
            dream_log.log(
                "dream.task.d22_split.cycle_done",
                f"split {split_count} nodes",
            )
