"""
基础维护任务 (D1-D6).

- D1 hot→warm 主动压缩 (升级现有 reactive)
- D2 warm→cold 主动压缩 (升级现有 reactive)
- D3 KB 占位清理 (调用现有 cleanup_stale_file_placeholders)
- D4 LLM 智能工作区清理 ⭐ (核心创新)
- D5 副产物清理 (规则: *.o, *.exe, __pycache__)
- D6 SQLite WAL checkpoint
"""

from app.core.dream.tasks.memory_maintenance import d4_workspace_cleanup  # noqa: F401
from app.core.dream.tasks.memory_maintenance import d1_d2_d3_compression  # noqa: F401
from app.core.dream.tasks.memory_maintenance import d5_d6_artifact_wal  # noqa: F401
