from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

from app.llm import client as llm

log = logging.getLogger(__name__)


INTERMEDIATE_FEEDBACK_SYSTEM = (
    "You decide whether to send a short mid-task update while work is still running. "
    "The update is visible to the user and will be remembered with the final answer.\n\n"
    "## Truth Priority\n"
    "Use current_event_facts as the source of truth. They are public-level work facts, not private implementation details. The user_request is only background. "
    "Name only work items whose state is explicit in the facts. "
    "Mention work items from the original request only when the facts name them in the current state. "
    "Preserve technical task labels, filenames, paths, and domain terms exactly when you name them; "
    "do not reinterpret opaque labels or translate identifier components into unrelated meanings. "
    "If a label is not user-friendly, describe the visible work area neutrally without changing its meaning. "
    "For work_started or running facts, use started/running wording. "
    "For work_branch_returned facts, say the branch returned for status checking; completion is unknown until work_result_summary confirms it. "
    "For completed work facts inside work_result_summary, use finished/returned-result wording only for those labels. "
    "For work_blocked_before_start facts, say those branches were blocked before startup and the plan is being adjusted; describe only confirmed running, finished, or active work as such. "
    "If current_event_facts include next_direction, reflect that direction instead of the original unstarted task list. "
    "A finished work branch is not the same as the whole user task being finished.\n\n"
    "## Decision\n"
    "Speak when the update helps the user understand progress, recovery, waiting, or a reached milestone. "
    "Keep persona voice and turn internal workflow into user-facing progress wording unless the user is explicitly watching workflow details. "
    "Use outcome-level wording such as reading material, checking files, verifying results, preparing a report, or applying changes. "
    "Do not expose internal process labels, private orchestration names, or private workspace paths in the user-visible message. "
    "Reserve whole-task completion for the final answer; branch completion may be named only when the facts say that branch finished.\n\n"
    "## Output\n"
    "Return strict JSON: {\"should_reply\": true|false, \"message\": \"...\"}. "
    "When should_reply is true, message must be one short Chinese line, normally under 45 characters.\n\n"
    "判断是否需要中途回复；需要时按人设输出一句用户可见进度，当前事件事实优先，技术标签原样保留。"
)


@dataclass
class IntermediateFeedbackGate:
    """Per-request gate for user-visible mid-task feedback."""

    preference: float = 0.5
    channel: str = "chat"
    start_at: float = 0.0
    last_emit_at: float = 0.0
    last_consider_at: float = 0.0
    emitted_count: int = 0
    last_event_consider_at: dict[str, float] | None = None
    last_event_emit_at: dict[str, float] | None = None

    def __post_init__(self) -> None:
        now = time.monotonic()
        if not self.start_at:
            self.start_at = now
        if not self.last_emit_at:
            self.last_emit_at = self.start_at
        if not self.last_consider_at:
            self.last_consider_at = self.start_at
        if self.last_event_consider_at is None:
            self.last_event_consider_at = {}
        if self.last_event_emit_at is None:
            self.last_event_emit_at = {}

    def allow_consideration(self, event: str, *, force: bool = False) -> bool:
        if self.preference <= 0.0:
            return False
        now = time.monotonic()
        event = event or "scheduled"
        if self.channel == "agent" and self.preference >= 1.0:
            per_event_gap = {
                "helper_start": 0.0,
                "helper_done": 0.0,
                "helper_blocked": 1.5,
                "milestone": 2.0,
                "breakthrough": 2.0,
                "stuck": 30.0,
                "long_silence": 45.0,
                "scheduled": 20.0,
            }.get(event, 8.0)
            has_event_seen = bool(self.last_event_consider_at and event in self.last_event_consider_at)
            last_event = (self.last_event_consider_at or {}).get(event, self.start_at)
            if has_event_seen and not force and per_event_gap > 0 and now - last_event < per_event_gap:
                return False
            self.last_consider_at = now
            if self.last_event_consider_at is not None:
                self.last_event_consider_at[event] = now
            return True
        elapsed = now - self.start_at
        since_emit = now - self.last_emit_at
        if self.channel == "agent":
            min_gap = 8.0 + (self.preference * 25.0)
            max_gap = 90.0 + (self.preference * 180.0)
        else:
            min_gap = 25.0 + (self.preference * 95.0)
            max_gap = 150.0 + (self.preference * 330.0)
        if event in {"helper_start", "helper_done", "helper_blocked", "milestone", "breakthrough", "stuck"}:
            min_gap *= 0.65
        elif event == "long_silence":
            min_gap *= 0.9
        else:
            min_gap *= 1.15

        first_visible_delay = 12.0 if self.channel == "agent" else 30.0
        overdue = since_emit >= max_gap and elapsed >= first_visible_delay
        if not force and not overdue and since_emit < min_gap:
            return False
        if not force and not overdue and now - self.last_consider_at < max(5.0, min_gap * 0.35):
            return False
        self.last_consider_at = now
        if self.last_event_consider_at is not None:
            self.last_event_consider_at[event] = now
        return True

    def record_emit(self, event: str | None = None) -> None:
        self.last_emit_at = time.monotonic()
        self.emitted_count += 1
        if event and self.last_event_emit_at is not None:
            self.last_event_emit_at[event] = self.last_emit_at


@dataclass(frozen=True)
class _FeedbackSink:
    sink_id: int
    queue: asyncio.Queue
    archive_id: str = ""
    group_id: str = ""
    user_id: str = ""
    trace_id: str = ""


