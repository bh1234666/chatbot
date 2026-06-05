from __future__ import annotations

import os
import shutil
import time
from typing import Any, Iterable

_ACTIVE_DIR_GRACE_SEC = 30 * 60


def _active_helper_workspace_paths() -> set[str]:
    try:
        from app.core.core_processes import registry
    except Exception:
        return set()
    reg = registry()
    procs = getattr(reg, "_procs", {})
    paths: set[str] = set()
    try:
        for handle in list(procs.values()):
            if getattr(handle, "proc_type", "") != "helper":
                continue
            helper_ws = getattr(handle, "helper_workspace", "") or ""
            if helper_ws:
                paths.add(os.path.normcase(os.path.abspath(helper_ws)))
    except Exception:
        return set()
    return paths


def _is_active_helper_dir(target: str, active_paths: set[str]) -> bool:
    if not target or not active_paths:
        return False
    try:
        return os.path.normcase(os.path.abspath(target)) in active_paths
    except OSError:
        return False


def _is_recently_touched_dir(target: str, *, now: float | None = None, grace_sec: float = _ACTIVE_DIR_GRACE_SEC) -> bool:
    try:
        mtime = os.path.getmtime(target)
    except OSError:
        return False
    return ((now or time.time()) - mtime) < grace_sec


def _delegate_suffix_parts(name: str, current_tag: str | None = None) -> tuple[str, str]:
    rest = name[len("_delegate_"):]
    if current_tag:
        if rest == current_tag:
            return current_tag, ""
        prefix = f"{current_tag}_"
        if rest.startswith(prefix):
            return current_tag, rest[len(prefix):]
    if rest[:1].isdigit():
        sep = rest.find("_")
        if sep > 0:
            return rest[:sep], rest[sep + 1:]
    parts = rest.rsplit("_", 1)
    return (parts[0], parts[1]) if len(parts) == 2 and parts[1] else (rest, "")


def _delegate_dir_tag(name: str) -> str:
    tag, _task_id = _delegate_suffix_parts(name)
    return tag


def _delegate_task_id(name: str, current_tag: str | None = None) -> str:
    _tag, task_id = _delegate_suffix_parts(name, current_tag=current_tag)
    return task_id


def _delegate_belongs_to_tag(name: str, current_tag: str) -> bool:
    rest = name[len("_delegate_"):]
    return rest == current_tag or rest.startswith(f"{current_tag}_")


def cleanup_cross_user_delegate_dirs(ws_dir: str, current_user_id: str, *, debug: Any | None = None) -> int:
    if not ws_dir or not os.path.isdir(ws_dir):
        return 0
    from app.llm.tools.delegate import _user_workspace_tag as _tag

    current_tag = _tag(current_user_id)
    cleaned = 0
    preserved_active = 0
    preserved_recent = 0
    active_paths = _active_helper_workspace_paths()
    now = time.time()
    for name in os.listdir(ws_dir):
        if not name.startswith("_delegate_"):
            continue
        target = os.path.join(ws_dir, name)
        if not os.path.isdir(target):
            continue
        if _delegate_belongs_to_tag(name, current_tag):
            continue
        if _is_active_helper_dir(target, active_paths):
            preserved_active += 1
            continue
        if _is_recently_touched_dir(target, now=now):
            preserved_recent += 1
            continue
        try:
            shutil.rmtree(target, ignore_errors=True)
            cleaned += 1
        except OSError:
            pass
    if (cleaned or preserved_active or preserved_recent) and debug is not None:
        debug.log(
            "workspace.cross_user_cleanup",
            f"removed {cleaned} cross-user _delegate_* dirs "
            f"(preserved current user tag={current_tag}, active={preserved_active}, "
            f"recent={preserved_recent})",
        )
    return cleaned


def cleanup_old_same_user_delegate_dirs(
    ws_dir: str,
    current_user_id: str,
    *,
    max_age_days: int = 7,
    debug: Any | None = None,
) -> int:
    if not ws_dir or not os.path.isdir(ws_dir):
        return 0
    from app.llm.tools.delegate import _user_workspace_tag as _tag

    current_tag = _tag(current_user_id)
    cutoff = time.time() - (max_age_days * 86400)
    cleaned = 0
    preserved_active = 0
    active_paths = _active_helper_workspace_paths()
    for name in os.listdir(ws_dir):
        if not name.startswith("_delegate_"):
            continue
        target = os.path.join(ws_dir, name)
        if not os.path.isdir(target):
            continue
        if not _delegate_belongs_to_tag(name, current_tag):
            continue
        if _is_active_helper_dir(target, active_paths):
            preserved_active += 1
            continue
        try:
            mtime = os.path.getmtime(target)
        except OSError:
            mtime = 0
        if mtime > 0 and mtime < cutoff:
            try:
                shutil.rmtree(target, ignore_errors=True)
                cleaned += 1
            except OSError:
                pass
    if (cleaned or preserved_active) and debug is not None:
        debug.log(
            "workspace.old_delegate_cleanup",
            f"removed {cleaned} old _delegate_* dirs (>{max_age_days}d) "
            f"for current user tag={current_tag}; preserved_active={preserved_active}",
        )
    return cleaned


