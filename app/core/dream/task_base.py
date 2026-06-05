"""
Dream Task 基类

InfoDrivenTask: 信息量驱动的基础任务 (A 类: 不打断, 默认大多数任务用)
LongRunningDreamTask: 带 checkpoint 的长任务 (D 类: D15-D18 大文件处理)

设计要点:
- 信息水位 (watermark): 每任务跟踪自己的"上次跑时信息量"
- 增量 ≥ threshold 才跑 (should_run)
- 默认不响应 CancelledError 特殊处理 (除非 Level 3+)
- 失败计数: 3 次失败暂停 1 小时, 10 次完全禁用
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Awaitable

from app.config import settings
from app.core.dream.dream_log import dream_log


class InfoDrivenTask(ABC):
    """信息量驱动任务基类。

    子类需实现:
        name: 任务名 (dream.task.{name})
        threshold: 信息量增量阈值
        info_fn: 返回当前信息量 (float/int)
        _do_work: 实际任务逻辑

    可选覆盖:
        max_duration_sec: 单次跑最长 (默认用 settings.dream_task_timeout_sec)
        uses_llm: 是否调 LLM (用于预算检查)
    """

    # 子类必须设置
    name: str = ""
    threshold: float = 1.0
    uses_llm: bool = False
    startup_sweep: bool = False

    def __init__(self):
        if not self.name:
            raise ValueError(f"{type(self).__name__}: name 必须设置")
        # 信息水位 - 上次成功跑时的信息量
        self.last_run_info: float = 0.0
        # 失败计数
        self.consecutive_failures: int = 0
        self.last_failure_at: float = 0.0
        # 暂停状态
        self.suspended_until: float = 0.0
        # 被打断次数 (Level 3+ 才计数, 不是 Level 0)
        self.interrupted_count: int = 0
        # 2026-05-16 新增: 上次被 cancel 的时间 (用于 supervisor cancel cooldown)
        self.last_cancelled_at: float = 0.0
        # 2026-05-16 新增: demoted 状态 - cancel ≥N 次后, LLM 调用强制走 lite
        # 子类可在 _do_work 里 LLM 调用时通过 self.demoted 判断
        self.demoted: bool = False
        # 最后一次成功跑时间 (诊断用)
        self.last_success_at: float = 0.0
        self.total_runs: int = 0
        # 2026-05-16 Round 14 加: 进程启动后是否已经强制 sweep 过.
        # 17 个 dream task 用 event_bus.total_count 做信息量, 重启归零
        # → should_run 永远 False → dream 重启后**完全不工作**直到本进程跑够阈值事件.
        # 启动后 5 分钟内, should_run 第一次返 True (强制 sweep), 之后正常逻辑.
        # 实测 trace 23:39 启动后 KB 仍有 138 stale placeholder, 但 D3 没跑.
        self._startup_sweep_done: bool = False

    @abstractmethod
    async def info_fn(self) -> float:
        """返回当前信息量 (cumulative)。"""
        raise NotImplementedError

    @abstractmethod
    async def _do_work(self) -> None:
        """实际任务逻辑。可抛异常, 框架会捕获并计入失败。"""
        raise NotImplementedError

    async def should_run(self) -> bool:
        """判断是否该跑。"""
        # 检查暂停状态
        if self.suspended_until > time.time():
            return False
        # 2026-05-16 Round 14: 启动 sweep — event_bus 重启归零, 强制第一次跑.
        # 2026-05-17 Round 14m bug 修: 之前在 should_run 内 set sweep_done=True,
        # 但 emergency 时 _schedule_dream_task 跳过非 D4 → sweep_done 已置 True 但 task
        # 没真跑 → 后续 cycle 走 event_bus delta=0 → 永远 wake 不到 → dream 永不工作.
        # 现在: should_run 返 True 即可, **不在这里 set sweep_done**. 让 run() 真完成时 set.
        if not self._startup_sweep_done and self.startup_sweep:
            return True
        if not self._startup_sweep_done:
            self._startup_sweep_done = True
        # 检查信息量增量
        try:
            current = await self.info_fn()
        except Exception as e:
            dream_log.error(f"dream.task.{self.name}.info_fn_failed", repr(e))
            return False
        delta = current - self.last_run_info
        return delta >= self.threshold

    async def urgency(self) -> float:
        """优先级 - 信息量超阈值的倍数 (供 supervisor 排序)。"""
        try:
            current = await self.info_fn()
            delta = max(0, current - self.last_run_info)
            return delta / max(self.threshold, 1e-6)
        except Exception:
            return 0.0

    async def run(self) -> None:
        """对外接口。框架调用, 不要直接 override。

        - 不捕获 CancelledError (让上层处理)
        - 其他异常计入失败
        - 成功后更新 watermark
        """
        start = time.time()
        dream_log.log(f"dream.task.{self.name}.start", f"info_threshold={self.threshold}")
        try:
            # 限制总时长
            timeout = settings.dream_task_timeout_sec
            await asyncio.wait_for(self._do_work(), timeout=timeout)
            # 成功 → 更新 watermark
            self.last_run_info = await self.info_fn()
            self.consecutive_failures = 0  # 重置失败计数
            self.last_success_at = time.time()
            self.total_runs += 1
            # 2026-05-17 Round 14m: run 真完成才 set sweep_done (防 emergency 跳过导致永不 wake)
            self._startup_sweep_done = True
            elapsed = time.time() - start
            dream_log.log(
                f"dream.task.{self.name}.done",
                f"elapsed={elapsed:.1f}s watermark={self.last_run_info}",
            )
        except asyncio.CancelledError:
            # Level 3+ 取消, 不更新 watermark, 下次重试
            self.interrupted_count += 1
            self.last_cancelled_at = time.time()
            
            # 2026-05-16 修复: cancel 累计达阈值时降级/暂停
            try:
                from app.config import settings as _s
                demote_at = getattr(_s, "dream_interrupt_demote_threshold", 5)
                suspend_at = demote_at * 3  # demote 3 倍后 suspend 5 min
            except Exception:
                demote_at, suspend_at = 5, 15
            
            if self.interrupted_count >= demote_at and not self.demoted:
                self.demoted = True
                dream_log.warn(
                    f"dream.task.{self.name}.demoted",
                    f"interrupted_count={self.interrupted_count}, "
                    f"forcing lite LLM for future calls",
                )
            
            if self.interrupted_count >= suspend_at:
                # 持续被 cancel → 暂停 5 min, 让系统先解决资源压力
                self.suspended_until = time.time() + 300
                dream_log.warn(
                    f"dream.task.{self.name}.suspended_by_cancels",
                    f"interrupted {self.interrupted_count} times, "
                    f"suspending 5min to break loop",
                )
            
            dream_log.log(
                f"dream.task.{self.name}.cancelled",
                f"interrupted_count={self.interrupted_count}",
            )
            raise
        except asyncio.TimeoutError:
            self._record_failure("timeout")
            dream_log.warn(
                f"dream.task.{self.name}.timeout",
                f"task exceeded {settings.dream_task_timeout_sec}s",
            )
        except Exception as e:
            # 2026-05-17 Round 14l: 加 traceback 让 root cause 暴露
            # 之前几轮 D3 TypeError 我都靠猜 — grep + 改 SQL + 部署 + 仍抛 + 再猜.
            # 修过 5 处 SQL 都没修对真根因. 现在 dump traceback, 一次性看清.
            import traceback as _tb_dump
            tb_str = _tb_dump.format_exc()
            self._record_failure(repr(e))
            dream_log.error(
                f"dream.task.{self.name}.error",
                f"{e!r}; tb_tail: {tb_str[-600:]}"
            )

    def _record_failure(self, reason: str) -> None:
        self.consecutive_failures += 1
        self.last_failure_at = time.time()
        # 3 次失败 → 暂停 1 小时
        if self.consecutive_failures == 3:
            self.suspended_until = time.time() + 3600
            dream_log.warn(
                f"dream.task.{self.name}.suspended",
                f"3 failures, suspended for 1h. last_reason={reason[:100]}",
            )
        # 10 次失败 → 完全禁用 (suspend 100 年)
        if self.consecutive_failures >= 10:
            self.suspended_until = time.time() + 3600 * 24 * 365 * 100
            dream_log.error(
                f"dream.task.{self.name}.disabled",
                f"10 consecutive failures, disabled. last_reason={reason[:100]}",
            )

    def stats(self) -> dict[str, Any]:
        """诊断信息。"""
        return {
            "name": self.name,
            "threshold": self.threshold,
            "last_run_info": self.last_run_info,
            "consecutive_failures": self.consecutive_failures,
            "interrupted_count": self.interrupted_count,
            "total_runs": self.total_runs,
            "suspended": self.suspended_until > time.time(),
            "suspended_until": self.suspended_until,
            "last_success_at": self.last_success_at,
        }


class LongRunningDreamTask(InfoDrivenTask):
    """长跑任务基类 (D 类) - 含 checkpoint 机制。

    用于 D15-D18 这类大文件处理 (PDF 100 页 / 长视频转写等)。
    服务重启 / 超时 cancel 后, 下次启动从 checkpoint 恢复。

    子类需实现:
        steps_fn: 返回所有步骤定义 (allowing resumption)
        execute_step: 跑单步, 返回结果
        finalize: 所有步骤完成后的整合
    """

    @abstractmethod
    async def steps_fn(self, context: dict) -> list[dict]:
        """返回步骤列表. 每项需含 'name' 字段 (用作 checkpoint key)。"""
        raise NotImplementedError

    @abstractmethod
    async def execute_step(self, step: dict, context: dict) -> Any:
        """跑单步. 异常会触发该步重做 (下次)。"""
        raise NotImplementedError

    async def finalize(self, context: dict, all_results: dict[str, Any]) -> None:
        """所有步骤完成后整合 (默认 no-op)。"""
        pass

    async def _do_work(self) -> None:
        """框架: 调度 steps + checkpoint."""
        # 子类应该在 _do_work 中:
        # 1. 准备 context
        # 2. 从 dream_cache 加载已完成步骤
        # 3. 遍历 steps, 跑未完成的
        # 4. 调 finalize
        #
        # 这里给基础实现, 子类按需覆盖
        from app.core.dream.cache import dream_cache
        
        # 子类通过设置 self.context_id 决定 cache key
        context_id = getattr(self, "context_id", None)
        if not context_id:
            raise NotImplementedError(
                f"{self.name}: context_id 必须在 _do_work 前设置"
            )
        
        manifest = await dream_cache.load_manifest(context_id, self.name)
        completed = set(manifest.get("completed_steps", []))
        results = manifest.get("step_results", {})
        
        context = {"task_id": context_id}
        steps = await self.steps_fn(context)
        
        for step in steps:
            step_name = step["name"]
            if step_name in completed:
                continue
            
            try:
                # 单 step 超时保护
                result = await asyncio.wait_for(
                    self.execute_step(step, context),
                    timeout=settings.dream_step_timeout_sec,
                )
            except asyncio.TimeoutError:
                dream_log.warn(
                    f"dream.task.{self.name}.step_timeout",
                    f"step={step_name}",
                )
                raise  # 上层会重试
            except asyncio.CancelledError:
                raise  # 让 CancelledError 上传
            except Exception as e:
                dream_log.error(
                    f"dream.task.{self.name}.step_error",
                    f"step={step_name} err={e!r}",
                )
                raise
            
            results[step_name] = result
            completed.add(step_name)
            await dream_cache.save_step(context_id, self.name, step_name, result)
        
        # 全部完成 → finalize
        await self.finalize(context, results)
        await dream_cache.mark_complete(context_id, self.name)
