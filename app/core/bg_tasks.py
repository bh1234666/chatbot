"""
Fire-and-forget task scheduler with strong reference tracking.

Python 官方文档明确警告: event loop 对 task 只持有弱引用。
若没有强引用保存 asyncio.create_task 的返回值, task 可能在
mid-execution 被 GC 回收。本模块提供 schedule() 统一入口,
全局 set 持引用, task 完成时自动移除。
"""

import asyncio
from typing import Coroutine, Any

_BG_TASKS: set[asyncio.Task] = set()


def schedule(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
    """Fire-and-forget but kept alive until completion."""
    task = asyncio.create_task(coro, name=name)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


def stats() -> dict:
    """Expose to /v1/observe/bg_tasks for monitoring."""
    return {
        "alive": len(_BG_TASKS),
        "by_name": sorted(t.get_name() for t in _BG_TASKS if t.get_name()),
    }


async def cancel_all(*, timeout: float = 1.0) -> int:
    """Cancel and drain scheduled background tasks.

    Used by tests and graceful shutdown paths to avoid pending task warnings while
    preserving fire-and-forget behavior during normal request handling.
    """
    tasks = [t for t in list(_BG_TASKS) if not t.done()]
    if not tasks:
        _BG_TASKS.difference_update(t for t in list(_BG_TASKS) if t.done())
        return 0
    for task in tasks:
        task.cancel()
    try:
        await asyncio.wait(tasks, timeout=timeout)
    finally:
        _BG_TASKS.difference_update(tasks)
        _BG_TASKS.difference_update(t for t in list(_BG_TASKS) if t.done())
    return len(tasks)
