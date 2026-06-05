"""
Dream tasks 实施

按功能分组:
- file_searchability/   ← D15-D20 (文件可寻性)
- kb_dag/               ← D21-D26 (KB DAG 整理, 含 D23 高级抽象 ⭐)
- memory_maintenance/   ← D1-D6 (基础维护, 升级现有 reactive)
- deep_maintenance/     ← D7-D10 (深度维护)
- experimental/         ← D11/D12/D14 (慎做)

任意子模块 import 时, 该任务会自动通过 @register_dream_task 注册.
所以这里 import 一遍就激活所有任务.
"""

# Phase 1+2 任务一次性导入 (注册到 REGISTERED_TASKS)
# 按需启用: 通过 settings 或 disable_task() 关单个任务
from app.core.dream.tasks import kb_dag  # noqa: F401
from app.core.dream.tasks import file_searchability  # noqa: F401
from app.core.dream.tasks import memory_maintenance  # noqa: F401