_feedback_queue_var: ContextVar[asyncio.Queue | None] = ContextVar(
    "intermediate_feedback_queue",
    default=None,
)
_feedback_sink_meta_var: ContextVar[tuple[str, str, str, str] | None] = ContextVar(
    "intermediate_feedback_sink_meta",
    default=None,
)
_feedback_sink_seq = 0
_registered_feedback_sinks: dict[int, _FeedbackSink] = {}


@contextmanager
def intermediate_feedback_event_sink(
    queue: asyncio.Queue | None,
    *,
    archive_id: str = "",
    group_id: str = "",
    user_id: str = "",
    trace_id: str = "",
) -> Iterator[None]:
    """Route workflow milestones into the current Round2 feedback gate."""

    global _feedback_sink_seq
    sink_id: int | None = None
    if queue is not None:
        _feedback_sink_seq += 1
        sink_id = _feedback_sink_seq
        _registered_feedback_sinks[sink_id] = _FeedbackSink(
            sink_id=sink_id,
            queue=queue,
            archive_id=archive_id,
            group_id=group_id,
            user_id=user_id,
            trace_id=trace_id,
        )
    token = _feedback_queue_var.set(queue)
    meta_token = _feedback_sink_meta_var.set((archive_id, group_id, user_id, trace_id))
    try:
        yield
    finally:
        try:
            _feedback_sink_meta_var.reset(meta_token)
        except ValueError:
            _feedback_sink_meta_var.set(None)
            log.warning("intermediate feedback sink meta reset crossed context; cleared current context")
        try:
            _feedback_queue_var.reset(token)
        except ValueError:
            _feedback_queue_var.set(None)
            log.warning("intermediate feedback sink queue reset crossed context; cleared current context")
        if sink_id is not None:
            _registered_feedback_sinks.pop(sink_id, None)


def publish_feedback_workflow_event(payload: dict) -> None:
    seen: set[int] = set()
    queue = _feedback_queue_var.get(None)
    if queue is not None:
        meta = _feedback_sink_meta_var.get(None)
        if meta is None:
            _put_feedback_event(queue, payload)
            seen.add(id(queue))
        else:
            archive_id, group_id, user_id, trace_id = meta
            if _feedback_sink_matches(
                _FeedbackSink(0, queue, archive_id, group_id, user_id, trace_id),
                payload,
            ):
                _put_feedback_event(queue, payload)
                seen.add(id(queue))
    for sink in list(_registered_feedback_sinks.values()):
        if id(sink.queue) in seen:
            continue
        if not _feedback_sink_matches(sink, payload):
            continue
        _put_feedback_event(sink.queue, payload)
        seen.add(id(sink.queue))


def _put_feedback_event(queue: asyncio.Queue, payload: dict) -> None:
    try:
        queue.put_nowait(("_feedback_workflow", payload))
    except Exception:
        pass


def _feedback_sink_matches(sink: _FeedbackSink, payload: dict) -> bool:
    if sink.archive_id and payload.get("archive_id") and payload.get("archive_id") != sink.archive_id:
        return False
    if sink.group_id and payload.get("group_id") and payload.get("group_id") != sink.group_id:
        return False
    if sink.user_id and payload.get("user_id") and payload.get("user_id") != sink.user_id:
        return False
    if sink.trace_id and payload.get("trace_id") and payload.get("trace_id") != sink.trace_id:
        return False
    return True


def parse_intermediate_feedback_preference(value: str, default: float = 0.5) -> float:
    try:
        parsed = float((value or "").strip())
    except Exception:
        return default
    return max(0.0, min(1.0, parsed))


def classify_workflow_feedback_event(payload: dict) -> str | None:
    kind = str((payload or {}).get("kind") or "")
    if kind == "helper_start":
        return "helper_start"
    if kind == "helper_blocked":
        return "helper_blocked"
    if kind == "helper_registry_done":
        return "helper_exit"
    if kind == "helper_progress":
        status = str(payload.get("heartbeat_status") or "").lower()
        wait_or_continue = str(payload.get("wait_or_continue") or "").lower()
        if wait_or_continue in {"stuck", "kill", "intervene"} or payload.get("_runaway"):
            return "stuck"
        if status in {"stale", "missing"}:
            return "long_silence"
        return None
    if kind == "main_milestone":
        milestone = str(payload.get("milestone") or "")
        if milestone.endswith("_started"):
            return "stage_start"
        return "milestone"
    if kind in {"tool_done", "command_done"}:
        return "milestone"
    if kind in {"tool_error", "command_error", "helper_error"}:
        return "stuck"
    return None


