"""
Dream 日志管控 (dream_log)

设计:
- dream 任务可能频繁触发 (信息量驱动), 不能污染控制台
- 所有 dream.* 类别**默认不进控制台** (除非 dream_log_level="verbose")
- 文件日志总是有 (用于审计)
- 仅 dream.*.error / dream.*.warn 会自动控制台显示 (沿用 debug.error/warn)
- 2026-05-17 Round 14n: dream 是重要后台机制, 真有成果时 (cycle_done /
  emergency 解除 / 一波 wake) 直接进控制台高亮, 用户能看到工作进度.

日志类别约定:
  dream.supervisor.{wake,sleep,trigger,skip}    ← 主循环
  dream.event.{emit,batch}                       ← event_bus
  dream.task.{name}.{start,step,done,error}      ← 任务生命周期
  dream.task.{name}.cancelled                    ← Level 3+ 取消
  dream.lock.{acquire,wait,release}              ← lock 协调
  dream.cache.{save,load,clean}                  ← checkpoint

控制级别 (settings.dream_log_level):
  "minimal" (默认): 只写文件 + 重要错误进控制台 + 重要成功事件进控制台
  "normal":         dream.task.*.{start,done} 也进控制台
  "verbose":        全部 dream.* 进控制台 (开发调试)
"""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.core import debug


# Dream 类别中"重要"事件 — normal 级别会进控制台
_NORMAL_LEVEL_CATEGORIES = {
    "dream.supervisor.wake",
    "dream.supervisor.shutdown",
}

# Dream 类别前缀模式 — normal 级别也会进控制台
_NORMAL_LEVEL_PREFIXES = [
    "dream.task.",  # task.X.start / task.X.done
]


# 2026-05-17 Round 14n: 重要成功事件 — 永远进控制台 (即使 minimal level)
# 让用户能看到 dream 在工作 — 它是核心后台机制, 不能完全静默.
# 选择: 真有成果 / 状态切换 / 启动一波. 频繁事件不进 (避免噪).
_ALWAYS_CONSOLE_CATEGORIES = {
    "dream.supervisor.start",                  # 启动时一次
    "dream.supervisor.shutdown",               # 关闭时一次
    "dream.supervisor.wake",                   # 一波 task 启动
    "dream.supervisor.emergency_cleared",      # emergency 解除
    "dream.supervisor.emergency_give_up",      # emergency 3 次失败放弃
    # emergency_enter 用 warn 已显示, 这里不重复
}

# 重要事件后缀 — 真有成果时显示
# 2026-05-17 Round 14n: 只 cycle_done — 真正"做了事情"的事件.
# 不含 info_stats (info_fn 每次 should_run 都调, 太频繁会刷屏)
# 不含 done (无成果的 done 也会打, 噪)
# 不含 start (启动信息走 wake 一行就够)
_ALWAYS_CONSOLE_SUFFIXES = (
    ".cycle_done",      # 任务真清/合/标记了东西
)


def _is_dream_category(category: str) -> bool:
    return category.startswith("dream.")


def _is_important_success(category: str) -> bool:
    """重要成功事件 — minimal 级别也进控制台."""
    if category in _ALWAYS_CONSOLE_CATEGORIES:
        return True
    if category.endswith(_ALWAYS_CONSOLE_SUFFIXES):
        return True
    return False


def _should_emit_to_console(category: str) -> bool:
    """根据 dream_log_level 决定是否输出到控制台。

    minimal: 否 (所有 dream.* 都仅写文件)
    normal:  仅 dream.task.*.{start,done,error} 等关键事件
    verbose: 全部 dream.*
    """
    level = settings.dream_log_level.lower()
    if level == "verbose":
        return True
    if level == "normal":
        if category in _NORMAL_LEVEL_CATEGORIES:
            return True
        if any(category.startswith(pfx) and category.endswith((".start", ".done", ".error"))
               for pfx in _NORMAL_LEVEL_PREFIXES):
            return True
        return False
    # minimal: 默认全部静默 (但 error/warn 走 debug.error/warn 仍会显示)
    return False


class DreamLog:
    """Dream 日志门面 - 默认静默, 仅写文件。

    用法:
        from app.core.dream import dream_log
        dream_log.log("dream.task.d23.start", "begin abstraction", payload)
        dream_log.error("dream.task.d23.error", "LLM call failed")
        dream_log.warn("dream.lock.timeout", "lock wait > 30s")
    """

    def log(self, category: str, msg: str = "", payload: Any | None = None) -> None:
        """记录 dream 事件 (默认仅写文件, 重要成功事件直接显示)。"""
        if not settings.debug_mode:
            return
        if not _is_dream_category(category):
            # 安全检查: dream_log 不应被用于非 dream 类别
            category = f"dream.{category}"

        # 2026-05-17 Round 14n: 重要成功事件直接进控制台 (debug.status 高亮显示).
        # 不依赖 dream_log_level — 即使 minimal 也显示, 因为 dream 是核心后台机制.
        if _is_important_success(category):
            # 用 status 显示 (cyan 高亮), 同时写文件
            debug.status(f"[{category}] {msg}")
            return

        # 一般事件: 按 dream_log_level
        if _should_emit_to_console(category):
            if settings.dream_log_level == "verbose":
                debug.status(f"[{category}] {msg}")
            # normal: 通过 debug.log buffer
            debug.log(category, msg, payload)
        else:
            # minimal: 仅写文件, 不进控制台 buffer
            debug.log(category, msg, payload)

    def error(self, category: str, msg: str) -> None:
        """记录错误 - 控制台 + 文件 (重要, 不静默)。"""
        if not settings.debug_mode:
            return
        if not _is_dream_category(category):
            category = f"dream.{category}"
        debug.error(f"[{category}] {msg}")

    def warn(self, category: str, msg: str) -> None:
        """记录警告 - 控制台 + 文件 (重要, 不静默)。"""
        if not settings.debug_mode:
            return
        if not _is_dream_category(category):
            category = f"dream.{category}"
        debug.warn(f"[{category}] {msg}")

    def section(self, msg: str) -> None:
        """大节标记 (仅 verbose 模式显示)。"""
        if not settings.debug_mode:
            return
        if settings.dream_log_level == "verbose":
            debug.section(f"DREAM: {msg}")
        else:
            debug.log("dream.section", msg)


# 单例
dream_log = DreamLog()
