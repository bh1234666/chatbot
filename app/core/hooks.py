# 2026-05-11 P5.2: Hooks 生命周期系统轻量版
#
# 参照 Claude Code hooks.mdx + hooks.ts 设计,但精简到 5 个核心事件。
# 不读 settings.json 配置,仅支持 in-process 注册(可后续扩展持久化)。
#
# 设计原则:
#   - **不影响现有行为**: 默认无 hook 注册时,所有 dispatch 是 no-op
#   - **异常隔离**: 单个 hook 抛错不影响主流程,只 warn 日志
#   - **轻量**: 没有 Zod schema 验证,没有 async timeout,没有外部 IPC
#   - **可观测**: 提供 register API 给现有 lifecycle 钩子(如 persona_guard)显式化
#
# 5 个核心事件:
#   - session_start    : 主线程 round2 chat_with_tools_loop 开始
#   - subagent_start   : helper 启动 (delegate spawn)
#   - subagent_stop    : helper 结束 (任何 terminal_reason)
#   - pre_tool_use     : 工具调用前 (主线程或 helper)
#   - post_tool_failure: 工具失败时 (返回 ok=False 或 抛错)
#
# 使用例:
#   from app.core.hooks import register_hook, HookEvent
#
#   def my_logger(event, payload):
#       print(f"[hook] {event}: {payload.get('task_id', '?')}")
#
#   register_hook(HookEvent.SUBAGENT_START, my_logger)
#   register_hook(HookEvent.SUBAGENT_STOP, my_logger)
#
# 内部触发(底层代码已加):
#   dispatch_hook(HookEvent.SUBAGENT_START, {"task_id": "...", "kind": "code", ...})
#   dispatch_hook(HookEvent.SUBAGENT_STOP,  {"task_id": "...", "terminal_reason": "completed", ...})

from __future__ import annotations

import logging
import asyncio
from enum import Enum
from typing import Callable, Any

log = logging.getLogger(__name__)


class HookEvent(str, Enum):
    """支持的生命周期事件枚举。"""
    SESSION_START = "session_start"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_STOP = "subagent_stop"
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_FAILURE = "post_tool_failure"


# Hook callback signature:
#   sync:  (event: str, payload: dict) -> None
#   async: async (event: str, payload: dict) -> None
# 不强制 typing,只用 dict 传递任意上下文,保持灵活。
HookCallback = Callable[[str, dict], Any]

# 注册表: {event_name: [callback, ...]}
_hooks: dict[str, list[HookCallback]] = {}


def register_hook(event: HookEvent | str, callback: HookCallback) -> None:
    """注册一个 hook callback。

    重复注册同一 callback 会被记录两次(各触发一次),如需去重外部自管。
    """
    key = event.value if isinstance(event, HookEvent) else str(event)
    if key not in _hooks:
        _hooks[key] = []
    _hooks[key].append(callback)
    log.info("hook registered: event=%s callback=%s (total %d)",
             key, getattr(callback, "__name__", repr(callback)), len(_hooks[key]))


def unregister_hook(event: HookEvent | str, callback: HookCallback) -> bool:
    """移除一个 hook。返回是否真的移除了。"""
    key = event.value if isinstance(event, HookEvent) else str(event)
    try:
        _hooks.get(key, []).remove(callback)
        return True
    except ValueError:
        return False


def list_hooks(event: HookEvent | str | None = None) -> dict[str, int]:
    """看当前注册了哪些 hook。用于 debug/调试。

    Returns: {event_name: callback_count}
    """
    if event is None:
        return {k: len(v) for k, v in _hooks.items()}
    key = event.value if isinstance(event, HookEvent) else str(event)
    return {key: len(_hooks.get(key, []))}


def dispatch_hook(event: HookEvent | str, payload: dict) -> None:
    """触发某事件的所有 hook (同步上下文)。

    设计:
    - **异常隔离**: 单 hook 抛错 only 记 warn,不影响主流程。
    - **顺序**: 按注册顺序执行。
    - **同步**: 同步 callback 直接调,async callback 用 ensure_future 后台跑。
      不阻塞主流程 — hook 不应该让主流程慢下来。
    """
    key = event.value if isinstance(event, HookEvent) else str(event)
    callbacks = _hooks.get(key, [])
    if not callbacks:
        return

    for cb in callbacks:
        try:
            res = cb(key, payload)
            # 如果 callback 是 async 函数, res 是 coroutine, 后台跑掉
            if asyncio.iscoroutine(res):
                try:
                    from app.core.bg_tasks import schedule

                    schedule(res, name=f"hook:{key}")
                except RuntimeError:
                    # 没有 event loop (单元测试场景) — 同步运行
                    asyncio.run(res)
        except Exception as e:
            log.warning("hook callback failed: event=%s cb=%s error=%r",
                        key, getattr(cb, "__name__", repr(cb)), e)


async def dispatch_hook_async(event: HookEvent | str, payload: dict) -> None:
    """async 版本 dispatch — await 每个 async hook 完成。

    用于需要等 hook 完成的场景(罕见,通常 dispatch_hook 已足够)。
    """
    key = event.value if isinstance(event, HookEvent) else str(event)
    callbacks = _hooks.get(key, [])
    for cb in callbacks:
        try:
            res = cb(key, payload)
            if asyncio.iscoroutine(res):
                await res
        except Exception as e:
            log.warning("hook callback failed (async): event=%s cb=%s error=%r",
                        key, getattr(cb, "__name__", repr(cb)), e)


def clear_all_hooks() -> None:
    """清空所有 hook 注册。仅用于测试。"""
    _hooks.clear()


# ─────────────────────────────────────────────────────────────────
# 内置 hook 示例: 默认装一个 debug logger(开发环境用)
# 生产环境不该有副作用 hook;这个示例放在注释里,需要时打开。
# ─────────────────────────────────────────────────────────────────
#
# def _debug_logger(event: str, payload: dict) -> None:
#     log.info("[lifecycle] %s payload_keys=%s",
#              event, sorted(payload.keys()))
#
# # register_hook(HookEvent.SUBAGENT_START, _debug_logger)
# # register_hook(HookEvent.SUBAGENT_STOP, _debug_logger)