def summarize_workflow_feedback_event(payload: dict) -> str:
    kind = str((payload or {}).get("kind") or "workflow")
    helper_kind = str(payload.get("helper_kind") or "").strip()
    task_id = str(payload.get("task_id") or "").strip()
    description = str(payload.get("description") or "").strip()
    what_doing = str(payload.get("what_doing") or "").strip()
    last_note = str(payload.get("last_note") or "").strip()
    last_thought = str(payload.get("last_thought") or "").strip()
    recent_tools = payload.get("recent_tools")
    if isinstance(recent_tools, list):
        tools = " -> ".join(str(x) for x in recent_tools[-4:] if x)
    else:
        tools = ""
    elapsed = payload.get("elapsed_seconds")
    elapsed_text = f"{elapsed:.1f}s" if isinstance(elapsed, (int, float)) else ""

    if kind == "helper_start":
        parts = [
            "event_fact=helper_started",
            "state=running",
            f"helper_task={task_id}" if task_id else "",
            f"helper_kind={helper_kind}" if helper_kind else "",
            f"visible_work={description[:500]}" if description else "",
            f"event_focus_task_ids={task_id}" if task_id else "",
            "label_policy=task ids, filenames, paths, and technical terms are stable labels; preserve them if named",
            "truth_scope=this helper has started; no result is available from this event yet",
            "wording_hint=use started or working wording",
        ]
        return "\n".join(p for p in parts if p)

    if kind == "helper_registry_done":
        parts = [
            "event_fact=helper_process_exited",
            f"state={str(payload.get('status') or 'exited').strip()}",
            f"helper_task={task_id}" if task_id else "",
            f"helper_kind={helper_kind}" if helper_kind else "",
            f"elapsed={elapsed_text}" if elapsed_text else "",
            f"visible_work={description[:500]}" if description else "",
            f"event_focus_task_ids={task_id}" if task_id else "",
            "label_policy=task ids, filenames, paths, and technical terms are stable labels; preserve them if named",
            "truth_scope=this only says the helper process left the active registry; success, blocker, resource request, or failure requires the delegate result.\n\n进程退出不等于任务完成。",
            "wording_hint=say this branch exited or returned for collection; use completed wording only when delegate_result says it completed.\n\n只有 delegate_result 确认时才说完成。",
        ]
        return "\n".join(p for p in parts if p)

    if kind == "helper_blocked":
        blocked = payload.get("blocked_tasks") or payload.get("tasks") or []
        blocked_ids: list[str] = []
        blocked_kinds: list[str] = []
        blocked_modes: list[str] = []
        if isinstance(blocked, list):
            for item in blocked[:12]:
                if isinstance(item, dict):
                    tid = str(item.get("task_id") or "").strip()
                    hkind = str(item.get("helper_kind") or item.get("kind") or "").strip()
                    mode = str(item.get("mode") or "").strip()
                    if tid:
                        blocked_ids.append(tid)
                    if hkind:
                        blocked_kinds.append(hkind)
                    if mode:
                        blocked_modes.append(mode)
                else:
                    text = str(item).strip()
                    if text:
                        blocked_ids.append(text[:80])
            blocked_text = ", ".join(blocked_ids)
        else:
            blocked_text = str(blocked)
        reason = str(payload.get("reason") or payload.get("error") or "").strip()
        instruction = str(payload.get("description") or payload.get("instruction") or "").strip()
        combined = f"{reason}\n{instruction}".lower()
        next_direction = ""
        if "framework" in combined or "contract" in combined:
            next_direction = "build_or_refine_framework_first"
        parts = [
            "event_fact=helper_blocked_before_start",
            "state=blocked_not_running",
            f"blocked_count={payload.get('blocked_count')}" if payload.get("blocked_count") is not None else "",
            f"blocked_task_ids={blocked_text}" if blocked_text else "",
            f"blocked_helper_kinds={', '.join(dict.fromkeys(blocked_kinds))}" if blocked_kinds else "",
            f"blocked_helper_modes={', '.join(dict.fromkeys(blocked_modes))}" if blocked_modes else "",
            f"reason={reason[:500]}" if reason else "",
            f"instruction={instruction[:500]}" if instruction else "",
            f"next_direction={next_direction}" if next_direction else "",
            "truth_scope=the blocked helpers did not start and are not running; describe them as blocked before startup",
            "wording_hint=state that the attempted delegation was rejected before start and the plan is being adjusted",
        ]
        return "\n".join(p for p in parts if p)

    if kind == "helper_progress":
        heartbeat = str(payload.get("heartbeat_status") or "").strip()
        wait_or_continue = str(payload.get("wait_or_continue") or "").strip()
        state = wait_or_continue if wait_or_continue in {"stuck", "kill", "intervene"} else (heartbeat or wait_or_continue or "running")
        runaway_reason = str(payload.get("_runaway_reason") or "").strip()
        parts = [
            "event_fact=helper_progress",
            f"state={state}",
            "event_fact=helper_runaway_or_stuck" if wait_or_continue in {"stuck", "kill", "intervene"} or payload.get("_runaway") else "",
            f"runaway_reason={runaway_reason}" if runaway_reason else "",
            f"helper_task={task_id}" if task_id else "",
            f"helper_kind={helper_kind}" if helper_kind else "",
            f"elapsed={elapsed_text}" if elapsed_text else "",
            f"current={what_doing}" if what_doing else "",
            f"recent_tools={tools}" if tools else "",
            f"note={last_note}" if last_note else "",
            f"thought={last_thought}" if last_thought else "",
            "truth_scope=state=intervene/stuck means the helper is not healthy progress; describe recovery, interruption, waiting-with-reason, or replanning instead of normal progress.\n\nintervene/stuck 是异常分支事实。",
            "wording_hint=for intervene/stuck say an abnormal branch is being handled, recovered, stopped, split, or explicitly waited on; otherwise use still working, checking, or waiting wording.\n\n异常分支说恢复或重规划。",
        ]
        return "\n".join(p for p in parts if p)

    if kind in {"main_tool_done", "tool_done"}:
        tool = str(payload.get("tool") or "").strip()
        status = str(payload.get("status") or "done").strip() or "done"
        preview = str(payload.get("result_preview") or "").strip()
        delegate_summary = ""
        todo_summary = ""
        if tool == "delegate" and preview:
            try:
                parsed = json.loads(preview)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                delegate_summary = _summarize_delegate_result_payload(parsed)
        elif tool == "todo_write" and preview:
            try:
                parsed = json.loads(preview)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                todo_summary = _summarize_todo_write_result_payload(parsed)
        parts = [
            f"event_fact={kind}",
            f"state={status}",
            f"tool={tool}" if tool else "",
            f"elapsed={elapsed_text}" if elapsed_text else "",
            delegate_summary,
            todo_summary,
            f"result_preview={preview[:500]}" if preview and not delegate_summary and not todo_summary else "",
            "truth_scope=report only the tool result facts above",
        ]
        return "\n".join(p for p in parts if p)

    if kind == "main_milestone":
        milestone = str(payload.get("milestone") or "").strip()
        message = str(payload.get("message") or payload.get("text") or "").strip()
        event_fact, state, public_message = _main_milestone_public_fact(milestone, message)
        if event_fact == "planning_stage_started":
            parts = [
                "event_fact=planning_stage_started",
                f"state={state}",
                "visible_milestone=planning_and_tool_preparation_started",
                f"message={public_message[:300]}" if public_message else "",
                "truth_scope=this is only the start of a planning/tool stage; no helper, file read, framework, analysis, document, or deliverable is complete yet.\n\n阶段开始不代表已有产物。",
                "wording_hint=say planning or preparation has started; mention completion, reading, generation, parallel execution, or helper work only when separate facts say so.\n\n只说已开始规划或准备。",
            ]
            return "\n".join(p for p in parts if p)
        if event_fact == "planning_recheck_started":
            parts = [
                "event_fact=planning_recheck_started",
                f"state={state}",
                "visible_milestone=approach_recheck_and_evidence_tightening",
                f"message={public_message[:300]}" if public_message else "",
                "truth_scope=this says the approach is being rechecked with stronger evidence; it does not mean new helpers are running or any deliverable is complete.\n\n重新核对不代表已完成或已启动新 helper。",
                "wording_hint=say the plan is being tightened, evidence is being rechecked, or the approach is being adjusted.\n\n说正在收紧方案、核验证据或调整办法。",
            ]
            return "\n".join(p for p in parts if p)
        parts = [
            f"event_fact={event_fact}",
            f"state={state}",
            f"message={public_message[:300]}" if public_message else "",
            "truth_scope=report only this milestone fact; infer requested-deliverable completion only when the message explicitly says so.\n\n里程碑只代表当前事实。",
        ]
        return "\n".join(p for p in parts if p)

    parts = [
        f"workflow_kind={kind}",
        f"helper_kind={helper_kind}" if helper_kind else "",
        f"task={task_id}" if task_id else "",
        f"elapsed={elapsed_text}" if elapsed_text else "",
        f"current={what_doing}" if what_doing else "",
        f"recent_tools={tools}" if tools else "",
        f"note={last_note}" if last_note else "",
        f"thought={last_thought}" if last_thought else "",
        f"description={description[:400]}" if description else "",
    ]
    return "\n".join(p for p in parts if p)


