"""
进程管理工具 — LLM 可见的 processes 工具。

让 helper 和主线程能查询/杀死自己创建的进程。
实际逻辑委托给 app.core.core_processes.ProcessRegistry (单例)。
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("processes.tool")


PROCESSES_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "processes",
        "description": (
            "List or kill your own helper/subprocess processes.\n"
            "This tool has only `list` and `kill`. It has no `status` action; "
            "use delegate(action='status') for the helper dashboard, and use "
            "processes(action='list') only when you need process/heartbeat/proc_id details.\n"
            "\n"
            "## Actions\n"
            "- **list**: List all processes you own (or all in your trace if main thread).\n"
            "  Returns: {\"ok\": true, \"processes\": [...], \"stats\": {...}}\n"
            "- **kill**: Kill a specific process by proc_id. Requires a valid kill reason. Set terminate=true for a hard terminate that also clears preserved helper state.\n"
            "  Returns fields such as ok, killed_proc_id, and error.\n"
            "\n"
            "## Valid kill reasons (required for helper kills)\n"
            "- self_report_cant_do: the helper explicitly reports that it cannot complete the task.\n"
            "- self_report_done: the helper explicitly reports that its task is done.\n"
            "- sibling_completed_first: another equivalent helper completed the same task first.\n"
            "- content_deemed_useless: the main process has verified that this process output is no longer useful.\n"
            "- api_stall_emergency: heartbeat evidence shows an API stall that needs emergency interruption.\n"
            "\n"
            "## Process fields (in list response)\n"
            "proc_id, proc_type(helper|subprocess), elapsed_seconds, description\n"
            "For helpers: task_id, iter, recent_tools, last_thought, progress_summary,\n"
            "  last_heartbeat_age_sec, heartbeat_status(fresh|slow|stale|no_heartbeat_yet),\n"
            "  what_doing(current action), estimated_remaining_sec(estimated seconds remaining),\n"
            "  wait_or_continue(wait=keep waiting|check=inspect actively|intervene=review/recover before waiting unchanged)\n"
            "For subprocesses: pid, command, rss_gib, process_tree_count when available\n"
            "\n进程工具用于查看或协作中断自己负责的 helper/子进程。\n"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "kill"],
                    "description": "Which operation to perform.\n进程操作类型。",
                },
                "owner": {
                    "type": "string",
                    "description": (
                        "Your owner ID (given in system prompt). Format: main:<trace_id> "
                        "or helper:<trace_id>:<task_id>.\n"
                        "当前调用者 owner 标识。"
                    ),
                },
                "proc_id": {
                    "type": "string",
                    "description": "The proc_id to kill (required for action=kill). Get from list output.\n要终止的进程 ID。",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Required for action=kill. Must be one of: self_report_cant_do, "
                        "self_report_done, sibling_completed_first, content_deemed_useless, "
                        "api_stall_emergency.\n"
                        "终止原因，必须使用允许值。"
                    ),
                },
                "terminate": {
                    "type": "boolean",
                    "description": (
                        "For action=kill only. true means hard terminate the helper and clear preserved helper state/workspace when available.\n"
                        "Hard terminate the helper and clear preserved helper state/workspace when available."
                    ),
                    "default": False,
                },
            },
            "required": ["action", "owner"],
        },
    },
}


async def handle_processes(args: dict) -> dict[str, Any]:
    """Handle processes tool calls. Returns a dict (caller json.dumps it)."""
    from app.core.core_processes import registry, validate_kill_reason

    reg = registry()
    action = (args.get("action") or "").strip().lower()
    owner = (args.get("owner") or "").strip()

    if not action:
        return {"ok": False, "error": "Missing action. Supported actions are list and kill.\n缺少 action 参数。"}
    if not owner:
        return {"ok": False, "error": "Missing owner. Pass your owner ID from the system prompt.\n缺少 owner 参数。"}

    if action == "list":
        procs = await reg.list_owned_by(owner)
        stats = await reg.stats()
        result: dict[str, Any] = {
            "ok": True,
            "stats": stats,
            "owner": owner,
        }
        # L7-1 (2026-05-09): 空数组省略,节省 token
        if procs:
            result["processes"] = procs
        return result

    elif action == "kill":
        proc_id = (args.get("proc_id") or "").strip()
        reason = (args.get("reason") or "").strip()
        terminate = bool(args.get("terminate", False))

        if not proc_id:
            return {"ok": False, "error": "Missing proc_id. Use processes(action='list') to find killable proc_id values.\n缺少 proc_id 参数。"}
        if not reason:
            return {
                "ok": False,
                "error": (
                    "Missing reason. Killing a helper requires one valid reason: "
                    "self_report_cant_do, self_report_done, sibling_completed_first, "
                    "content_deemed_useless, api_stall_emergency.\n"
                    "缺少 reason 参数。"
                ),
            }

        helper = await reg.get(proc_id)
        result = await reg.kill(
            proc_id, requested_by=owner, reason=reason,
            force=terminate,
        )
        if not terminate or not result.get("ok") or result.get("proc_type") != "helper":
            return result

        helper_task_id = str(result.get("task_id") or getattr(helper, "helper_task_id", "") or "").strip()
        helper_ws = str(getattr(helper, "helper_workspace", "") or "").strip()
        helper_archive_id = str(getattr(helper, "archive_id", "") or "").strip()
        helper_group_id = str(getattr(helper, "group_id", "") or "").strip()
        helper_user_id = str(getattr(helper, "user_id", "") or "").strip()

        if helper_ws:
            try:
                import shutil
                from pathlib import Path

                ws_path = Path(helper_ws).resolve()
                shutil.rmtree(ws_path, ignore_errors=True)
                result["helper_workspace_removed"] = not ws_path.exists()
                result["helper_workspace"] = str(ws_path)
            except Exception as exc:
                result["helper_workspace_removed"] = False
                result["helper_workspace_error"] = f"{type(exc).__name__}: {exc}"

        if helper_task_id and helper_archive_id and helper_group_id and helper_user_id:
            try:
                from app.core import pause_state as _pause_state

                await _pause_state.remove_helper_from_pause(
                    archive_id=helper_archive_id,
                    group_id=helper_group_id,
                    user_id=helper_user_id,
                    task_id=helper_task_id,
                )
                result["pause_state_pruned"] = True
            except Exception as exc:
                result["pause_state_pruned"] = False
                result["pause_state_error"] = f"{type(exc).__name__}: {exc}"

        result["terminate"] = terminate
        if terminate:
            result["terminate_mode"] = "hard"
        return result

    else:
        return {
            "ok": False,
            "error": (
                f"Unknown processes action: {action!r}. This tool supports only list and kill. "
                "Use delegate(action='status') for the helper dashboard and completed-task state; "
                "use processes(action='list') for heartbeats and proc_id details.\n"
                "processes 仅支持 list/kill。"
            ),
        }
