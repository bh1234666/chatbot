"""
D19: 索引复审 (老索引重打分)
D20: 失效引用清理 (文件已删但索引还在)

D19: 老节点的 salience 可能不准 (用户最近没用), 用 access_count + 时间衰减重打分
     注: D26 是低 salience 的归档. D19 是动态调整所有节点的 salience.

D20: file 节点引用的 workspace_path 不存在 → 标记 archived (不真删)

都是规则任务, 无 LLM. 阈值: kb_nodes_added 累计.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from app.core.dream.dream_log import dream_log
from app.core.dream.event_bus import event_bus
from app.core.dream.registry import register_dream_task
from app.core.dream.task_base import InfoDrivenTask
from app.core.dream.tasks.kb_dag.signals import (
    file_indexed_count,
    kb_maintenance_signal_count,
)


D19_THRESHOLD = 25  # node/file-index changes trigger salience review in small batches
D19_MAX_PER_RUN = 100
D20_THRESHOLD = 5  # 累计 5 个文件上传触发清理 (file_uploaded 事件)


@register_dream_task
class D19IndexReview(InfoDrivenTask):
    """D19: 索引复审 - 重打分 salience.
    
    规则:
    - access_count > 5 + 最近 24h 访问 → 加 0.1 salience
    - access_count = 0 + 老于 30 天 → salience × 0.8
    - 不动 topic / file 节点 (避免影响 D23/D15-D17 写的元数据)
    """
    
    name = "d19_index_review"
    threshold = D19_THRESHOLD
    uses_llm = False
    
    async def info_fn(self) -> float:
        return kb_maintenance_signal_count()
    
    async def _do_work(self) -> None:
        from app.db.pool import pool
        
        # 2026-05-17 Round 14m: 改写 SQLite 兼容
        # - jsonb_set(...) → json_set(meta, '$.key', 'value')
        # - ::jsonb cast 移除 (translate_sql 已移除)
        # - ->>  → json_extract(col, '$.key')
        # - NOW() - INTERVAL → 中央 translate 已处理 + 14k datetime wrap
        async with pool().acquire() as conn:
            # 1. 高频访问的 → 提升 salience
            boosted = await conn.execute("""
                UPDATE cold_nodes
                SET salience = LEAST(1.0, salience + 0.1)
                WHERE scope = 'kb'
                  AND node_type IN ('fact', 'preference', 'event')
                  AND COALESCE(access_count, 0) > 5
                  AND last_access > NOW() - INTERVAL '24 hours'
                  AND salience < 0.9
                  AND (json_extract(file_metadata, '$.d19_boosted_recently') IS NULL
                       OR json_extract(file_metadata, '$.d19_boosted_recently') = 'false')
                  AND json_extract(file_metadata, '$.merged_to') IS NULL
            """)
            
            # 标记已 boost (避免反复加)
            await conn.execute("""
                UPDATE cold_nodes
                SET file_metadata = json_set(
                    COALESCE(file_metadata, '{}'),
                    '$.d19_boosted_recently',
                    'true'
                )
                WHERE scope = 'kb'
                  AND COALESCE(access_count, 0) > 5
                  AND last_access > NOW() - INTERVAL '24 hours'
            """)
            
            # 2. 长期 idle 的 → 衰减 salience (但不到 D26 阈值)
            decayed = await conn.execute("""
                UPDATE cold_nodes
                SET salience = GREATEST(0.05, salience * 0.85),
                    file_metadata = json_set(
                        COALESCE(file_metadata, '{}'),
                        '$.d19_boosted_recently',
                        'false'
                    )
                WHERE scope = 'kb'
                  AND node_type IN ('fact', 'preference', 'event')
                  AND COALESCE(access_count, 0) = 0
                  AND created_at < NOW() - INTERVAL '30 days'
                  AND salience > 0.2
                  AND json_extract(file_metadata, '$.merged_to') IS NULL
            """)
        
        # boosted/decayed 形如 "UPDATE N"
        def _n(s):
            try:
                return int(str(s).split()[-1])
            except Exception:
                return 0
        
        n_boost = _n(boosted)
        n_decay = _n(decayed)
        
        if n_boost or n_decay:
            dream_log.log(
                "dream.task.d19_index_review.cycle_done",
                f"boosted={n_boost} decayed={n_decay}",
            )


@register_dream_task
class D20InvalidRefCleanup(InfoDrivenTask):
    """D20: 失效文件标记 - 文件已删 → 标记 [已删除], 保留索引供历史查询.
    
    设计 (按用户要求改进):
    - 不真删节点 (软删 + 软隐藏 都不是)
    - 在 headline / content 加 [已删除] 标记
    - file_metadata.deleted = True (区别于 archived)
    - salience 适度降 (但不归零, 保留检索可能)
    - 主线程 search 仍能命中, 但能看到删除状态
    
    效果: 模型知道"曾经存在过这个文件, 但被删了"
    """
    
    name = "d20_invalid_ref_cleanup"
    threshold = D20_THRESHOLD
    uses_llm = False
    
    async def info_fn(self) -> float:
        return float(event_bus.total_count("file_uploaded")) + file_indexed_count()
    
    async def _do_work(self) -> None:
        from app.db.pool import pool
        import time as _t
        
        # 2026-05-17 Round 14m: 不用 EXTRACT(EPOCH FROM ...) — SQLite 不支持 EXTRACT.
        # 不用 ->> — SQLite 老版本不支持. 全 Python 层 filter + 算 age.
        async with pool().acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, archive_id, group_id, headline, content, file_metadata,
                       created_at
                FROM cold_nodes
                WHERE scope = 'kb' AND node_type = 'file'
                LIMIT 500
            """)
        
        if not rows:
            return
        
        # Python 层 filter (deleted, has workspace_path) + 算 age
        now_ts = _t.time()
        from datetime import datetime as _dt
        filtered_rows = []
        for r in rows:
            meta = r.get("file_metadata")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    continue
            if not isinstance(meta, dict):
                continue
            # filter: 未标 deleted + 有 workspace_path
            if str(meta.get("deleted", "")).lower() == "true":
                continue
            ws_path = meta.get("workspace_path", "")
            if not ws_path:
                continue
            # 算 age
            ts_raw = r.get("created_at")
            age_sec = 0.0
            try:
                if isinstance(ts_raw, (int, float)):
                    age_sec = now_ts - float(ts_raw)
                elif isinstance(ts_raw, str):
                    # 尝试 ISO 解析
                    try:
                        ts_dt = _dt.fromisoformat(ts_raw.replace(' ', 'T'))
                        age_sec = now_ts - ts_dt.timestamp()
                    except (ValueError, TypeError):
                        age_sec = 0.0
            except Exception:
                age_sec = 0.0
            # 保存 row 加 age_sec
            r_dict = dict(r) if not isinstance(r, dict) else r
            r_dict["age_sec"] = age_sec
            r_dict["_meta_parsed"] = meta
            r_dict["_ws_path"] = ws_path
            filtered_rows.append(r_dict)
        rows = filtered_rows
        
        if not rows:
            return
        
        # 检查每个文件路径是否仍存在
        from app.core.dream.cache import _get_workspace_root
        ws_root = _get_workspace_root()
        
        if not ws_root:
            return
        
        invalid = []  # [(id, headline, content, meta, reason), ...]
        for r in rows:
            meta = r["file_metadata"]
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    continue
            if not isinstance(meta, dict):
                continue
            
            ws_path = meta.get("workspace_path", "")
            full = os.path.join(ws_root, r["archive_id"], r["group_id"], ws_path)
            
            if not os.path.isfile(full):
                # 类型 1: 文件不在磁盘 → file_not_found
                invalid.append({
                    "id": r["id"],
                    "headline": r["headline"] or "",
                    "content": r["content"] or "",
                    "meta": meta,
                    "reason": "file_not_found",
                })
                continue
            
            # 类型 2 (2026-05-16 新增): 文件在磁盘, 但 download_status='pending'
            # 且老于 1 天 → 大概率是 phase 2 未完成的 placeholder, 标 [已删除]
            # 病因: heal 内存 cooldown 重启失效, 122 pending 节点反复 reschedule.
            # 老于 1 天 + pending = 几乎确定永久失败 (多次重启都没成).
            try:
                age_sec = float(r["age_sec"] or 0)
            except (TypeError, ValueError):
                age_sec = 0
            
            if (meta.get("download_status") == "pending"
                and age_sec > 86400  # 1 day
            ):
                invalid.append({
                    "id": r["id"],
                    "headline": r["headline"] or "",
                    "content": r["content"] or "",
                    "meta": meta,
                    "reason": "stale_pending_over_1_day",
                })
        
        if not invalid:
            return
        
        # 逐个标记 [已删除] (按 reason 分类)
        import time as _t
        now_ts = _t.time()
        reason_counts: dict[str, int] = {}
        
        for item in invalid:
            new_headline = item["headline"]
            new_content = item["content"]
            reason = item.get("reason", "file_not_found")
            
            # 加 [已删除] 标记 (如未加过)
            if not new_headline.startswith("[已删除]"):
                new_headline = f"[已删除] {new_headline[:25]}"
            
            # content 头部加说明 (让模型一眼知道是已删除文件)
            reason_human = {
                "file_not_found": "文件不在磁盘",
                "stale_pending_over_1_day": "下载/摘要从未完成 (>1 天)",
            }.get(reason, reason)
            deleted_note = (
                f"[文件已删除, 索引保留供历史检索]\n"
                f"删除时间: {_t.strftime('%Y-%m-%d %H:%M', _t.localtime(now_ts))}\n"
                f"删除原因: {reason_human}\n"
                f"\n[原内容]\n"
            )
            if not new_content.startswith("[文件已删除"):
                new_content = deleted_note + new_content
            
            # 更新 metadata
            new_meta = dict(item["meta"])
            new_meta.update({
                "deleted": True,
                "deleted_at": now_ts,
                "deleted_reason": reason,
                "deleted_original_path": new_meta.get("workspace_path", ""),
            })
            
            try:
                async with pool().acquire() as conn:
                    await conn.execute("""
                        UPDATE cold_nodes
                        SET headline = $1,
                            content = $2,
                            file_metadata = $3,
                            salience = LEAST(salience, 0.2),
                            updated_at = NOW()
                        WHERE id = $4
                    """, new_headline, new_content,
                         json.dumps(new_meta, ensure_ascii=False),
                         item["id"])
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            except Exception as e:
                dream_log.warn(
                    "dream.task.d20_invalid_ref_cleanup.update_failed",
                    f"id={item['id']} err={e!r}"[:200],
                )
                continue
        
        if reason_counts:
            summary = " ".join(f"{k}={v}" for k, v in reason_counts.items())
            dream_log.log(
                "dream.task.d20_invalid_ref_cleanup.cycle_done",
                f"marked {sum(reason_counts.values())} file nodes as [已删除]: {summary}",
            )
