"""
Dream Event Bus — 信息量事件收集

设计:
- 现有"信息产生点" emit 事件 (hot 写入 / 文件上传 / KB 节点新增 / 工作区写入)
- Supervisor 监听 event, 醒来后查任务的"信息水位"决定跑谁
- 极轻量: 仅 emit + 唤醒, 不复杂事件路由

使用:
    from app.core.dream import event_bus
    
    # 信息产生点 emit
    await event_bus.emit("hot_turn_added", archive_id="a1", user_id="u1")
    
    # Supervisor 监听
    await event_bus.wait_any()  # 阻塞直到任意事件
    stats = event_bus.recent_stats()  # 取最近事件统计
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Any

from app.core.dream.dream_log import dream_log


class DreamEventBus:
    """轻量 event 收集器 - 仅唤醒 supervisor, 不复杂路由。"""

    def __init__(self):
        self._event = asyncio.Event()
        # 最近事件计数 (per type) - 用 deque 防内存膨胀
        self._recent: dict[str, deque] = defaultdict(lambda: deque(maxlen=200))
        # 累计事件统计 (诊断用)
        self._total_count: dict[str, int] = defaultdict(int)
        # 最近 emit 时间 (诊断用)
        self._last_emit_at: float = 0.0

    async def emit(self, event_type: str, **payload: Any) -> None:
        """信息产生时调用. 不复杂事件传递, 仅唤醒 + 计数。

        不抛异常 (event_bus 失败不应破坏主流程)。
        """
        try:
            now = time.time()
            self._recent[event_type].append((now, payload))
            self._total_count[event_type] += 1
            self._last_emit_at = now
            # 唤醒所有等 wait_any 的任务
            self._event.set()
        except Exception as e:
            # event_bus 失败不影响主流程
            dream_log.error("dream.event.emit_failed", f"{event_type}: {e!r}")

    async def wait_any(self, timeout: float | None = None) -> bool:
        """阻塞直到任意事件. 返回 True (事件来) 或 False (timeout)。"""
        try:
            if timeout is not None:
                await asyncio.wait_for(self._event.wait(), timeout=timeout)
            else:
                await self._event.wait()
            self._event.clear()
            return True
        except asyncio.TimeoutError:
            return False

    def event_count_since(self, event_type: str, since_ts: float) -> int:
        """查询某事件自某时间起的发生次数 (供 InfoDrivenTask 用)。"""
        if event_type not in self._recent:
            return 0
        return sum(1 for ts, _ in self._recent[event_type] if ts > since_ts)

    def total_count(self, event_type: str) -> int:
        """累计事件数 (信息水位)。"""
        return self._total_count.get(event_type, 0)

    def recent_payloads(self, event_type: str, since_ts: float = 0.0) -> list[dict]:
        """取某事件的 payload 列表 (供任务消费)。"""
        if event_type not in self._recent:
            return []
        return [p for ts, p in self._recent[event_type] if ts > since_ts]

    def stats(self) -> dict[str, Any]:
        """诊断: 各事件总数 + 最近发生时间。"""
        return {
            "total_counts": dict(self._total_count),
            "recent_buffer_sizes": {k: len(v) for k, v in self._recent.items()},
            "last_emit_at": self._last_emit_at,
            "last_emit_ago_sec": (
                time.time() - self._last_emit_at if self._last_emit_at else None
            ),
        }


# 单例
event_bus = DreamEventBus()
