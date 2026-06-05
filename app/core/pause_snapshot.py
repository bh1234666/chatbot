from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from app.core import pause_state
from app.core.core_processes import ProcessRegistry
from app.schemas.api import ChatRequest, ResponsePlan


def _model_dump_or_dict(model: Any) -> dict | None:
    if model is None:
        return None
    try:
        return model.model_dump()
    except Exception:
        try:
            return dict(model.__dict__)
        except Exception:
            return None


async def collect_and_save_pause_snapshot(
    *,
    req: ChatRequest,
    trace_id: str,
    abort_event: asyncio.Event,
    plan: ResponsePlan | None,
    round3_partial_text: str,
    workspace_dir: str,
    debug: Any | None = None,
    log: Any | None = None,
) -> None:
    try:
        await asyncio.sleep(1.5)
    except asyncio.CancelledError:
        if log is not None:
            log.warning("[%s] pause snapshot collect: sleep cancelled, proceeding", trace_id)

    active_by_tid: dict[str, dict] = {}
    completed_by_tid: dict[str, dict] = {}
    try:
        from app.core.core_processes import registry as _proc_reg

        main_owner = ProcessRegistry.make_main_owner(trace_id)
        procs = await _proc_reg().list_owned_by(main_owner)
        for proc in procs:
            task_id = (
                proc.get("helper_task_id")
                or proc.get("task_id")
                or proc.get("description", "").split(":", 1)[0].strip()[:40]
            )
            if not task_id:
                continue
            active_by_tid[task_id] = {
                "task_id": task_id,
                "proc_id": proc.get("proc_id"),
                "iter": proc.get("last_iter") or proc.get("iter"),
                "recent_tools": list((proc.get("recent_tools") or [])[-6:]),
                "last_thought": (proc.get("last_thought_preview") or "")[:300],
                "last_progress_at": proc.get("last_progress_at"),
                "workspace_path": proc.get("helper_workspace") or proc.get("workspace") or "",
                "interrupted": True,
                "report_excerpt": "",
            }
    except Exception:
        if log is not None:
            log.exception("[%s] pause snapshot: ProcessRegistry list failed", trace_id)

    try:
        if workspace_dir and os.path.isdir(workspace_dir):
            for entry in os.listdir(workspace_dir):
                if not entry.startswith("_delegate_"):
                    continue
                helper_ws = os.path.join(workspace_dir, entry)
                if not os.path.isdir(helper_ws):
                    continue
                match = re.match(r'^_delegate_([A-Za-z0-9]+)_(.+)$', entry)
                task_id = match.group(2) if match else (entry.split("_")[-1] or entry)
                summary_path = os.path.join(helper_ws, ".helper_summary.txt")
                excerpt = ""
                if os.path.isfile(summary_path):
                    try:
                        with open(summary_path, "r", encoding="utf-8") as file:
                            excerpt = file.read(4000)
                    except (OSError, ValueError):
                        pass
                if task_id in active_by_tid:
                    if excerpt:
                        active_by_tid[task_id]["report_excerpt"] = excerpt[:1600]
                    active_by_tid[task_id]["summary_path"] = os.path.relpath(summary_path, workspace_dir)
                elif excerpt:
                    active_by_tid[task_id] = {
                        "task_id": task_id,
                        "proc_id": None,
                        "iter": None,
                        "recent_tools": [],
                        "last_thought": "",
                        "workspace_path": entry,
                        "summary_path": os.path.relpath(summary_path, workspace_dir),
                        "interrupted": True,
                        "report_excerpt": excerpt[:1600],
                    }
                elif task_id not in completed_by_tid:
                    completed_by_tid[task_id] = {
                        "task_id": task_id,
                        "workspace_path": entry,
                        "files": [],
                        "report_excerpt": "",
                    }
    except Exception:
        if log is not None:
            log.exception("[%s] pause snapshot: workspace scan failed", trace_id)

    plan_dict = _model_dump_or_dict(plan)
    new_active_tids = set(active_by_tid.keys())
    new_completed_tids = set(completed_by_tid.keys())
    try:
        old_snapshot = await pause_state.load_pause(
            archive_id=req.archive_id,
            group_id=req.group_id,
            user_id=req.user_id,
        )
        if old_snapshot:
            merged_from_old = 0
            for helper in (old_snapshot.get("active_helpers") or []):
                task_id = helper.get("task_id")
                if task_id and task_id not in new_active_tids and task_id not in new_completed_tids:
                    active_by_tid[task_id] = helper
                    merged_from_old += 1
            for helper in (old_snapshot.get("completed_helpers") or []):
                task_id = helper.get("task_id")
                if task_id and task_id not in new_active_tids and task_id not in new_completed_tids:
                    completed_by_tid[task_id] = helper
                    merged_from_old += 1
            if merged_from_old and debug is not None:
                debug.log(
                    "pause_state.merge",
                    f"merged {merged_from_old} task(s) from previous snapshot "
                    f"(active+completed not touched this chat)",
                )
    except Exception:
        if log is not None:
            log.exception("[%s] pause snapshot merge from old failed (non-fatal)", trace_id)

    try:
        ok = await pause_state.save_pause(
            archive_id=req.archive_id,
            group_id=req.group_id,
            user_id=req.user_id,
            trace_id=trace_id,
            user_message=req.message,
            round2_plan=plan_dict,
            round3_partial_text=round3_partial_text,
            active_helpers=list(active_by_tid.values()),
            completed_helpers=list(completed_by_tid.values()),
        )
        if debug is not None:
            debug.log(
                "pause_state.save.done",
                f"ok={ok} active={len(active_by_tid)} completed={len(completed_by_tid)}",
            )
    except Exception:
        if log is not None:
            log.exception("[%s] pause snapshot save failed", trace_id)
