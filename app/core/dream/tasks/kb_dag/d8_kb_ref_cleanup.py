"""
D8: KB 内部失效引用清理

D20 处理 **file 节点 → 文件已删**.
D8 处理 **KB 节点之间** 的失效引用:

1. cold_edges 指向已 merged_to/split_to 的节点 → 转向新节点 (或删边)
2. cold_edges 指向不存在的 cold_nodes → 删边 (cleanup)
3. source_refs 引用的 message_id 已被 mark_processed → 不动 (这是正常)
   但若 group_messages 真被删 → 标记 source_refs 已失效

设计: 完全规则, 无 LLM
风险: 极低 (只动边, 不删节点)
阈值: kb_nodes_added 累计 20 个 (KB 变化引入边失效)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from app.core.dream.dream_log import dream_log
from app.core.dream.event_bus import event_bus
from app.core.dream.registry import register_dream_task
from app.core.dream.task_base import InfoDrivenTask
from app.core.dream.tasks.kb_dag.signals import kb_maintenance_signal_count


D8_THRESHOLD = 10


@register_dream_task
class D8KbRefCleanup(InfoDrivenTask):
    """D8: KB 内部失效引用清理 (cold_edges).
    
    场景 1: 边指向已 merged_to 的节点 → 转向 merge 目标
    场景 2: 边指向不存在的节点 → 删边 (cleanup)
    """
    
    name = "d8_kb_ref_cleanup"
    threshold = D8_THRESHOLD
    uses_llm = False
    
    async def info_fn(self) -> float:
        return kb_maintenance_signal_count()
    
    async def _do_work(self) -> None:
        from app.db.pool import pool
        
        followed = 0
        orphaned = 0
        
        async with pool().acquire() as conn:
            # === 场景 1: 边指向 merged 节点 → 跟随到合并目标 ===
            # 2026-05-17 Round 14o: SQLite 不支持 `UPDATE table_name alias` 和
            # `UPDATE ... FROM other_table`. 改用 correlated subquery (SQLite 兼容).
            # PG-only 语法实测抛 OperationalError('near "ce": syntax error').
            
            # 入边 (dst_id 是 merged) — 跟随到合并目标
            result = await conn.execute("""
                UPDATE cold_edges
                SET dst_id = (
                    SELECT cn.file_metadata->>'merged_to'
                    FROM cold_nodes cn
                    WHERE cn.id = cold_edges.dst_id
                      AND cn.archive_id = cold_edges.archive_id
                )
                WHERE EXISTS (
                    SELECT 1 FROM cold_nodes cn
                    WHERE cn.id = cold_edges.dst_id
                      AND cn.archive_id = cold_edges.archive_id
                      AND cn.file_metadata->>'merged_to' IS NOT NULL
                      AND cold_edges.src_id != cn.file_metadata->>'merged_to'
                )
            """)
            try:
                followed += int(str(result).split()[-1])
            except Exception:
                pass
            
            # 出边 (src_id 是 merged) — 跟随到合并目标
            result = await conn.execute("""
                UPDATE cold_edges
                SET src_id = (
                    SELECT cn.file_metadata->>'merged_to'
                    FROM cold_nodes cn
                    WHERE cn.id = cold_edges.src_id
                      AND cn.archive_id = cold_edges.archive_id
                )
                WHERE EXISTS (
                    SELECT 1 FROM cold_nodes cn
                    WHERE cn.id = cold_edges.src_id
                      AND cn.archive_id = cold_edges.archive_id
                      AND cn.file_metadata->>'merged_to' IS NOT NULL
                      AND cold_edges.dst_id != cn.file_metadata->>'merged_to'
                )
            """)
            try:
                followed += int(str(result).split()[-1])
            except Exception:
                pass
            
            # 清自循环边 (上面 update 可能产生)
            await conn.execute("""
                DELETE FROM cold_edges WHERE src_id = dst_id
            """)
            
            # === 场景 2: 边指向不存在的节点 → 删孤儿边 ===
            result = await conn.execute("""
                DELETE FROM cold_edges
                WHERE NOT EXISTS (
                    SELECT 1 FROM cold_nodes cn
                    WHERE cn.id = cold_edges.src_id AND cn.archive_id = cold_edges.archive_id
                )
                OR NOT EXISTS (
                    SELECT 1 FROM cold_nodes cn
                    WHERE cn.id = cold_edges.dst_id AND cn.archive_id = cold_edges.archive_id
                )
            """)
            try:
                orphaned += int(str(result).split()[-1])
            except Exception:
                pass
            
            # === 场景 3: 重复边去重 (同 src→dst 多条 → 保留 max weight) ===
            # 2026-05-17 Round 14o: SQLite 不支持 UPDATE/DELETE 别名, 去掉 ce alias.
            dedup_result = await conn.execute("""
                DELETE FROM cold_edges
                WHERE EXISTS (
                    SELECT 1 FROM cold_edges ce2
                    WHERE ce2.archive_id = cold_edges.archive_id
                      AND ce2.src_id = cold_edges.src_id
                      AND ce2.dst_id = cold_edges.dst_id
                      AND ce2.weight > cold_edges.weight
                )
            """)
            try:
                dedup_n = int(str(dedup_result).split()[-1])
            except Exception:
                dedup_n = 0
        
        if followed or orphaned or dedup_n:
            dream_log.log(
                "dream.task.d8_kb_ref_cleanup.cycle_done",
                f"followed_merged={followed} orphaned_deleted={orphaned} "
                f"dedup_deleted={dedup_n}",
            )