def _summarize_delegate_result_payload(result: dict) -> str:
    structured_summary = result.get("result_summary")
    if isinstance(structured_summary, dict):
        result = {**result, **structured_summary}
    action = str(result.get("action") or "").strip().lower()
    requested = result.get(
        "background_work_requested",
        result.get("helpers_requested", result.get("background_work_started", result.get("helpers_initially_spawned", 0))),
    )
    returned = result.get("results_returned", result.get("helpers_returned", result.get("helpers_completed", 0)))
    success = result.get("success_count", 0)
    running = result.get("background_work_running", result.get("helpers_still_running", 0))
    unavailable = result.get("background_work_unavailable", result.get("helpers_unavailable", 0))
    task_ok = result.get("task_ok")
    error = str(result.get("error") or result.get("error_kind") or "").strip()
    reason = str(result.get("reason") or result.get("error_summary") or "").strip()
    preflight_guard = bool(result.get("preflight_guard"))
    instruction = str(result.get("instruction") or "").strip()
    result_items = result.get("results") or []
    still_items = result.get("still_running") or []

    completed_ids: list[str] = list(result.get("completed_task_ids") or [])
    failed_ids: list[str] = list(result.get("failed_task_ids") or [])
    missing_by_task = result.get("missing_outputs_by_task") or {}
    for item in result_items:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("task_id") or "").strip()
        if not tid or tid in completed_ids or tid in failed_ids:
            continue
        terminal = str(item.get("terminal_reason") or "").strip().lower()
        ok = item.get("ok")
        outputs = item.get("outputs_check") or {}
        outputs_complete = outputs.get("outputs_complete") if isinstance(outputs, dict) else None
        outputs_missing = outputs.get("outputs_missing") if isinstance(outputs, dict) else None
        if ok is True and outputs_complete is not False and not outputs_missing:
            completed_ids.append(tid)
        elif ok is False or outputs_missing or terminal in {"failed", "interrupted", "stuck", "timeout", "crashed", "resource_required", "quality_blocked", "outputs_missing"}:
            failed_ids.append(tid)
        if outputs_missing:
            missing_by_task[tid] = outputs_missing

    running_ids: list[str] = list(result.get("running_task_ids") or [])
    for item in still_items:
        if isinstance(item, dict):
            tid = str(item.get("task_id") or "").strip()
        else:
            tid = str(item).strip()
        if tid and tid not in running_ids:
            running_ids.append(tid)

    parts = [
        "delegate_result:",
        f"action={action}" if action else "",
        f"error={error}" if error else "",
        f"reason={reason[:500]}" if reason else "",
        f"instruction={instruction[:500]}" if instruction else "",
        "event_fact=helper_blocked_before_start" if preflight_guard or error == "guard_blocked" else "",
        "state=blocked_not_running" if preflight_guard or error == "guard_blocked" else "",
        f"requested={requested}",
        f"returned_result_count={returned}",
        f"success_count={success}",
        f"running_count={running}",
        f"unavailable_count={unavailable}",
        f"task_ok={task_ok}" if task_ok is not None else "",
        f"completed_task_ids={', '.join(completed_ids)}" if completed_ids else "",
        f"failed_task_ids={', '.join(failed_ids)}" if failed_ids else "",
        f"running_task_ids={', '.join(running_ids)}" if running_ids else "",
        f"event_focus_task_ids={', '.join(dict.fromkeys(completed_ids + failed_ids + running_ids))}" if (completed_ids or failed_ids or running_ids) else "",
        f"outputs_missing={json.dumps(missing_by_task, ensure_ascii=False)}" if missing_by_task else "",
        "label_policy=task ids, filenames, paths, and technical terms are stable labels; preserve them if named",
        "truth_scope=preflight_guard means the requested helpers were blocked before startup and are not running; success_count and completed_task_ids are successful helpers; returned_result_count only means helper results were collected; running_task_ids are still running; failed_task_ids and outputs_missing are blockers or recovery facts.\n\npreflight_guard 表示 helper 未启动。",
        "wording_hint=if event_fact=helper_blocked_before_start is present, say the plan is being adjusted rather than saying helpers are running.\n\n被拦截时说正在调整方案。",
    ]
    return "\n".join(p for p in parts if p)


