"""
D5: 副产物清理 (规则, 无 LLM)
D6: SQLite WAL checkpoint (无 LLM)

都是轻量任务. D5 模式匹配, D6 一个 PRAGMA.
"""
from __future__ import annotations

import asyncio
import os
import shutil

from app.core.dream.dream_log import dream_log
from app.core.dream.event_bus import event_bus
from app.core.dream.registry import register_dream_task
from app.core.dream.task_base import InfoDrivenTask


D5_THRESHOLD = 100  # workspace_grew 累计 100 (按 file 计) - 一般 100 个 artifact 才值得清
D6_THRESHOLD = 200  # hot_turn_added 累计 200 触发 WAL checkpoint

# 副产物模式
_ARTIFACT_EXTS = {".o", ".obj", ".exe", ".dll", ".so", ".pyc", ".class"}
_ARTIFACT_DIRS = {"__pycache__", ".pytest_cache", "node_modules"}
_ARTIFACT_FILES = {"core", "core.dump"}  # core dump
_PROTECTED_DIRS = {"_helpers_shared", "_shared", "commit_to_main"}  # 不进


def _is_artifact(name: str) -> bool:
    ext = os.path.splitext(name)[1].lower()
    if ext in _ARTIFACT_EXTS:
        return True
    if name in _ARTIFACT_FILES:
        return True
    return False


@register_dream_task
class D5ArtifactCleanup(InfoDrivenTask):
    """D5: 副产物清理 (*.o, *.exe, __pycache__ ...)."""
    
    name = "d5_artifact_cleanup"
    threshold = D5_THRESHOLD
    uses_llm = False
    
    async def info_fn(self) -> float:
        return float(event_bus.total_count("hot_turn_added"))
    
    async def _do_work(self) -> None:
        from app.core.dream.cache import _get_workspace_root
        ws_root = _get_workspace_root()
        if not ws_root or not os.path.isdir(ws_root):
            return
        
        # 异步包装的同步扫描
        freed = await asyncio.to_thread(self._scan_and_clean, ws_root)
        
        if freed:
            dream_log.log(
                "dream.task.d5_artifact_cleanup.cycle_done",
                f"freed {freed:.1f}MB",
            )
    
    def _scan_and_clean(self, root: str) -> float:
        """同步: 扫所有沙箱清副产物."""
        freed_bytes = 0
        try:
            for r, dirs, files in os.walk(root, topdown=True):
                # 排除保护目录 (commit_to_main / _helpers_shared 等不进)
                # 但 _delegate_xxx 沙箱内部的 __pycache__ 该清
                base = os.path.basename(r)
                if base in _PROTECTED_DIRS:
                    dirs.clear()  # 不进
                    continue
                
                # 删 __pycache__ 整目录
                pycache_dirs = [d for d in dirs if d in _ARTIFACT_DIRS]
                for pd in pycache_dirs:
                    pd_path = os.path.join(r, pd)
                    try:
                        # 算大小
                        for r2, _, fs2 in os.walk(pd_path):
                            for f in fs2:
                                try:
                                    freed_bytes += os.path.getsize(os.path.join(r2, f))
                                except OSError:
                                    pass
                        shutil.rmtree(pd_path, ignore_errors=True)
                        dirs.remove(pd)
                    except OSError:
                        pass
                
                # 删 artifact 文件
                for f in files:
                    if _is_artifact(f):
                        fp = os.path.join(r, f)
                        try:
                            sz = os.path.getsize(fp)
                            os.unlink(fp)
                            freed_bytes += sz
                        except OSError:
                            pass
        except Exception as e:
            dream_log.warn("dream.task.d5_artifact_cleanup.walk_failed", repr(e)[:200])
        
        return freed_bytes / (1024 * 1024)


@register_dream_task
class D6SqliteWalCheckpoint(InfoDrivenTask):
    """D6: SQLite WAL checkpoint (压实 wal 文件)."""
    
    name = "d6_sqlite_wal"
    threshold = D6_THRESHOLD
    uses_llm = False
    
    async def info_fn(self) -> float:
        return float(event_bus.total_count("hot_turn_added"))
    
    async def _do_work(self) -> None:
        # 现有可能用 SQLite 的地方: pause_state / 部分 cache
        # 主 DB 是 PostgreSQL, 但仍有 SQLite 辅助文件
        
        # 找 .db / .sqlite 文件
        from app.core.dream.cache import _get_workspace_root
        ws_root = _get_workspace_root()
        
        candidates = []
        # 常见 SQLite 文件位置
        for p in [".", ws_root or "."]:
            if not os.path.isdir(p):
                continue
            for r, _, fs in os.walk(p):
                for f in fs:
                    if f.endswith(('.db', '.sqlite', '.sqlite3')) and not f.endswith('-journal') and not f.endswith('-wal'):
                        candidates.append(os.path.join(r, f))
                # 仅扫第一层
                break
            # 仅 top-level
            break
        
        if not candidates:
            return
        
        checkpointed = 0
        for db_path in candidates:
            try:
                ok = await asyncio.to_thread(self._do_checkpoint, db_path)
                if ok:
                    checkpointed += 1
            except Exception as e:
                dream_log.warn(
                    "dream.task.d6_sqlite_wal.failed",
                    f"db={db_path} err={e!r}"[:200],
                )
        
        if checkpointed:
            dream_log.log(
                "dream.task.d6_sqlite_wal.cycle_done",
                f"checkpointed {checkpointed} dbs",
            )
    
    def _do_checkpoint(self, db_path: str) -> bool:
        """同步: 跑 PRAGMA wal_checkpoint(TRUNCATE)."""
        try:
            import sqlite3
            conn = sqlite3.connect(db_path, timeout=5)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.commit()
                return True
            finally:
                conn.close()
        except Exception:
            return False
