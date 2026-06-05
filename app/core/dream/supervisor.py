"""
Dream Supervisor — 主循环

设计 (v9):
- Event-driven: 等 event_bus 唤醒, 不轮询时间
- Schedule 模式: 学 bg_tasks.schedule, fire-and-forget 启动任务
- Idle 判定: 主线程活跃时不启动新任务 (但已启动的继续跑)
- 紧急 cancel: 工作区 > 阈值 / 内存告急 时取消所有 dream

主循环:
    while running:
        await event_bus.wait_any(timeout=fallback)
        if not _can_dream():
            continue
        ready = [t for t in REGISTERED if t.should_run()]
        for t in ready:
            schedule(t.run())  # fire-and-forget
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from app.config import settings
from app.core.dream.dream_log import dream_log
from app.core.dream.event_bus import event_bus
from app.core.dream.registry import REGISTERED_TASKS


# 主线程活动时间戳 - orchestrator 入口更新
_last_main_activity: float = 0.0
_active_main_requests: int = 0
# 当前活跃 dream tasks (用于紧急 cancel)
_active_dream_tasks: set[asyncio.Task] = set()
# 当前活跃任务名集合 (按 task.name 防止同任务并发多 instance)
# 2026-05-16 fix: 实测短时间内多 wake → 同一 d15/d16 同时跑多份, 浪费 DB query
_active_task_names: set[str] = set()
# 2026-05-16: emergency 模式 flag. set 后 supervisor 只让 D4 跑, 暂停其他任务,
# 直到 max-agent size 降回 emergency 阈值以下.
# 修复实测 trace 22:59: emergency 一直触发, d17 反复重启被 cancel, 浪费 LLM 调用.
_emergency_active: bool = False
# 2026-05-17 Round 14f: D4 "catch-22" 防御
# 实测 (trace 08:28): 3670MB 大 agent 全是 user 上传 (synced_files protected).
# D4 找不到 candidates, watermark 永不降, emergency 永持续, 19 个 dream 永阻塞.
# 计数 D4 跑了几次 watermark 没动 → 3 次就放弃 emergency, 让其他 dream 跑.
_d4_failed_attempts: int = 0
_last_emergency_watermark: float = 0.0
# Supervisor 主任务句柄
_supervisor_task: Optional[asyncio.Task] = None
_shutdown_event = asyncio.Event()


def mark_main_activity() -> None:
    """orchestrator 入口调用. 标记主线程正在处理用户请求。"""
    global _last_main_activity
    _last_main_activity = time.time()


def mark_main_request_start() -> None:
    """Record a foreground request as active for dream backoff."""
    global _active_main_requests
    _active_main_requests += 1
    mark_main_activity()


def mark_main_request_done() -> None:
    """Record foreground request completion for dream backoff."""
    global _active_main_requests
    _active_main_requests = max(0, _active_main_requests - 1)
    mark_main_activity()


def _can_dream() -> bool:
    """判断是否可以启动新 dream 任务。

    条件:
    - dream_enabled
    - 主线程 idle >= dream_idle_threshold_sec
    - 服务未 shutdown
    """
    if not settings.dream_enabled:
        return False
    if _shutdown_event.is_set():
        return False
    if _active_main_requests > 0:
        return False
    if _last_main_activity == 0:
        # 服务刚启动, 还没有主线程活动 → 允许 dream
        return True
    idle_for = time.time() - _last_main_activity
    return idle_for >= settings.dream_idle_threshold_sec


async def _maybe_emergency_cancel() -> None:
    """Level 3: 工作区紧急时 cancel 所有 dream 任务。

    注: 工作区大小检查 需要 workspace 模块, 这里轻量调用。
    """
    # 延迟 import 避免循环
    try:
        from app.llm.tools.workspace import workspace_disk_usage
    except ImportError:
        return
    
    global _emergency_active  # 函数级 global, 后面 set/clear 都用这个
    
    # 取 main workspace
    from app.core.dream.cache import _get_workspace_root
    ws_root = _get_workspace_root()
    if not ws_root:
        return
    
    # 2026-05-16 修订: emergency 判定改用**最大单智能体**大小, 不是 ws_root 总量.
    # 原因: 大小限制是 per-agent 的 (每个 archive_id/group_id 一个智能体).
    # 一个智能体爆了就该 cancel + 让 D4 救场.
    try:
        from app.core.dream.tasks.memory_maintenance.d4_workspace_cleanup import (
            _max_agent_size_mb,
        )
        size_mb, big_archive, big_group = await _max_agent_size_mb(ws_root)
    except Exception:
        # fallback to ws_root 总量
        try:
            usage = workspace_disk_usage(ws_root)
            size_mb = usage.get("bytes", 0) / (1024 * 1024)
            big_archive, big_group = "", ""
        except Exception:
            return
    
    if size_mb > settings.dream_emergency_workspace_mb:
        # 2026-05-17 Round 14f: D4 失败计数 — 防"catch-22"
        # 实测 (trace 08:28): 大 agent (3670MB) 全是用户上传文件 (synced_files
        # protected). D4 找不到 candidates, 静默返 0. watermark 永远 3670MB.
        # emergency 永远持续, 其他 19 个 dream 永远阻塞. dream 子系统瘫痪.
        # 解决: D4 跑了 N 次 watermark 没降 → "无法清" 共识 → 解除 emergency,
        # 让其他 dream 跑 (D17 OCR / D21 merge / D20 cleanup 仍有意义).
        global _d4_failed_attempts, _last_emergency_watermark
        
        _was_in_emergency = _emergency_active
        
        # 2026-05-17 Round 14f 修订: D4 还在跑时不计 failed (公平 — 等它真跑完再说)
        # 之前 bug: supervisor cycle 比 D4 完成快, 每次 cycle 都加 1 → false-positive give_up.
        d4_currently_running = any(
            "d4_workspace_cleanup" in (t.get_name() if hasattr(t, "get_name") else "")
            for t in _active_dream_tasks
            if not t.done()
        )
        
        # 判定 D4 是否在"清不了"状态
        # 如果 watermark 跟上次 emergency 时几乎不变 (差 < 50MB), 且 D4 已跑过 (不在跑) →
        # 说明 D4 无效, 不再 cancel 其他任务.
        watermark_unchanged = (
            _was_in_emergency
            and _last_emergency_watermark > 0
            and abs(size_mb - _last_emergency_watermark) < 50
            and not d4_currently_running  # ← D4 跑完才计 failed
        )
        if watermark_unchanged:
            _d4_failed_attempts += 1
        _last_emergency_watermark = size_mb
        
        # 失败 3 次 → 放弃 emergency, 让其他任务跑 (但 D4 仍可周期跑)
        D4_GIVE_UP_THRESHOLD = 3
        if _d4_failed_attempts >= D4_GIVE_UP_THRESHOLD:
            if _emergency_active:
                _emergency_active = False
                dream_log.warn(
                    "dream.supervisor.emergency_give_up",
                    f"D4 failed to reduce watermark after {_d4_failed_attempts} "
                    f"attempts (stuck at {size_mb:.0f}MB > "
                    f"{settings.dream_emergency_workspace_mb}MB). "
                    f"Disabling emergency mode — other dream tasks may run. "
                    f"Probable cause: large agent has all-protected files (user uploads).",
                )
            return  # 不再 cancel/schedule, 走正常 dream cycle
        
        # 正常 emergency 流程
        _emergency_active = True
        
        if not _was_in_emergency:
            dream_log.warn(
                "dream.supervisor.emergency_enter",
                f"max_agent={size_mb:.0f}MB ({big_archive[:12]}/{big_group[:12]}) "
                f"> {settings.dream_emergency_workspace_mb}MB, "
                f"entering emergency mode (only D4 will run until ws drops). "
                f"Will give up after {D4_GIVE_UP_THRESHOLD} failed D4 attempts.",
            )
        
        # 紧急: cancel 所有活跃 dream, 但保留 D4 (它来救场)
        cancelled = 0
        d4_alive = False
        for task in list(_active_dream_tasks):
            if task.done():
                continue
            tname = task.get_name() if hasattr(task, "get_name") else ""
            if "d4_workspace_cleanup" in tname:
                d4_alive = True
                continue  # 别 cancel D4!
            task.cancel()
            cancelled += 1
        
        if cancelled:
            dream_log.warn(
                "dream.supervisor.emergency_cancel",
                f"max_agent={size_mb:.0f}MB ({big_archive[:12]}/{big_group[:12]}) "
                f"> {settings.dream_emergency_workspace_mb}MB, "
                f"cancelled {cancelled} dream tasks (D4 preserved)",
            )
        
        # D4 不在跑 → 主动启动
        if not d4_alive and "d4_workspace_cleanup" not in _active_task_names:
            from app.core.dream.registry import REGISTERED_TASKS
            d4_task = REGISTERED_TASKS.get("d4_workspace_cleanup")
            if d4_task and d4_task.suspended_until <= time.time():
                _schedule_dream_task(d4_task)
                dream_log.warn(
                    "dream.supervisor.emergency_scheduled_d4",
                    f"max_agent={size_mb:.0f}MB, forcing d4 to run "
                    f"(attempt #{_d4_failed_attempts + 1}/{D4_GIVE_UP_THRESHOLD})",
                )
    else:
        # ws 降回阈值下 → 退出 emergency 模式 + reset 失败计数
        if _emergency_active:
            _emergency_active = False
            dream_log.log(
                "dream.supervisor.emergency_cleared",
                f"max_agent={size_mb:.0f}MB back under "
                f"{settings.dream_emergency_workspace_mb}MB, resuming normal dream",
            )
        # reset 失败计数 (watermark 降回正常)
        _d4_failed_attempts = 0


def _schedule_dream_task(task) -> Optional[asyncio.Task]:
    """启动单 dream 任务 (fire-and-forget)。

    用现有 bg_tasks.schedule 模式. 不等待完成。
    
    2026-05-16: 
    - 同任务名只允许 1 个 instance. 若已在跑, 跳过 (下次 cycle 自然重试).
    - cancel cooldown: 刚被 cancel 的任务 1 秒内不重启 (防止 emergency_cancel 循环)
      实测 trace 22:00 (no_candidates → emergency_cancel → 立即重启 → 又 cancel),
      短 cooldown 让系统有机会运行 D4 真正清理.
    """
    import time as _t
    
    # 同名任务正在跑 → 跳过
    if task.name in _active_task_names:
        return None
    
    # 2026-05-16: emergency 模式下只允许 D4 运行, 其他任务暂停 (防 d17 反复重启循环)
    if _emergency_active and task.name != "d4_workspace_cleanup":
        return None
    
    # cancel cooldown: 1 秒内刚被 cancel 的不重启
    last_cancel = getattr(task, "last_cancelled_at", 0.0)
    if last_cancel > 0 and (_t.time() - last_cancel) < 1.0:
        return None
    
    try:
        from app.core.bg_tasks import schedule
        coro = task.run()
        async_task = schedule(coro, name=f"dream_{task.name}")
        _active_dream_tasks.add(async_task)
        _active_task_names.add(task.name)
        
        task_name = task.name
        def _on_done(t: asyncio.Task) -> None:
            _active_dream_tasks.discard(t)
            _active_task_names.discard(task_name)
        async_task.add_done_callback(_on_done)
        return async_task
    except Exception as e:
        dream_log.error(
            "dream.supervisor.schedule_failed",
            f"task={task.name} err={e!r}",
        )
        return None


_TASK_CATEGORY_ORDER = ("file", "kb", "memory", "maintenance", "other")
_KB_MAINTENANCE_TASKS = {
    "d21_merge", "d22_split", "d23_abstract", "d24_refine", "d25_edges",
    "d26_salience_decay", "d8_kb_ref_cleanup",
}
_TASK_CATEGORY_PREFIXES = {
    "file": ("d15_", "d16_", "d17_", "d18_", "d19_", "d20_"),
    "kb": ("d21_", "d22_", "d23_", "d24_", "d25_", "d26_", "d8_"),
    "memory": ("d1_", "d2_", "d3_", "d27_"),
    "maintenance": ("d4_", "d5_", "d6_"),
}


def _task_category(task) -> str:
    name = getattr(task, "name", "")
    for category, prefixes in _TASK_CATEGORY_PREFIXES.items():
        if name.startswith(prefixes):
            return category
    return "other"


def _limit_ready_tasks_for_cycle(ready_tasks: list) -> tuple[list, list]:
    """Select ready tasks by resource conflicts, not by LLM count.

    File analysis is the front of the pipeline and should activate immediately
    after uploads. KB maintenance follows, but graph-maintenance tasks are
    selected one per cycle because they mutate shared KB/edge state.
    """
    buckets = {k: [] for k in _TASK_CATEGORY_ORDER}
    for task in ready_tasks:
        buckets.setdefault(_task_category(task), []).append(task)

    selected = []
    deferred = []
    kb_maintenance_selected = False

    def try_take(task) -> bool:
        nonlocal kb_maintenance_selected
        if getattr(task, "name", "") in _KB_MAINTENANCE_TASKS:
            if kb_maintenance_selected:
                return False
            kb_maintenance_selected = True
        selected.append(task)
        return True

    for category in _TASK_CATEGORY_ORDER:
        bucket = buckets.get(category) or []
        while bucket:
            task = bucket.pop(0)
            if try_take(task):
                continue
            else:
                deferred.append(task)

    for category in _TASK_CATEGORY_ORDER:
        deferred.extend(buckets.get(category) or [])
    for category, bucket in buckets.items():
        if category not in _TASK_CATEGORY_ORDER:
            deferred.extend(bucket)

    return selected, deferred


async def _dream_cycle() -> None:
    """一次 dream cycle: 查所有任务, 启动 ready 的。"""
    if not _can_dream():
        return
    
    # 检查紧急情况
    await _maybe_emergency_cancel()
    
    # 2026-05-16 Round 14e: 并行 should_run (20 task 串行 await 慢)
    # 旧版串行 await 每个 task.should_run, 每个 info_fn 可能 DB 查询.
    # 20 个 task 串行 ≈ 几百 ms - 几秒. asyncio.gather 并行后 ≈ 单 task 时间.
    async def _check_ready(task):
        try:
            if await task.should_run():
                return task
            return None
        except Exception as e:
            dream_log.error(
                f"dream.task.{task.name}.should_run_failed",
                repr(e)[:200],
            )
            return None
    
    all_tasks = list(REGISTERED_TASKS.values())
    check_results = await asyncio.gather(
        *(_check_ready(t) for t in all_tasks),
        return_exceptions=False,
    )
    ready_tasks = [t for t in check_results if t is not None]
    
    if not ready_tasks:
        return
    
    # 按紧迫度排序 (信息量超阈值越多越优先) — 同样并行
    try:
        urgency_values = await asyncio.gather(
            *(t.urgency() for t in ready_tasks),
            return_exceptions=True,
        )
        urgencies = {
            t.name: (u if not isinstance(u, BaseException) else 0)
            for t, u in zip(ready_tasks, urgency_values)
        }
        ready_tasks.sort(key=lambda t: -urgencies.get(t.name, 0))
    except Exception:
        pass

    selected_tasks, deferred_tasks = _limit_ready_tasks_for_cycle(ready_tasks)
    
    # Fire-and-forget 启动 (不等完成)
    # 2026-05-16: 显式标注 emergency 阻塞 (之前 wake 列出但 _schedule 静默跳过 → log 不可见)
    # 2026-05-17 Round 14m: 不再截断 [:5] — 看清完整 ready/blocked 列表
    if _emergency_active:
        blocked = sorted(t.name for t in selected_tasks if t.name != "d4_workspace_cleanup")
        dream_log.log(
            "dream.supervisor.wake",
            f"starting 1 task (emergency mode, only d4); "
            f"blocked by emergency ({len(blocked)}): {blocked}",
        )
    else:
        names = sorted(t.name for t in selected_tasks)
        suffix = ""
        if deferred_tasks:
            suffix = f"; deferred {len(deferred_tasks)}: {sorted(t.name for t in deferred_tasks)}"
        dream_log.log(
            "dream.supervisor.wake",
            f"starting {len(selected_tasks)} tasks: {names}{suffix}",
        )
    
    for task in selected_tasks:
        _schedule_dream_task(task)


async def dream_supervisor() -> None:
    """主循环. 由 main.py startup 启动。

    无限循环, 直到 _shutdown_event 设置。
    """
    if not settings.dream_enabled:
        dream_log.log("dream.supervisor.disabled", "dream_enabled=False")
        return
    
    dream_log.log("dream.supervisor.start", f"loaded {len(REGISTERED_TASKS)} tasks")
    
    # Fallback timeout - 即使没 event 也定期 check (但不频繁)
    # 防止 event_bus 未来某种 bug 导致永远 sleep
    FALLBACK_TIMEOUT = 60.0
    
    try:
        while not _shutdown_event.is_set():
            try:
                got_event = await event_bus.wait_any(timeout=FALLBACK_TIMEOUT)
                # got_event=True: 有事件; False: timeout fallback
                # 两种情况都 run 一次 cycle
                await _dream_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                dream_log.error("dream.supervisor.cycle_failed", repr(e)[:200])
                # 不退出, 继续下一 cycle
                await asyncio.sleep(5)  # 防 error loop
    finally:
        dream_log.log(
            "dream.supervisor.shutdown",
            f"shutting down with {len(_active_dream_tasks)} active dream tasks",
        )


async def shutdown_dream() -> None:
    """优雅关闭. 等当前 dream 任务完成 (有超时)。"""
    _shutdown_event.set()
    # 通知 supervisor 退出循环
    event_bus._event.set()  # type: ignore[attr-defined]
    
    if _supervisor_task and not _supervisor_task.done():
        try:
            await asyncio.wait_for(_supervisor_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _supervisor_task.cancel()
    
    # 取消所有活跃 dream
    for task in list(_active_dream_tasks):
        if not task.done():
            task.cancel()
    
    if _active_dream_tasks:
        try:
            await asyncio.wait(_active_dream_tasks, timeout=2.0)
        except Exception:
            pass


def start_supervisor() -> asyncio.Task:
    """启动 supervisor 主循环. 返回 task 句柄。"""
    global _supervisor_task
    if _supervisor_task and not _supervisor_task.done():
        return _supervisor_task
    
    from app.core.bg_tasks import schedule
    _supervisor_task = schedule(dream_supervisor(), name="dream_supervisor")
    return _supervisor_task


def supervisor_stats() -> dict:
    """诊断接口 (供 /v1/observe/dream 用)。"""
    from app.core.dream.registry import task_stats
    return {
        "enabled": settings.dream_enabled,
        "shutdown": _shutdown_event.is_set(),
        "last_main_activity": _last_main_activity,
        "active_main_requests": _active_main_requests,
        "active_dream_tasks": len(_active_dream_tasks),
        "active_task_names": [t.get_name() for t in _active_dream_tasks if not t.done()],
        "registered_tasks": len(REGISTERED_TASKS),
        "tasks": task_stats(),
        "event_bus_stats": event_bus.stats(),
    }