def _summarize_todo_write_result_payload(result: dict) -> str:
    counts = result.get("counts") or {}
    if not isinstance(counts, dict):
        counts = {}
    todos = result.get("todos") or []
    in_progress: list[str] = []
    completed: list[str] = []
    pending_count = counts.get("pending")
    if isinstance(todos, list):
        for item in todos:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            status = str(item.get("status") or "").strip().lower()
            if status in {"in_progress", "running"}:
                in_progress.append(content[:120])
            elif status in {"completed", "done"}:
                completed.append(content[:120])

    parts = [
        "todo_result:",
        f"total={counts.get('total')}" if counts.get("total") is not None else "",
        f"completed_count={counts.get('completed')}" if counts.get("completed") is not None else "",
        f"in_progress_count={counts.get('in_progress')}" if counts.get("in_progress") is not None else "",
        f"pending_count={pending_count}" if pending_count is not None else "",
        f"plan_in_progress={'; '.join(in_progress[:3])}" if in_progress else "",
        f"plan_completed={'; '.join(completed[:3])}" if completed else "",
        "truth_scope=todo_write records the plan checklist only; it is not proof that helpers, commands, or parallel execution have started.\n\ntodo 只是计划清单。",
        "wording_hint=describe this as planning or checklist update unless separate helper or command facts show execution.\n\n没有执行事实时只说更新计划。",
    ]
    return "\n".join(p for p in parts if p)


_EVENT_HINTS = {
    "helper_start": (
        "A helper or subtask has been dispatched. The user may benefit from knowing that concrete work started. "
        "Mention the visible work area, not internal process names.\n\n"
        "已开始分工处理时，可按人设简短说明正在推进哪类工作。"
    ),
    "helper_done": (
        "A helper or subtask returned useful evidence or an artifact. Briefly summarize what became available "
        "and what you will do next. If specific completed task ids are present, name those ids or their visible work only.\n\n"
        "子任务已有结果时，可说明获得了什么证据或产物以及下一步。"
    ),
    "helper_exit": (
        "A helper process left the active registry. This is a lifecycle fact, not proof that the helper succeeded. "
        "Mention only that this branch returned for collection or status checking.\n\n"
        "helper 进程退出只是生命周期事实，不能当作成功完成。"
    ),
    "milestone": (
        "A meaningful milestone changed: planning upgraded, a slice landed, verification started, or evidence coverage changed. "
        "Report only the user-visible milestone and current direction.\n\n"
        "到达里程碑时说明可见进展和接下来的方向。"
    ),
    "stuck": (
        "Work has met repeated friction. A short in-persona note can tell the user you are adjusting approach. "
        "If the facts say a helper is runaway, stuck, or kill-suggested, say recovery or replanning is happening; "
        "make clear this is an abnormal branch, not ordinary progress or near completion.\n\n"
        "遇到反复阻力时说明正在调整办法，失控 helper 不能说成正常推进。"
    ),
    "helper_blocked": (
        "A helper delegation was blocked before startup. Treat the listed helpers as not running and not completed. "
        "Briefly say the plan is being adjusted; if the facts mention a framework or contract, describe that framework-first direction.\n\n"
        "分工在启动前被拦截时，说明这些 helper 没有运行，正在按事实调整方案。"
    ),
    "stage_start": (
        "A new planning or tool stage has just started. Only preparation has begun. "
        "A concise line may say that planning, inspection, or task breakdown is starting.\n\n"
        "阶段刚开始时只能说明开始规划、准备检查或准备拆分。"
    ),
    "breakthrough": (
        "Something that had been failing just succeeded. A short relief or next-check line is appropriate "
        "when the persona would naturally speak.\n\n"
        "突破失败点时可短暂报喜并说明下一步。"
    ),
    "long_silence": (
        "The task has been quiet for a while. Speak only if the persona and current context make a status line useful. "
        "Quiet personas can stay silent.\n\n"
        "长时间沉默后，只有有用且符合人设时才短暂报进度。"
    ),
    "scheduled": (
        "A routine progress opportunity occurred. Usually stay silent unless the persona is talkative or the user has waited. "
        "If speaking, describe visible progress.\n\n"
        "普通进度节点通常少说，必要时只说可见进展。"
    ),
}


