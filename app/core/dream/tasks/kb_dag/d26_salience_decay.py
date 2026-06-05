"""
D26: Salience 衰减 + 老节点归档

问题: 老节点 salience 不变, 主线程查 topk 总返回老的
目标: 长期未引用 + 低 salience → 归档 (移 archive 表), 释放 KB top 空间

工作流 (规则, 无 LLM):
1. 找 salience < 0.2 且 source_message_ids 中最新消息 > 7d 前
2. 还要看 access_count (现有字段): 0 或 1 次访问的低重要节点
3. 移到 archive (但保留, 可恢复)
4. 删除关联的 edges

阈值: 时间附带 (kb_nodes_added 累计 50 触发一次)
不用 LLM, 规则即可
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.core.dream.dream_log import dream_log
from app.core.dream.event_bus import event_bus
from app.core.dream.registry import register_dream_task
from app.core.dream.task_base import InfoDrivenTask
from app.core.dream.tasks.kb_dag.signals import kb_maintenance_signal_count


D26_THRESHOLD = 50  # kb_nodes_added 累计 50 才考虑衰减
D26_SALIENCE_THRESHOLD = 0.2  # 低于此衰减
D26_AGE_DAYS = 7  # 7 天前的低 salience 衰减
D26_MAX_PER_RUN = 50  # 单次最多处理


@register_dream_task
class D26SalienceDecay(InfoDrivenTask):
    """D26: Salience 衰减 + 老节点归档."""
    
    name = "d26_salience_decay"
    threshold = D26_THRESHOLD
    uses_llm = False
    
    async def info_fn(self) -> float:
        return kb_maintenance_signal_count()
    
    async def _do_work(self) -> None:
        from app.db.pool import pool
        
        async with pool().acquire() as conn:
            # 1. 找低 salience 老节点 + 低访问
            old_nodes = await conn.fetch("""
                SELECT id, archive_id, group_id, node_type, salience, 
                       last_access, access_count, created_at
                FROM cold_nodes
                WHERE scope = 'kb'
                  AND node_type NOT IN ('topic', 'file')
                  AND salience < $1
                  AND COALESCE(access_count, 0) < 2
                  AND created_at < NOW() - INTERVAL '7 days'
                  AND (file_metadata->>'merged_to' IS NULL)
                  AND (file_metadata->>'archived' IS NULL)
                ORDER BY salience ASC, created_at ASC
                LIMIT $2
            """, D26_SALIENCE_THRESHOLD, D26_MAX_PER_RUN)
        
        if not old_nodes:
            return
        
        archived = 0
        decayed = 0
        
        async with pool().acquire() as conn:
            async with conn.transaction():
                for node in old_nodes:
                    sal = float(node["salience"])
                    # 已极低 → 归档
                    if sal < 0.1:
                        # 2026-05-17 Round 14m: jsonb_set → SQLite json_set; '{}'::jsonb cast 移除
                        await conn.execute("""
                            UPDATE cold_nodes
                            SET salience = 0.01,
                                file_metadata = json_set(
                                    COALESCE(file_metadata, '{}'),
                                    '$.archived',
                                    'true'
                                )
                            WHERE id = $1
                        """, node["id"])
                        archived += 1
                    else:
                        # 衰减 (×0.7)
                        new_sal = max(0.01, sal * 0.7)
                        await conn.execute("""
                            UPDATE cold_nodes
                            SET salience = $1
                            WHERE id = $2
                        """, new_sal, node["id"])
                        decayed += 1
        
        if archived or decayed:
            dream_log.log(
                "dream.task.d26_salience_decay.cycle_done",
                f"archived={archived} decayed={decayed}",
            )
