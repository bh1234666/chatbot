"""
Dream Task Registry — 任务注册装饰器

设计:
- 模块 import 时自动注册到 REGISTERED_TASKS
- supervisor 启动时遍历所有注册任务
- 支持启用/禁用单任务

用法:
    from app.core.dream.registry import register_dream_task
    
    @register_dream_task
    class D23HighLevelAbstract(InfoDrivenTask):
        name = "d23_abstract"
        threshold = 10
        async def info_fn(self): ...
        async def _do_work(self): ...
"""
from __future__ import annotations

from typing import Type, TYPE_CHECKING

from app.core.dream.dream_log import dream_log

if TYPE_CHECKING:
    from app.core.dream.task_base import InfoDrivenTask


# 全局注册表 - name → task instance
REGISTERED_TASKS: dict[str, "InfoDrivenTask"] = {}


def register_dream_task(cls: Type["InfoDrivenTask"]) -> Type["InfoDrivenTask"]:
    """注册装饰器. cls 必须是 InfoDrivenTask 的子类。"""
    try:
        instance = cls()
    except Exception as e:
        dream_log.error(
            f"dream.registry.init_failed",
            f"cannot instantiate {cls.__name__}: {e!r}",
        )
        return cls
    
    if not instance.name:
        dream_log.error(
            f"dream.registry.no_name",
            f"{cls.__name__} 缺少 name 属性",
        )
        return cls
    
    if instance.name in REGISTERED_TASKS:
        dream_log.warn(
            f"dream.registry.duplicate",
            f"name '{instance.name}' already registered; overwriting",
        )
    
    REGISTERED_TASKS[instance.name] = instance
    dream_log.log(
        f"dream.registry.registered",
        f"task '{instance.name}' registered (threshold={instance.threshold})",
    )
    return cls


def disable_task(name: str) -> None:
    """禁用单个任务 (设置 suspended_until 极远)。"""
    if name in REGISTERED_TASKS:
        import time
        REGISTERED_TASKS[name].suspended_until = time.time() + 3600 * 24 * 365 * 100
        dream_log.log(f"dream.registry.disabled", name)


def enable_task(name: str) -> None:
    """启用单个任务 (清 suspended_until)。"""
    if name in REGISTERED_TASKS:
        REGISTERED_TASKS[name].suspended_until = 0
        REGISTERED_TASKS[name].consecutive_failures = 0
        dream_log.log(f"dream.registry.enabled", name)


def task_stats() -> list[dict]:
    """所有任务的诊断信息。"""
    return [t.stats() for t in REGISTERED_TASKS.values()]