_RUNNING_WORDS = (
    "正在",
    "进行中",
    "推进",
    "运行",
    "执行",
    "并行",
    "启动",
    "开始",
)
_COMPLETION_WORDS = (
    "已完成",
    "完成了",
    "已经完成",
    "全部完成",
    "交付",
    "生成了",
    "产出",
)


_STRUCTURED_LABEL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[_\-.][A-Za-z0-9]+)+\b")
_STRUCTURED_FACT_KEYS = (
    "helper_task",
    "blocked_task_ids",
    "completed_task_ids",
    "failed_task_ids",
    "running_task_ids",
    "event_focus_task_ids",
)
_INTERNAL_USER_VISIBLE_TERMS = (
    "helper",
    "delegate",
    "producer",
    "producer-owned",
    "background_work",
    "background work",
    "processing_records",
    "processing record",
    "_helpers_shared",
    "_delegate_",
    ".helper_",
    "agent_state",
    "toolchain",
    "后台任务",
    "round1",
    "round2",
    "round3",
    "veryhard",
    "medium_coding",
    "模型池",
    "模型档",
    "档位",
    "规划强度",
    "非常困难",
)


def _public_stage_name(stage: str) -> str:
    normalized = (stage or "").strip().lower()
    if normalized in {"round1", "round2", "round3", "planning"}:
        return {
            "round1": "routing",
            "round2": "planning_and_tools",
            "round3": "final_response",
            "planning": "planning_and_tools",
        }[normalized]
    return normalized or "workflow"


def _main_milestone_public_fact(milestone: str, message: str) -> tuple[str, str, str]:
    """Map internal milestone names to user-facing facts for feedback LLM input."""

    lower = (milestone or "").lower()
    if "upgrade" in lower:
        return (
            "planning_recheck_started",
            "rechecking_with_stronger_evidence",
            "The approach is being tightened and prior work will be rechecked before continuing.",
        )
    if lower.endswith("_started"):
        return (
            "planning_stage_started",
            "planning_started",
            "A planning and tool-preparation stage has started.",
        )
    if lower.endswith("_done") or lower in {"done", "complete", "completed"}:
        return (
            "planning_handoff_ready",
            "handoff_ready",
            "Planning and tool work reached a handoff point for the final response.",
        )
    clean_message = (message or "").strip()
    if "round" in clean_message.lower() or "veryhard" in clean_message.lower():
        clean_message = "A workflow milestone changed."
    return ("main_milestone", "milestone", clean_message[:300])


def _sanitize_progress_visible_text(text: str, *, limit: int = 500) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return ""
    replacements = (
        (r"_helpers_shared[/\\][^\s,;，。]+", "shared work evidence"),
        (r"_delegate_[^\s,;，。]+", "work branch"),
        (r"\.helper_[^\s,;，。]+", "work record"),
        (r"\bhelper\b", "work branch"),
        (r"\bdelegate\b", "work coordination"),
        (r"\bagent_state\b", "task ledger"),
        (r"\btoolchain\b", "tool workflow"),
        (r"\bRound\s*[123]\b", "workflow stage"),
        (r"\bround\s*[123]\b", "workflow stage"),
        (r"\bveryhard\b", "harder planning mode"),
        (r"\bmedium_coding\b", "implementation planning mode"),
    )
    for pattern, repl in replacements:
        value = re.sub(pattern, repl, value, flags=re.IGNORECASE)
    if len(value) > limit:
        value = value[:limit].rstrip() + "..."
    return value


