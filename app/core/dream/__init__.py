"""
Dream — 后台空闲时的智能整理子系统。

设计原则 (来自 v9 设计):
1. 信息量驱动 (非时间驱动): 每任务跟踪自己的"信息水位", 增量超阈值才跑
2. 默认不打断: 学现有 bg_tasks.schedule() 模式, fire-and-forget
3. 复用现有: lock / LLM / checkpoint 用现有机制, 不重造轮子
4. 用户无感: 全部仅 debug.log, 无 UI / 无命令
5. 5 级响应:
   - Level 0 (日常): 不打断, 跟主线程并行
   - Level 1: 主线程拿 lock → dream 自然 lock 等
   - Level 2: 协作让出 (sleep)
   - Level 3 (紧急): 工作区 > 3.5GB → cancel + checkpoint
   - Level 4 (容错): 服务重启 → 从 checkpoint 恢复

子模块:
- event_bus: 信息量事件收集
- task_base: InfoDrivenTask + LongRunningDreamTask 基类
- supervisor: event-driven 主循环
- dream_log: 日志管控 (dream 专用, 默认不进控制台)
- cache: dream_cache (checkpoint, 仅 D 类长任务用)
- registry: 任务注册装饰器

实际任务模块 (Phase 1+2 实施):
- tasks/file_searchability/  ← D15-D18 (文件可寻性)
- tasks/kb_dag/               ← D21-D26 (KB DAG 整理)
- tasks/memory_maintenance/   ← D1-D6 (基础维护)
"""

from app.core.dream.dream_log import dream_log
from app.core.dream.event_bus import event_bus
from app.core.dream.supervisor import dream_supervisor, _can_dream
from app.core.dream.task_base import InfoDrivenTask, LongRunningDreamTask
from app.core.dream.registry import register_dream_task, REGISTERED_TASKS

# import tasks 子包以触发 @register_dream_task 装饰器执行
# 失败时不要破坏 dream package import (单任务失败仍能起 supervisor)
try:
    from app.core.dream import tasks as _tasks  # noqa: F401
except Exception as _e:
    import logging
    logging.getLogger(__name__).warning(
        "dream.tasks import failed: %r (supervisor will run but no tasks registered)",
        _e,
    )

__all__ = [
    "dream_log",
    "event_bus",
    "dream_supervisor",
    "_can_dream",
    "InfoDrivenTask",
    "LongRunningDreamTask",
    "register_dream_task",
    "REGISTERED_TASKS",
]
