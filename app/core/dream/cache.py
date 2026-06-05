"""
Dream Cache — checkpoint 机制

仅供 LongRunningDreamTask (D 类) 使用. 大多数 A 类任务不需要 checkpoint.

设计:
- 存储路径: {workspace_root}/{dream_cache_subdir}/{context_id}/{task_name}/
- atomic save: 写 .tmp → rename .json (POSIX 原子)
- manifest: 记录已完成 step + step_results

服务重启时:
    manifest = await dream_cache.load_manifest(context_id, task_name)
    completed = manifest["completed_steps"]
    # 跳过已完成, 续跑

容错:
- 写一半被 cancel → tmp 残留, 下次启动清理 + 重跑该步
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from app.config import settings
from app.core.dream.dream_log import dream_log


_MANIFEST_NAME = "manifest.json"


def _cache_path(workspace_root: str, context_id: str, task_name: str) -> str:
    """返回 cache 目录路径. workspace_root 通常是 main_workspace。"""
    safe_ctx = "".join(c if c.isalnum() or c in "._-" else "_" for c in context_id)
    safe_task = "".join(c if c.isalnum() or c in "._-" else "_" for c in task_name)
    return os.path.join(
        workspace_root,
        settings.dream_cache_subdir,
        safe_ctx,
        safe_task,
    )


# 工作区根目录的获取 - 注入时设置 (避免循环 import)
_workspace_root_getter: callable | None = None


def set_workspace_root_getter(getter):
    """由 main.py / orchestrator 启动时注入。"""
    global _workspace_root_getter
    _workspace_root_getter = getter


def _get_workspace_root() -> str:
    if _workspace_root_getter is None:
        return ".temp"  # fallback
    try:
        return _workspace_root_getter()
    except Exception:
        return ".temp"


class DreamCache:
    """Dream cache 操作单例。"""

    async def load_manifest(self, context_id: str, task_name: str) -> dict[str, Any]:
        """加载 manifest. 不存在则返回空。"""
        path = _cache_path(_get_workspace_root(), context_id, task_name)
        manifest_path = os.path.join(path, _MANIFEST_NAME)
        if not os.path.isfile(manifest_path):
            return {"completed_steps": [], "step_results": {}, "started_at": None}
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            dream_log.warn(
                f"dream.cache.load_failed",
                f"ctx={context_id} task={task_name} err={e!r}; treating as fresh",
            )
            return {"completed_steps": [], "step_results": {}, "started_at": None}

    async def save_step(
        self,
        context_id: str,
        task_name: str,
        step_name: str,
        result: Any,
    ) -> None:
        """原子保存一步结果。"""
        path = _cache_path(_get_workspace_root(), context_id, task_name)
        os.makedirs(path, exist_ok=True)
        
        # 加载现有 manifest
        manifest = await self.load_manifest(context_id, task_name)
        if step_name in manifest["completed_steps"]:
            return  # 已存
        
        # 更新
        manifest["completed_steps"].append(step_name)
        try:
            manifest["step_results"][step_name] = result
        except Exception:
            # result 可能不可序列化, 退化为字符串
            manifest["step_results"][step_name] = str(result)[:1000]
        if not manifest.get("started_at"):
            manifest["started_at"] = time.time()
        manifest["last_update"] = time.time()
        
        # atomic write: .tmp → rename
        tmp_path = os.path.join(path, _MANIFEST_NAME + ".tmp")
        final_path = os.path.join(path, _MANIFEST_NAME)
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, default=str)
            # POSIX atomic rename
            os.replace(tmp_path, final_path)
            dream_log.log(
                f"dream.cache.save",
                f"ctx={context_id} task={task_name} step={step_name}",
            )
        except Exception as e:
            # 清 tmp
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            dream_log.error(
                f"dream.cache.save_failed",
                f"ctx={context_id} task={task_name} step={step_name} err={e!r}",
            )
            raise

    async def mark_complete(self, context_id: str, task_name: str) -> None:
        """标记整任务完成. 不删除 cache (留 audit), 但可被后续 cleanup。"""
        path = _cache_path(_get_workspace_root(), context_id, task_name)
        manifest = await self.load_manifest(context_id, task_name)
        manifest["completed_at"] = time.time()
        
        tmp_path = os.path.join(path, _MANIFEST_NAME + ".tmp")
        final_path = os.path.join(path, _MANIFEST_NAME)
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, default=str)
            os.replace(tmp_path, final_path)
            dream_log.log(f"dream.cache.complete", f"ctx={context_id} task={task_name}")
        except Exception as e:
            dream_log.error(f"dream.cache.complete_failed", repr(e)[:200])

    async def cleanup_completed(
        self,
        context_id: str | None = None,
        max_age_sec: int = 7 * 86400,
    ) -> int:
        """清理已完成 + 老的 cache. 返回清理数。"""
        root = os.path.join(_get_workspace_root(), settings.dream_cache_subdir)
        if not os.path.isdir(root):
            return 0
        
        cleaned = 0
        now = time.time()
        try:
            ctx_dirs = [context_id] if context_id else os.listdir(root)
        except OSError:
            return 0
        
        for ctx_dir in ctx_dirs:
            ctx_path = os.path.join(root, ctx_dir)
            if not os.path.isdir(ctx_path):
                continue
            for task_dir in os.listdir(ctx_path):
                task_path = os.path.join(ctx_path, task_dir)
                manifest_path = os.path.join(task_path, _MANIFEST_NAME)
                if not os.path.isfile(manifest_path):
                    continue
                try:
                    with open(manifest_path) as f:
                        manifest = json.load(f)
                    # 已完成 且 老于 max_age
                    completed_at = manifest.get("completed_at", 0)
                    if completed_at and (now - completed_at) > max_age_sec:
                        import shutil
                        shutil.rmtree(task_path, ignore_errors=True)
                        cleaned += 1
                except Exception:
                    continue
        
        if cleaned:
            dream_log.log(
                f"dream.cache.cleanup",
                f"cleaned {cleaned} old completed caches",
            )
        return cleaned


dream_cache = DreamCache()