def _public_progress_event_facts(recent_work: str) -> str:
    """Convert structured workflow facts into model-visible progress facts.

    The raw lifecycle facts are still used by local validation. The generation
    LLM only needs user-level state, visible work, and truth boundaries.
    """

    lines: list[str] = []
    for raw_line in (recent_work or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "=" not in line:
            if line.endswith(":"):
                label = line[:-1].strip()
                if label == "delegate_result":
                    lines.append("work_result_summary:")
                elif label == "todo_result":
                    lines.append("plan_checklist_summary:")
                else:
                    lines.append(_sanitize_progress_visible_text(line, limit=300))
            else:
                lines.append(_sanitize_progress_visible_text(line, limit=300))
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        sanitized = _sanitize_progress_visible_text(value)
        if key == "event_fact":
            mapping = {
                "helper_started": "work_started",
                "helper_process_exited": "work_branch_returned",
                "helper_blocked_before_start": "work_blocked_before_start",
                "helper_progress": "work_progress",
                "helper_runaway_or_stuck": "work_branch_needs_recovery",
            }
            lines.append(f"event_fact={mapping.get(value, value)}")
        elif key == "helper_task":
            lines.append(f"work_label={sanitized}")
        elif key == "helper_kind":
            lines.append(f"work_type={sanitized}")
        elif key == "blocked_task_ids":
            lines.append(f"blocked_work_labels={sanitized}")
        elif key == "blocked_helper_kinds":
            lines.append(f"blocked_work_types={sanitized}")
        elif key == "completed_task_ids":
            lines.append(f"completed_work_labels={sanitized}")
        elif key == "failed_task_ids":
            lines.append(f"failed_work_labels={sanitized}")
        elif key == "running_task_ids":
            lines.append(f"running_work_labels={sanitized}")
        elif key == "event_focus_task_ids":
            lines.append(f"event_focus_work_labels={sanitized}")
        elif key == "delegate_result":
            lines.append(f"work_result_summary={sanitized}")
        elif key == "tool":
            tool = sanitized
            if tool == "delegate":
                tool = "work coordination"
            elif tool == "todo_write":
                tool = "plan checklist"
            lines.append(f"operation={tool}")
        elif key == "recent_tools":
            lines.append(f"recent_operations={sanitized}")
        elif key == "workflow_kind":
            lines.append(f"workflow_event={sanitized}")
        elif key == "label_policy":
            lines.append("label_policy=technical labels, filenames, paths, and domain terms are stable labels; preserve them if named")
        elif key == "truth_scope":
            lines.append(f"truth_scope={sanitized}")
        elif key == "wording_hint":
            lines.append(f"wording_hint={sanitized}")
        else:
            lines.append(f"{key}={sanitized}")
    public_facts = "\n".join(line for line in lines if line.strip())
    return public_facts[:1800]


def _structured_labels_from_text(text: str) -> set[str]:
    return {m.group(0).strip() for m in _STRUCTURED_LABEL_RE.finditer(text or "") if m.group(0).strip()}


def _current_event_task_labels(facts: str) -> set[str]:
    labels: set[str] = set()
    for raw_line in (facts or "").splitlines():
        line = raw_line.strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() not in _STRUCTURED_FACT_KEYS:
            continue
        for part in re.split(r"[,;\s]+", value):
            token = part.strip()
            if token:
                labels.add(token)
    return labels


def _has_technical_novel_context(facts: str) -> bool:
    """Return whether `novel` appears as a technical adjective in event facts."""

    text = (facts or "").lower()
    if "novel" not in text:
        return False
    if any(word in text for word in ("fiction", "story", "literature", "book review")):
        return False
    technical_markers = (
        "data structure",
        "algorithm",
        "database",
        "index",
        "paper",
        "section",
        "benchmark",
        "framework",
        "contract",
        "analysis",
        "implementation",
        "module",
    )
    return any(marker in text for marker in technical_markers)


def validate_intermediate_feedback_message(
    *,
    message: str,
    recent_work: str,
    event: str,
) -> tuple[bool, str]:
    """Check a generated progress line against structured event facts."""

    msg = (message or "").strip()
    facts = (recent_work or "").lower()
    if not msg:
        return False, "empty_message"
    lowered_msg = msg.lower()
    if any(term in lowered_msg or term in msg for term in _INTERNAL_USER_VISIBLE_TERMS):
        return False, "internal_workflow_term_exposed"

    has_running = any(word in msg for word in _RUNNING_WORDS)
    has_completed = any(word in msg for word in _COMPLETION_WORDS)
    mentions_parallel = "并行" in msg or "parallel" in msg.lower()
    claims_parallel_running = any(
        phrase in msg
        for phrase in ("并行执行", "并行运行", "并行推进", "并行处理", "正在并行")
    ) or "parallel running" in msg.lower()

    current_task_labels = _current_event_task_labels(recent_work or "")
    message_structured_labels = _structured_labels_from_text(msg)
    if current_task_labels and message_structured_labels:
        allowed_labels = current_task_labels | _structured_labels_from_text(recent_work or "")
        stale_or_unknown = sorted(label for label in message_structured_labels if label not in allowed_labels)
        if stale_or_unknown:
            return False, "message_mentions_label_outside_current_event_facts"
    if _has_technical_novel_context(recent_work or "") and "小说" in msg:
        return False, "technical_term_mistranslated"

    if "event_fact=helper_blocked_before_start" in facts:
        adjusting = any(word in msg for word in ("调整", "重规划", "改方案", "重新规划", "修正方案", "暂阻", "先建", "先统一", "先补"))
        if has_completed or claims_parallel_running or (has_running and not adjusting):
            return False, "blocked_helper_described_as_running_or_complete"
        if "blocked" not in facts and "拦" not in msg and "调整" not in msg:
            return False, "blocked_helper_without_adjustment_wording"

    if "todo_result:" in facts:
        execution_facts = any(
            marker in facts
            for marker in (
                "event_fact=helper_started",
                "event_fact=helper_progress",
                "delegate_result:",
                "event_fact=main_tool_done\nstate=done\ntool=bash",
                "event_fact=main_tool_done\nstate=done\ntool=workspace",
            )
        )
        if not execution_facts and has_completed:
            return False, "todo_plan_described_as_completion"
        if not execution_facts and (has_running or mentions_parallel):
            return False, "todo_plan_described_as_execution"

    if "event_fact=planning_stage_started" in facts or "event_fact=round2_stage_started" in facts:
        if has_completed:
            return False, "stage_start_described_as_completion"
        if mentions_parallel or any(word in msg for word in ("已读", "读完", "已生成", "已建立", "已经建立", "正在进行算法", "正在扫描所有文件")):
            return False, "stage_start_described_as_execution"

    if "event_fact=planning_recheck_started" in facts:
        if has_completed:
            return False, "planning_recheck_described_as_completion"

    if "event_fact=helper_process_exited" in facts and "delegate_result:" not in facts:
        if has_completed:
            return False, "helper_lifecycle_exit_described_as_completion"

    if "running_count=0" in facts and "completed_count=0" in facts:
        if has_running or has_completed:
            return False, "zero_running_zero_completed_described_as_progress"

    if "failed_task_ids=" in facts or "outputs_missing" in facts:
        has_recovery_word = any(
            word in msg
            for word in (
                "失败",
                "缺失",
                "中断",
                "重试",
                "续作",
                "恢复",
                "调整",
                "未完成",
                "blocked",
                "missing",
                "failed",
                "retry",
                "resume",
            )
        )
        if has_completed and not has_recovery_word:
            return False, "failed_or_missing_delegate_described_as_complete"

    if (
        "event_fact=helper_runaway_or_stuck" in facts
        or "state=kill" in facts
        or "state=intervene" in facts
        or "state=stuck" in facts
    ):
        has_recovery_word = any(
            word in msg
            for word in (
                "调整",
                "恢复",
                "重试",
                "续作",
                "收束",
                "终止",
                "重新规划",
                "卡住",
                "异常",
                "卡顿",
                "遇阻",
                "改用",
                "拆",
                "recovery",
                "retry",
                "resume",
                "stuck",
            )
        )
        if has_completed or (has_running and not has_recovery_word):
            return False, "runaway_helper_described_as_normal_progress"

    if event == "helper_exit" and has_completed:
        return False, "helper_exit_described_as_completion"

    return True, ""


def _prefer_direct_decision(preference: float, event: str) -> bool | None:
    if preference <= 0.0:
        return False
    if preference >= 1.0:
        return True
    return None


def _intermediate_feedback_user_payload(
    *,
    persona: str,
    user_request: str,
    recent_work: str,
    event: str,
    event_hint: str,
    preference: float,
    direct: bool | None,
    stage: str,
    iteration: int | None,
) -> str:
    if direct is True:
        decision_instruction = (
            "The persona feedback preference is 1, so produce a brief progress message.\n\n"
            "中途回复倾向为 1：当前关键节点直接生成一句简短进度。"
        )
    else:
        decision_instruction = (
            f"The persona feedback preference is {preference:.2f}. Higher means more likely to speak. "
            "Decide whether a mid-task message is useful now using the progress, persona, preference, total runtime, and last successful update timing."
            "\n\n根据人设、进度、运行时长和上次回复时间判断是否需要中途回复。"
        )
    payload = {
        "current_event_facts": _public_progress_event_facts(recent_work or ""),
        "decision_instruction": decision_instruction,
        "event": event or "scheduled",
        "event_hint": event_hint,
        "iteration": iteration if iteration is not None else "",
        "persona": persona or "No explicit persona is available. Use a natural, concise assistant voice.",
        "preference": round(float(preference or 0.0), 3),
        "stage": _public_stage_name(stage),
        "user_request": (user_request or "")[:1200],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def generate_intermediate_feedback(
    *,
    persona: str,
    user_request: str,
    recent_work: str,
    event: str,
    preference: float,
    stage: str = "round2",
    iteration: int | None = None,
) -> str | None:
    """Return one user-visible mid-task message, or None for silence."""

    direct = _prefer_direct_decision(preference, event)
    if direct is False:
        return None
    force_reply = direct is True and event in {
        "helper_start",
        "helper_done",
        "helper_blocked",
        "milestone",
        "stage_start",
        "breakthrough",
        "stuck",
    }

    event = event or "scheduled"
    event_hint = _EVENT_HINTS.get(event, _EVENT_HINTS["scheduled"])
    base_user_payload = _intermediate_feedback_user_payload(
        persona=persona,
        user_request=user_request,
        recent_work=recent_work,
        event=event,
        event_hint=event_hint,
        preference=preference,
        direct=direct,
        stage=stage,
        iteration=iteration,
    )
    try:
        from app.llm.model_pool import resolve_task

        spec = resolve_task("progress_message")
        messages = [
            {"role": "system", "content": INTERMEDIATE_FEEDBACK_SYSTEM},
            {"role": "user", "content": base_user_payload},
        ]
        last_reason = ""
        for attempt in range(2):
            raw: Any = await llm.chat_json(
                messages,
                model_spec=spec,
                metrics_tag="json.progress_message",
            )
            if not force_reply and direct is not True and not bool(raw.get("should_reply")):
                return None
            msg = str(raw.get("message") or "").strip()
            ok, reason = validate_intermediate_feedback_message(
                message=msg,
                recent_work=recent_work,
                event=event,
            )
            if ok:
                return msg
            last_reason = reason
            repair_payload = json.loads(base_user_payload)
            repair_payload["consistency_repair"] = (
                f"The previous candidate conflicted with the event facts: {reason}. "
                "Regenerate one truthful line from public current_event_facts only. "
                "If a truthful useful line cannot be written, return {\"should_reply\": false, \"message\": \"\"}."
            )
            messages = [
                {"role": "system", "content": INTERMEDIATE_FEEDBACK_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(
                        repair_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ]
        log.debug("intermediate feedback rejected after repair: %s", last_reason)
        return None
    except Exception:
        log.debug("intermediate feedback generation failed; staying silent", exc_info=True)
        return None
