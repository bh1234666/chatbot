"""
D1: hot → warm 主动压缩
D2: warm → cold 主动压缩
D3: KB 占位清理

升级现有 reactive:
- reactive: 每 chat 完成后跑, lite_first=True (快)
- dream: 信息量驱动 + lite_first=False (后台用 main+max 提质量)

共享现有 lock 与 reactive 串行.
"""
from __future__ import annotations

import asyncio

from app.core.dream.dream_log import dream_log
from app.core.dream.event_bus import event_bus
from app.core.dream.registry import register_dream_task
from app.core.dream.task_base import InfoDrivenTask


D1_THRESHOLD = 20  # 累计 20 个新 turn 触发 hot→warm
D2_THRESHOLD = 50  # 累计 50 个新 KB 节点触发 warm→cold
D3_THRESHOLD = 10  # 累计 10 个新 KB 节点触发占位清理


async def _get_active_users(archive_id: str, group_id: str, since_hours: int = 24) -> list[str]:
    """取最近 N 小时活跃 user."""
    from app.db.pool import pool
    
    async with pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT user_id
            FROM hot_user_turns
            WHERE archive_id = $1 AND group_id = $2
              AND created_at > NOW() - INTERVAL '24 hours'
            LIMIT 50
        """, archive_id, group_id)
    return [r["user_id"] for r in rows]


@register_dream_task
class D1HotToWarm(InfoDrivenTask):
    """D1: hot → warm 主动压缩 (升级 reactive)."""
    
    name = "d1_hot_to_warm"
    threshold = D1_THRESHOLD
    uses_llm = True
    
    async def info_fn(self) -> float:
        return float(event_bus.total_count("hot_turn_added"))
    
    async def _do_work(self) -> None:
        from app.db.pool import pool
        from app.memory import hot, warm
        
        # 取最近活跃 (archive, group)
        async with pool().acquire() as conn:
            active = await conn.fetch("""
                SELECT DISTINCT archive_id, group_id
                FROM hot_user_turns
                WHERE created_at > NOW() - INTERVAL '24 hours'
                LIMIT 10
            """)
        
        compressed = 0
        
        for row in active:
            archive_id = row["archive_id"]
            group_id = row["group_id"]
            
            # 用户级别
            users = await _get_active_users(archive_id, group_id)
            for user_id in users:
                try:
                    u_overflow = await hot.get_user_hot_overflow(
                        archive_id, group_id, user_id
                    )
                except Exception:
                    continue
                
                if not u_overflow or len(u_overflow) < 5:
                    continue
                
                try:
                    # 复用现有压缩函数 - 它内部用 chat_json_with_upgrade
                    # 但 reactive 默认 lite_first=True, 我们用 dream 模式跑
                    # 注: compress_user_overflow 内部参数固定了 lite_first=True
                    #     dream 这里就用现有的, 它失败会升级到 main+max
                    await warm.compress_user_overflow(
                        archive_id, group_id, user_id, u_overflow
                    )
                    compressed += 1
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    dream_log.warn(
                        "dream.task.d1_hot_to_warm.user_failed",
                        f"user={user_id} err={e!r}"[:200],
                    )
            
            # 群级 events
            try:
                g_overflow = await hot.get_group_events_overflow(archive_id, group_id)
                if g_overflow and len(g_overflow) >= 5:
                    await warm.compress_group_overflow(
                        archive_id, group_id, g_overflow
                    )
                    compressed += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                dream_log.warn(
                    "dream.task.d1_hot_to_warm.group_failed",
                    f"group={group_id} err={e!r}"[:200],
                )
        
        if compressed:
            dream_log.log(
                "dream.task.d1_hot_to_warm.cycle_done",
                f"compressed {compressed} buckets",
            )


@register_dream_task
class D2WarmToCold(InfoDrivenTask):
    """D2: warm → cold 主动压缩."""
    
    name = "d2_warm_to_cold"
    threshold = D2_THRESHOLD
    uses_llm = True
    
    async def info_fn(self) -> float:
        return float(event_bus.total_count("kb_nodes_added"))
    
    async def _do_work(self) -> None:
        from app.db.pool import pool
        from app.memory import warm, cold
        
        # 2026-05-17 Round 14p: 表名修 warm_user_summaries → warm_memories
        # 实测 trace 10:43: OperationalError no such table: warm_user_summaries.
        # 工程实际表名 warm_memories (单表, scope 列区分 user/group), 见 memory/warm.py.
        async with pool().acquire() as conn:
            active = await conn.fetch("""
                SELECT DISTINCT archive_id, group_id
                FROM warm_memories
                WHERE created_at > NOW() - INTERVAL '24 hours'
                LIMIT 10
            """)
        
        compressed = 0
        
        for row in active:
            archive_id = row["archive_id"]
            group_id = row["group_id"]
            
            # 取活跃 user 列表 (从 warm 表, scope='user')
            async with pool().acquire() as conn:
                user_rows = await conn.fetch("""
                    SELECT DISTINCT user_id FROM warm_memories
                    WHERE archive_id = $1 AND group_id = $2
                      AND scope = 'user'
                      AND user_id IS NOT NULL
                    LIMIT 30
                """, archive_id, group_id)
            
            for ur in user_rows:
                user_id = ur["user_id"]
                try:
                    w_overflow = await warm.get_user_warm_overflow(
                        archive_id, group_id, user_id
                    )
                except Exception:
                    continue
                
                if not w_overflow or len(w_overflow) < 10:
                    continue
                
                try:
                    await cold.compress_user_warm_to_cold(
                        archive_id, group_id, user_id
                    )
                    compressed += 1
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    dream_log.warn(
                        "dream.task.d2_warm_to_cold.failed",
                        f"user={user_id} err={e!r}"[:200],
                    )
        
        if compressed:
            dream_log.log(
                "dream.task.d2_warm_to_cold.cycle_done",
                f"compressed {compressed} users",
            )


@register_dream_task
class D3KbPlaceholderCleanup(InfoDrivenTask):
    """D3: KB stale placeholder cleanup.
    
    两类处理:
    1. 现有 cleanup_stale_file_placeholders: 清"被真实摘要覆盖"的 placeholder
       (会真 DELETE - 因为该 placeholder 已有更好的版本替代)
    2. 新增: 老于 1 天的孤儿 stale placeholder → 标 [已删除]
       (跟 D20 / _mark_download_failed 一致风格, 保留节点供历史检索)
    
    log 实测教训 (2026-05-16 trace 21:57): 启动时过滤 134 个 stale placeholder,
    但 cleanup_stale_file_placeholders 只清 "被覆盖" 的 (~7 个), 剩 ~127 个孤儿
    永远积累. D3 现在主动处理这些孤儿.
    """
    
    name = "d3_kb_placeholder"
    threshold = D3_THRESHOLD
    uses_llm = False

    # 2026-05-21: info_fn 是 500 行全表扫描 + 逐行 json.loads + 日期解析(注释自述"慢但稳")。
    # 实测 trace c647979: 高 helper 活动把 supervisor wake 触发 243 次,每次 should_run/urgency
    # 都调 info_fn 做一遍全扫,结论几乎全是 orphan=0("不用跑")→ 几百次昂贵全扫纯浪费 + 243 条
    # 相同 info_stats 刷屏。加一个短 TTL 缓存:KB placeholder 状态秒级内不会剧变(threshold=10,
    # 差几秒无影响),把高频 wake 的重复全扫降到每 ~45s 一次。正确性不变(仍是同一 info 值)。
    _INFO_CACHE_TTL_SEC = 45.0

    async def info_fn(self) -> float:
        import time as _t
        _now = _t.time()
        _cached_at = getattr(self, "_info_cache_at", 0.0)
        if _now - _cached_at < self._INFO_CACHE_TTL_SEC:
            return getattr(self, "_info_cache_val", 0.0)
        _val = await self._info_fn_uncached()
        self._info_cache_at = _now
        self._info_cache_val = _val
        return _val

    async def _info_fn_uncached(self) -> float:
        """信息量 = 实际待清孤儿 placeholder 数.
        
        2026-05-17 Round 14h 终极修 (Round 14f 修了 ->> 但仍 TypeError):
        Round 14f 的 SQL 不含 ->>, 仍抛 TypeError "'<' not supported between int and str".
        根因: cold_nodes.created_at 列**混合类型** — 一些 row 是 INTEGER (unix ts),
        一些 row 是 TEXT (ISO string). SQL WHERE created_at < datetime(...) 比较时
        SQLite 内部类型亲和性出 bug → Python 收到时抛 TypeError.
        
        终极修: SQL 不在 WHERE 做日期比较, 拉所有 KB file 节点, Python 层 filter.
        慢一点但 100% 稳.
        """
        from app.db.pool import pool
        from app.memory.kb import _is_placeholder_content
        import json as _json
        import datetime as _dt
        import time as _t
        
        try:
            async with pool().acquire() as conn:
                # 2026-05-17 Round 14h: 不用 created_at < NOW() - INTERVAL (类型亲和性 bug).
                # 拉前 500 个 KB file 节点, Python 层用 datetime 解析+比较, 100% 稳.
                rows = await conn.fetch("""
                    SELECT id, headline, content, file_metadata, created_at
                    FROM cold_nodes
                    WHERE scope = 'kb' AND node_type = 'file'
                    LIMIT 500
                """)
        except Exception as e:
            import traceback as _tb
            dream_log.warn(
                f"dream.task.{self.name}.info_fn_failed",
                f"SQL fetch err: {e!r}; tb_tail: {_tb.format_exc()[-300:]}",
            )
            return 0.0
        
        # Python 层日期 filter: 老于 1 day
        _cutoff_ts = _t.time() - 86400  # 1 day 前
        _cutoff_iso_naive = (_dt.datetime.now() - _dt.timedelta(days=1)).isoformat(sep=' ')
        
        orphan_count = 0
        _first_row_err_logged = False
        _filtered_by_age = 0
        _filtered_by_deleted = 0
        
        for row in rows:
            try:
                # 1. 日期 filter (Python 层, 兼容 int/str created_at)
                ts_raw = row.get("created_at")
                ts_is_old = True  # 默认当老的处理
                if ts_raw is not None:
                    if isinstance(ts_raw, (int, float)):
                        # unix timestamp
                        ts_is_old = ts_raw < _cutoff_ts
                    elif isinstance(ts_raw, str):
                        # ISO string '2026-05-17 08:28:30' 或 '2026-05-17T08:28:30'
                        # 字符串字典序比较 OK (ISO 8601 排序友好)
                        ts_is_old = ts_raw < _cutoff_iso_naive
                if not ts_is_old:
                    _filtered_by_age += 1
                    continue
                
                # 2. deleted filter (Python 层, json.loads)
                raw_meta = row.get("file_metadata")
                if raw_meta:
                    try:
                        meta = _json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
                        if isinstance(meta, dict) and str(meta.get("deleted", "")).lower() == "true":
                            _filtered_by_deleted += 1
                            continue
                    except (ValueError, TypeError):
                        pass
                
                # 3. placeholder check
                if _is_placeholder_content(row.get("headline"), row.get("content") or ""):
                    orphan_count += 1
            except Exception as _row_e:
                if not _first_row_err_logged:
                    import traceback as _tb
                    dream_log.warn(
                        f"dream.task.{self.name}.row_check_failed",
                        f"row err: {_row_e!r}; tb_tail: {_tb.format_exc()[-200:]}",
                    )
                    _first_row_err_logged = True
                continue
        
        # 周期 log 看到诊断 (每次 should_run 都会调, 但 dream_log 自己有 debounce)
        if orphan_count > 0 or _filtered_by_age > 0 or _filtered_by_deleted > 0:
            dream_log.log(
                f"dream.task.{self.name}.info_stats",
                f"total_rows={len(rows)} orphan={orphan_count} "
                f"filtered_age={_filtered_by_age} filtered_deleted={_filtered_by_deleted}",
            )
        
        return float(orphan_count)
    
    async def should_run(self) -> bool:
        """绝对触发: 只要孤儿数 ≥ threshold 就跑 (不依赖 delta).
        
        2026-05-16: watermark 卡死防御 (类似 D27). 用增量逻辑会出现:
        - 第一轮: 138 个孤儿, 清掉 100 个, last_run_info=38
        - 第二轮: 38 个孤儿, delta=0 → False, 永不清 (即使还有 38 个)
        """
        import time as _t
        if self.suspended_until > _t.time():
            return False
        try:
            current = await self.info_fn()
        except Exception:
            return False
        return current >= self.threshold
    
    async def _do_work(self) -> None:
        from app.db.pool import pool
        from app.memory import kb
        
        # ── Part 1: 调现有 cleanup_stale_file_placeholders (清被覆盖的) ──
        # 2026-05-17 Round 14k: 不用 created_at > NOW()-INTERVAL (混合类型抛 TypeError).
        # 拉所有 active archive/group, cleanup_stale_file_placeholders 自身 idempotent,
        # 即使老群也 cleanup 不会有副作用 (它只清"被覆盖的"重复 placeholder).
        try:
            async with pool().acquire() as conn:
                active = await conn.fetch("""
                    SELECT DISTINCT archive_id, group_id
                    FROM cold_nodes
                    WHERE scope = 'kb' AND node_type = 'file'
                    LIMIT 20
                """)
        except Exception as e:
            import traceback as _tb
            dream_log.warn(
                "dream.task.d3_kb_placeholder.active_query_failed",
                f"err={e!r}; tb_tail: {_tb.format_exc()[-200:]}",
            )
            active = []
        
        covered_cleaned = 0
        for row in active:
            try:
                n = await kb.cleanup_stale_file_placeholders(
                    row["archive_id"], row["group_id"]
                )
                covered_cleaned += n
            except asyncio.CancelledError:
                raise
            except Exception as e:
                dream_log.warn(
                    "dream.task.d3_kb_placeholder.covered_failed",
                    f"err={e!r}"[:200],
                )
        
        # ── Part 2: 标记孤儿 stale placeholder 为 [已删除] ──
        # 老于 1 天 + content 含 placeholder 模板词 + 未标 deleted
        orphan_marked = await self._mark_orphan_placeholders()
        
        if covered_cleaned or orphan_marked:
            # 2026-05-21: 真清理后 orphan 数已变,使 info 缓存失效,下次 should_run 重新真扫。
            self._info_cache_at = 0.0
            dream_log.log(
                "dream.task.d3_kb_placeholder.cycle_done",
                f"deleted_covered={covered_cleaned} marked_orphans={orphan_marked}",
            )
    
    async def _mark_orphan_placeholders(self) -> int:
        """标记孤儿 stale placeholder 为 [已删除] (保留节点)."""
        from app.db.pool import pool
        from app.memory.kb import _is_placeholder_content
        import json as _json
        import time as _t
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        
        try:
            async with pool().acquire() as conn:
                # 2026-05-17 Round 14h: 不用 created_at < NOW()-INTERVAL + ->> (类型亲和性 + PG-only).
                # 拉 500 个, Python 层 filter age + deleted.
                rows = await conn.fetch("""
                    SELECT id, archive_id, group_id, headline, content, file_metadata, created_at
                    FROM cold_nodes
                    WHERE scope = 'kb' AND node_type = 'file'
                    LIMIT 500
                """)
        except Exception as e:
            import traceback as _tb
            dream_log.warn(
                "dream.task.d3_kb_placeholder.orphan_query_failed",
                f"err={e!r}; tb_tail: {_tb.format_exc()[-200:]}",
            )
            return 0
        
        if not rows:
            return 0
        
        # Python 层 filter age + deleted
        _cutoff_ts = _t.time() - 86400
        _cutoff_iso = (_dt.now() - _td(days=1)).isoformat(sep=' ')
        
        _filtered_rows = []
        for r in rows:
            ts_raw = r.get("created_at")
            ts_is_old = True
            if ts_raw is not None:
                if isinstance(ts_raw, (int, float)):
                    ts_is_old = ts_raw < _cutoff_ts
                elif isinstance(ts_raw, str):
                    ts_is_old = ts_raw < _cutoff_iso
            if not ts_is_old:
                continue
            meta_raw = r.get("file_metadata")
            if meta_raw:
                try:
                    _m = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
                    if isinstance(_m, dict) and str(_m.get("deleted", "")).lower() == "true":
                        continue
                except (ValueError, TypeError):
                    pass
            _filtered_rows.append(r)
        rows = _filtered_rows
        
        if not rows:
            return 0
        
        marked = 0
        now_ts = _t.time()
        _human_time = _dt.fromtimestamp(now_ts, tz=_tz(_td(hours=8))).strftime("%Y-%m-%d %H:%M")
        
        for r in rows:
            headline = r["headline"] or ""
            content = r["content"] or ""
            
            # 检测 placeholder
            if not _is_placeholder_content(headline, content):
                continue
            
            # 解析 metadata
            meta = r["file_metadata"]
            if isinstance(meta, str):
                try:
                    meta = _json.loads(meta)
                except Exception:
                    meta = {}
            meta = dict(meta) if isinstance(meta, dict) else {}
            
            # 标 [已删除] (跟 D20 / _mark_download_failed 一致)
            new_headline = (
                headline if headline.startswith("[已删除]")
                else f"[已删除] {headline[:25]}"
            )
            deleted_note = (
                f"[文件已删除, 索引保留供历史检索]\n"
                f"删除时间: {_human_time}\n"
                f"删除原因: stale placeholder (从未成功摘要, 老于 1 天)\n"
                f"\n[原内容]\n"
            )
            new_content = (
                content if content.startswith("[文件已删除")
                else deleted_note + content
            )
            
            meta.update({
                "deleted": True,
                "deleted_at": now_ts,
                "deleted_reason": "stale_placeholder_orphan",
                "deleted_original_path": meta.get("workspace_path", ""),
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
                         _json.dumps(meta, ensure_ascii=False),
                         r["id"])
                marked += 1
            except Exception as e:
                dream_log.warn(
                    "dream.task.d3_kb_placeholder.mark_failed",
                    f"id={r['id']} err={e!r}"[:200],
                )
                continue
        
        return marked


# ──────────────────────────────────────────────────────────────────
# D27: pending backlog 消化
# ──────────────────────────────────────────────────────────────────

D27_THRESHOLD = 10  # ≥10 个 pending 触发


@register_dream_task
class D27HealPendingBacklog(InfoDrivenTask):
    """D27: 消化积压的 pending file 节点 backlog.
    
    问题 (实测 log: 122 pending 启动时, 每次 heal 只 reschedule 3 个):
    - sync_group_files / _heal_pending_nodes 受 MAX_BG_DOWNLOADS=3 限速
    - 122 个 pending 要 ~40 次 chat 才能清完
    - 实际上多数 pending 节点是 NapCat file_id 已失效 (永远拿不到 URL)
    
    D27 策略:
    1. 查所有 pending 节点 (download_status='pending', 未 deleted)
    2. 老于 1 小时 → 大概率是 stale (chat 多次都没成功), 标 [已删除]
    3. 单次最多处理 50 个 (避免 DB 长锁)
    """
    
    name = "d27_heal_pending_backlog"
    threshold = D27_THRESHOLD
    uses_llm = False
    
    async def info_fn(self) -> float:
        """信息量 = pending file 节点总数.
        
        2026-05-17 Round 14h: SQL 不用 ->> + NOW()-INTERVAL (SQLite 不支持 / 类型亲和性).
        Python 层解析 file_metadata JSON 看 deleted + download_status.
        """
        from app.db.pool import pool
        import json as _json
        import time as _t
        try:
            async with pool().acquire() as conn:
                rows = await conn.fetch("""
                    SELECT file_metadata, created_at
                    FROM cold_nodes
                    WHERE scope = 'kb' AND node_type = 'file'
                    LIMIT 500
                """)
        except Exception:
            return 0.0
        
        # Python 层 filter: pending + 老于 1 hour + 未 deleted
        _cutoff_ts = _t.time() - 3600
        from datetime import datetime as _dt, timedelta as _td
        _cutoff_iso = (_dt.now() - _td(hours=1)).isoformat(sep=' ')
        
        pending_old = 0
        for r in rows:
            # 1. age filter
            ts_raw = r.get("created_at")
            ts_is_old = True
            if ts_raw is not None:
                if isinstance(ts_raw, (int, float)):
                    ts_is_old = ts_raw < _cutoff_ts
                elif isinstance(ts_raw, str):
                    ts_is_old = ts_raw < _cutoff_iso
            if not ts_is_old:
                continue
            # 2. meta filter
            meta_raw = r.get("file_metadata")
            if not meta_raw:
                continue
            try:
                meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            except (ValueError, TypeError):
                continue
            if not isinstance(meta, dict):
                continue
            if str(meta.get("deleted", "")).lower() == "true":
                continue
            if meta.get("download_status") != "pending":
                continue
            pending_old += 1
        
        return float(pending_old)
    
    async def should_run(self) -> bool:
        """绝对触发: 只要有积压就跑 (不依赖增量)."""
        import time as _t
        if self.suspended_until > _t.time():
            return False
        try:
            current = await self.info_fn()
        except Exception:
            return False
        # 任意 pending ≥ threshold 就跑 (不依赖增量, 避免 watermark 卡死)
        return current >= self.threshold
    
    async def _do_work(self) -> None:
        # 多批次循环直到清完或处理上限 (避免 watermark 卡死)
        total_marked = 0
        for _batch_i in range(10):  # 最多 10 batch = 500 个
            n = await self._mark_one_batch(50)
            if n == 0:
                break
            total_marked += n
        
        if total_marked:
            dream_log.log(
                "dream.task.d27_heal_pending_backlog.cycle_done",
                f"marked {total_marked} stale pending file nodes as [已删除] "
                f"(>1h pending, likely NapCat file_id stale)",
            )
    
    async def _mark_one_batch(self, limit: int) -> int:
        from app.db.pool import pool
        import json as _json
        import time as _t
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        
        try:
            async with pool().acquire() as conn:
                # 2026-05-17 Round 14h: SQL 不用 ->> 和 NOW()-INTERVAL.
                # 拉 LIMIT * 5 倍 (Python 层会 filter, 没 SQL 预 filter 了)
                rows = await conn.fetch("""
                    SELECT id, archive_id, group_id, headline, content, file_metadata, created_at
                    FROM cold_nodes
                    WHERE scope = 'kb' AND node_type = 'file'
                    LIMIT $1
                """, limit * 5)
        except Exception as e:
            import traceback as _tb
            dream_log.warn(
                "dream.task.d27_heal_pending_backlog.query_failed",
                f"err={e!r}; tb_tail: {_tb.format_exc()[-200:]}",
            )
            return 0
        
        if not rows:
            return 0
        
        # Python 层 filter (≥ 1 hour old, pending, 非 deleted)
        _cutoff_ts = _t.time() - 3600
        _cutoff_iso = (_dt.now() - _td(hours=1)).isoformat(sep=' ')
        
        _eligible = []
        for r in rows:
            # age filter
            ts_raw = r.get("created_at")
            ts_is_old = True
            if ts_raw is not None:
                if isinstance(ts_raw, (int, float)):
                    ts_is_old = ts_raw < _cutoff_ts
                elif isinstance(ts_raw, str):
                    ts_is_old = ts_raw < _cutoff_iso
            if not ts_is_old:
                continue
            # meta filter
            meta_raw = r.get("file_metadata")
            if not meta_raw:
                continue
            try:
                _m = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            except (ValueError, TypeError):
                continue
            if not isinstance(_m, dict):
                continue
            if str(_m.get("deleted", "")).lower() == "true":
                continue
            if _m.get("download_status") != "pending":
                continue
            _eligible.append(r)
            if len(_eligible) >= limit:
                break
        rows = _eligible
        
        if not rows:
            return 0
        
        marked = 0
        now_ts = _t.time()
        _human_time = _dt.fromtimestamp(
            now_ts, tz=_tz(_td(hours=8))
        ).strftime("%Y-%m-%d %H:%M")
        
        for r in rows:
            headline = r["headline"] or ""
            content = r["content"] or ""
            meta = r["file_metadata"]
            if isinstance(meta, str):
                try:
                    meta = _json.loads(meta)
                except Exception:
                    meta = {}
            meta = dict(meta) if isinstance(meta, dict) else {}
            
            new_headline = (
                headline if headline.startswith("[已删除]")
                else f"[已删除] {headline[:25]}"
            )
            deleted_note = (
                f"[文件已删除, 索引保留供历史检索]\n"
                f"删除时间: {_human_time}\n"
                f"删除原因: pending backlog 老于 1 小时, NapCat file_id 大概率已失效\n"
                f"\n[原内容]\n"
            )
            new_content = (
                content if content.startswith("[文件已删除")
                else deleted_note + content
            )
            
            meta.update({
                "deleted": True,
                "deleted_at": now_ts,
                "deleted_reason": "pending_backlog_stale_1h",
                "deleted_original_path": meta.get("workspace_path", ""),
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
                         _json.dumps(meta, ensure_ascii=False),
                         r["id"])
                marked += 1
            except Exception as e:
                dream_log.warn(
                    "dream.task.d27_heal_pending_backlog.mark_failed",
                    f"id={r['id']} err={e!r}"[:200],
                )
                continue
        
        return marked