def cleanup_inactive_delegate_dirs(
    ws_dir: str,
    current_user_id: str,
    *,
    active_task_ids: Iterable[str] = (),
    keep_resume_task_ids: Iterable[str] = (),
    max_keep: int = 8,
    debug: Any | None = None,
) -> int:
    """Remove same-user helper sandboxes that are not active or intentionally resumable.

    This is deliberately narrower than cleanup_delegate_dirs(): it preserves current user's
    active helpers and explicitly resumable task ids, then keeps the newest few inactive
    directories as a safety buffer.
    """
    if not ws_dir or not os.path.isdir(ws_dir):
        return 0
    from app.llm.tools.delegate import _user_workspace_tag as _tag

    current_tag = _tag(current_user_id)
    active = {str(x) for x in active_task_ids if x}
    keep_resume = {str(x) for x in keep_resume_task_ids if x}
    active_paths = _active_helper_workspace_paths()
    candidates: list[tuple[float, str, str]] = []
    for name in os.listdir(ws_dir):
        if not name.startswith("_delegate_"):
            continue
        target = os.path.join(ws_dir, name)
        if not os.path.isdir(target):
            continue
        if not _delegate_belongs_to_tag(name, current_tag):
            continue
        task_id = _delegate_task_id(name, current_tag=current_tag)
        if task_id in active or task_id in keep_resume:
            continue
        if _is_active_helper_dir(target, active_paths):
            continue
        try:
            mtime = os.path.getmtime(target)
        except OSError:
            mtime = 0
        candidates.append((mtime, name, target))

    candidates.sort(reverse=True)
    cleaned = 0
    for _mtime, _name, target in candidates[max(0, max_keep):]:
        try:
            shutil.rmtree(target, ignore_errors=True)
            if not os.path.exists(target):
                cleaned += 1
        except OSError:
            pass
    if cleaned and debug is not None:
        debug.log(
            "workspace.inactive_delegate_cleanup",
            f"removed {cleaned} inactive same-user _delegate_* dirs "
            f"(active={len(active)}, keep_resume={len(keep_resume)}, max_keep={max_keep})",
        )
    return cleaned


def cleanup_stale_helpers_shared(
    ws_dir: str,
    *,
    max_age_days: int = 3,
    keep_recent: int = 10,
    debug: Any | None = None,
) -> int:
    """Remove stale subdirectories under _helpers_shared/ (2026-05-15 P64).

    _helpers_shared/ 是兄弟 helper 共享区, 跨会话残留极易污染本次任务:
    - 16:28:57 trace 实测: comp_custom 报告"已交付到主区"列表里混入了上一轮
      sorting 任务残留的 radix_bench/*.c/h 文件, 主线程被误导。
    - 单一任务的脚手架文件 (compress.h 等) 应该在会话结束后回收, 不该留到下一次。

    保留策略:
    - 删除 max_age_days 之前 mtime 的子目录 (默认 3 天 — 比 _delegate_ 短, 因为
      _helpers_shared/ 不像沙箱有 resume 价值, 是共享脚手架, 旧的可以重建)
    - 同时按最近修改时间保留 keep_recent 个最新子目录作为安全缓冲

    返回清理的目录数。
    """
    helpers_shared = os.path.join(ws_dir, "_helpers_shared")
    if not ws_dir or not os.path.isdir(helpers_shared):
        return 0

    cutoff = time.time() - (max_age_days * 86400)
    candidates: list[tuple[float, str, str]] = []
    for name in os.listdir(helpers_shared):
        target = os.path.join(helpers_shared, name)
        if not os.path.isdir(target):
            continue
        try:
            mtime = os.path.getmtime(target)
        except OSError:
            mtime = 0
        candidates.append((mtime, name, target))

    # 按 mtime 降序, 最新的 keep_recent 个无条件保留 (即使过期)
    candidates.sort(reverse=True)
    cleaned = 0
    cleaned_names: list[str] = []
    for idx, (mtime, name, target) in enumerate(candidates):
        if idx < keep_recent:
            continue  # 在安全缓冲区
        if mtime >= cutoff:
            continue  # 未过期
        try:
            shutil.rmtree(target, ignore_errors=True)
            if not os.path.exists(target):
                cleaned += 1
                cleaned_names.append(name)
        except OSError:
            pass

    if cleaned and debug is not None:
        debug.log(
            "workspace.helpers_shared_cleanup",
            f"removed {cleaned} stale _helpers_shared/<subdir> "
            f"(>{max_age_days}d, kept newest {keep_recent}); "
            f"removed: {cleaned_names[:5]}"
            + (f" (+{cleaned - 5})" if cleaned > 5 else ""),
        )
    return cleaned
