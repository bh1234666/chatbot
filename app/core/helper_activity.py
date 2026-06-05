from __future__ import annotations

import os
import re
from typing import Any

_DELEGATE_DIR_RE = re.compile(r'^_delegate_([A-Za-z0-9]+)_(.+)$')


def _task_id_from_process(process: dict) -> str:
    return (
        process.get("helper_task_id")
        or process.get("task_id")
        or process.get("description", "").split(":", 1)[0].strip()[:40]
    )


async def scan_active_helpers(
    *,
    trace_id: str,
    workspace_dir: str,
    log: Any | None = None,
    completed_since: float | None = None,
) -> tuple[dict[str, dict], dict[str, dict]]:
    active_by_tid: dict[str, dict] = {}
    completed_by_tid: dict[str, dict] = {}
    live_tids: set[str] = set()

    try:
        from app.core.core_processes import ProcessRegistry, registry as _proc_reg

        main_owner = ProcessRegistry.make_main_owner(trace_id)
        procs = await _proc_reg().list_owned_by(main_owner)
        for proc in procs:
            proc_type = proc.get("proc_type")
            if proc_type and proc_type != "helper":
                continue
            task_id = _task_id_from_process(proc)
            if not task_id:
                continue
            live_tids.add(task_id)
            active_by_tid[task_id] = {
                "task_id": task_id,
                "proc_id": proc.get("proc_id"),
                "iter": proc.get("last_iter") or proc.get("iter"),
                "recent_tools": list((proc.get("recent_tools") or [])[-6:]),
                "last_thought": (proc.get("last_thought_preview") or "")[:300],
                "last_progress_at": proc.get("last_progress_at"),
                "workspace_path": proc.get("helper_workspace") or proc.get("workspace") or "",
                "interrupted": False,
                "report_excerpt": "",
            }
    except Exception:
        if log is not None:
            log.exception("[%s] scan_active_helpers: registry list failed", trace_id)

    try:
        if workspace_dir and os.path.isdir(workspace_dir):
            for entry in os.listdir(workspace_dir):
                match = _DELEGATE_DIR_RE.match(entry)
                if not match:
                    continue
                task_id = match.group(2)
                helper_ws = os.path.join(workspace_dir, entry)
                if not os.path.isdir(helper_ws):
                    continue
                if completed_since is not None:
                    try:
                        if os.path.getmtime(helper_ws) < completed_since:
                            continue
                    except OSError:
                        continue
                summary_path = os.path.join(helper_ws, ".helper_summary.txt")
                excerpt = ""
                if os.path.isfile(summary_path):
                    try:
                        with open(summary_path, "r", encoding="utf-8") as file:
                            excerpt = file.read(2000)
                    except (OSError, ValueError):
                        pass

                if task_id in live_tids:
                    if excerpt:
                        active_by_tid[task_id]["report_excerpt"] = excerpt[:1600]
                    active_by_tid[task_id]["summary_path"] = os.path.relpath(summary_path, workspace_dir)
                elif task_id not in completed_by_tid:
                    completed_by_tid[task_id] = {
                        "task_id": task_id,
                        "workspace_path": entry,
                        "files": [],
                        "report_excerpt": excerpt[:1600] if excerpt else "",
                    }
    except Exception:
        if log is not None:
            log.exception("[%s] scan_active_helpers: workspace scan failed", trace_id)

    return active_by_tid, completed_by_tid


async def request_active_helpers_finalize(*, trace_id: str, active_helpers: dict[str, dict], debug: Any | None = None, log: Any | None = None) -> int:
    if not active_helpers:
        return 0
    sent = 0
    try:
        from app.core.core_processes import registry as _proc_reg

        reg = _proc_reg()
        for task_id, helper in active_helpers.items():
            proc_id = helper.get("proc_id")
            if not proc_id:
                continue
            try:
                find_handle = getattr(reg, "get", None) or getattr(reg, "find_by_proc_id", None)
                if find_handle is None:
                    continue
                handle = await find_handle(proc_id)
                if handle is None:
                    continue
                abort_event = getattr(handle, "abort_event", None)
                if abort_event is not None and hasattr(abort_event, "set"):
                    abort_event.set()
                    sent += 1
                    if debug is not None:
                        debug.log(
                            "orchestrate.complete.finalize_helper",
                            f"sent cooperative abort to helper task_id={task_id} proc_id={proc_id}",
                        )
            except Exception:
                if log is not None:
                    log.exception("[%s] failed to send abort to helper %s (non-fatal)", trace_id, task_id)
    except Exception:
        if log is not None:
            log.exception("[%s] request_active_helpers_finalize failed", trace_id)
    return sent
