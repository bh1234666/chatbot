"""Pending helper results and completion ledger state for delegate."""
from __future__ import annotations

import asyncio
from collections import deque
import logging

from app.core import agent_state
from app.core.core_processes import registry as proc_registry
from app.llm.tools.workspace_utils import _disk_result_for_collect

log = logging.getLogger(__name__)


_pending_results: dict[tuple[str, str], dict] = {}
_pending_results_lock = asyncio.Lock()

# 完成事件 — 让 wait_any 高效等待(不轮询)
# Key: (trace_id, task_id) → asyncio.Event
# spawn_async 时创建,helper 完成时 set,collect/wait_any 用 wait()
_completion_events: dict[tuple[str, str], asyncio.Event] = {}

_PENDING_RESULT_TTL = 1800.0  # 30 分钟。超过此时间还没 collect 的,自动清掉。

# 2026-05-11 Tier 1.A: Completion Ledger
# 主线程长跑(56+ 分钟)后会"忘记"已 done 的 helper(LLM 上下文太长),
# 又派同名 helper 浪费时间(实测 trace: abpt 16:04 done 后 16:09 又被 spawn)。
# Ledger 在 helper done 时双写, collect 后**也保留**, 供 status action 查询。
# 用 deque 保 per-trace 最近 N 个完成记录, 避免无限增长。
from collections import deque
_LEDGER_PER_TRACE_LIMIT = 50  # 每个 trace 最多保留 50 条完成记录
_completion_ledger: dict[str, "deque[dict]"] = {}
_completion_ledger_lock = asyncio.Lock()
_task_contracts: dict[str, dict[str, dict]] = {}
_task_contracts_lock = asyncio.Lock()

# 2026-05-15 P94: 跨批 benchmark 一致性检测的 registry
# 在 spawn 时记录每个 sibling task (产 results_*.csv 等) 的 bench dims,
# 后续批 spawn 时与历史 dims 对比, 防止跨批 inconsistency。
# 实测教训(00:00 排序论文 trace): batch1 (quick/merge/heap/acms) 4 helper, batch2
# (timsort/insertion) 2 helper。P92 只查批内 — batch1 内部不一致就够 P92 报警了,
# 但若 batch1 内一致而 batch2 与 batch1 不一致, P92 漏判。P94 维护跨批 registry。


async def _add_to_completion_ledger(
    trace_id: str, task_id: str, result: dict
) -> None:
    """把 helper 完成事件记录到 ledger,这是查询型 truth source。

    与 _pending_results 双写: pending 在 collect 后清空, ledger 永久保留 (per trace)。
    """
    import time as _t
    async with _completion_ledger_lock:
        if trace_id not in _completion_ledger:
            _completion_ledger[trace_id] = deque(maxlen=_LEDGER_PER_TRACE_LIMIT)
        # 摘要化记录(不存完整 report 防止内存膨胀)
        files = result.get("files", [])
        outputs_check = result.get("outputs_check")
        entry = {
            "task_id": task_id,
            "ts": _t.monotonic(),
            "ok": result.get("ok", False),
            "terminal_reason": result.get("terminal_reason", "?"),
            "elapsed_sec": result.get("elapsed_sec"),
            "delivered_count": len(files),
            "delivered_files": files[:20],  # 最多前 20 个文件名
            "delivered_summary": result.get("delivered_summary"),  # G: 分类
            "outputs_complete": (outputs_check or {}).get("outputs_complete"),  # C: 验收
            "outputs_missing": (outputs_check or {}).get("outputs_missing"),
            "internal_evidence_files": (result.get("internal_evidence_files") or [])[:20],
            "read_evidence_summary": result.get("read_evidence_summary"),
            # 2026-05-11 P12.I: quality_warnings 持久到 ledger 让主线程持续看到
            "quality_warnings": (outputs_check or {}).get("quality_warnings") or [],
            # 2026-05-11 Tier 2.F: 保留 retry_instruction / next_action 给主线程 loop 注入用
            "retry_instruction": result.get("retry_instruction"),
            "next_action_type": (result.get("next_action") or {}).get("type"),
            # 2026-05-15 P105: 记 kind + mode 便于检测同 task_id 反复换 kind 的不一致
            # (实测 trace 16:24 comp_verify_algos 一会儿 verify 一会儿 code, 主线程混淆)
            "kind": result.get("kind") or "code",
            "mode": result.get("mode") or "easy",
        }
        _completion_ledger[trace_id].append(entry)


def _get_completion_ledger(trace_id: str, last_n: int = 10) -> list[dict]:
    """读 ledger,返回最近 last_n 个完成事件(不阻塞,直接读 deque)。"""
    dq = _completion_ledger.get(trace_id)
    if not dq:
        return []
    # deque 切片用 list 转换
    return list(dq)[-last_n:]


def _clean_contract_list(value, *, max_items: int = 60) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        raw_items = [line.strip(" -\t") for line in value.splitlines()]
    elif isinstance(value, dict):
        raw_items = [f"{k}: {v}" for k, v in value.items()]
    elif isinstance(value, (list, tuple, set)):
        raw_items = [str(item).strip() for item in value]
    else:
        raw_items = [str(value).strip()]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item[:500])
        if len(out) >= max_items:
            break
    return out


async def _record_task_contracts(trace_id: str, tasks: list[dict]) -> None:
    """Remember the last helper envelope per trace/task for later resume calls.

    The completion ledger records terminal facts. This contract ledger records the
    request boundary chosen by the main process so a resume call that omits stable
    envelope fields can continue the same task without losing framework, files, or
    acceptance checks.
    """
    if not trace_id or not tasks:
        return
    import time as _t
    contracts = _task_contracts.setdefault(trace_id, {})
    async with _task_contracts_lock:
        contracts = _task_contracts.setdefault(trace_id, {})
        for task in tasks:
            task_id = str(task.get("task_id") or "").strip()
            if not task_id:
                continue
            contracts[task_id] = {
                "task_id": task_id,
                "ts": _t.monotonic(),
                "kind": str(task.get("kind") or "").strip(),
                "mode": str(task.get("mode") or "").strip(),
                "framework": str(task.get("framework") or "").strip()[:4000],
                "input_files": _clean_contract_list(
                    task.get("input_files") or task.get("source_files") or task.get("files"),
                    max_items=80,
                ),
                "expected_outputs": _clean_contract_list(task.get("expected_outputs"), max_items=80),
                "acceptance_checks": _clean_contract_list(
                    task.get("acceptance_checks") or task.get("checks"),
                    max_items=40,
                ),
            }
        # Keep memory bounded when a trace creates many semantic task ids.
        if len(contracts) > _LEDGER_PER_TRACE_LIMIT * 4:
            oldest = sorted(contracts.items(), key=lambda item: item[1].get("ts", 0))
            for key, _ in oldest[: len(contracts) - _LEDGER_PER_TRACE_LIMIT * 4]:
                contracts.pop(key, None)


def _get_task_contract(trace_id: str, task_id: str) -> dict | None:
    if not trace_id or not task_id:
        return None
    contract = (_task_contracts.get(trace_id) or {}).get(task_id)
    return dict(contract) if isinstance(contract, dict) else None


def _count_task_id_attempts(trace_id: str, task_id: str) -> int:
    """统计同 task_id 在 ledger 中出现的次数(完成事件 = spawn 次数).

    用途: P14.C repeated-attempt warning — 多次未完成时提示主线程升级、拆分或调整策略。
    """
    dq = _completion_ledger.get(trace_id)
    if not dq:
        return 0
    return sum(1 for e in dq if e.get("task_id") == task_id)


async def _store_pending_result(
    trace_id: str, task_id: str, result: dict, helper_task: asyncio.Task,
) -> None:
    """helper 完成时调用:把 result 存进缓存,并 set 完成事件。"""
    import time as _t
    key = (trace_id, task_id)
    async with _pending_results_lock:
        _pending_results[key] = {
            "result": result,
            "completed_at": _t.monotonic(),
            "helper_task": helper_task,
        }
        ev = _completion_events.get(key)
        if ev is None:
            ev = asyncio.Event()
            _completion_events[key] = ev
        ev.set()
        # 清过期项(惰性 GC)
        _cutoff = _t.monotonic() - _PENDING_RESULT_TTL
        _stale = [k for k, v in _pending_results.items() if v["completed_at"] < _cutoff]
        for k in _stale:
            _pending_results.pop(k, None)
            _completion_events.pop(k, None)
    # 2026-05-11 Tier 1.A: 同时写 ledger(不阻塞主路径,异常隔离)
    try:
        await _add_to_completion_ledger(trace_id, task_id, result)
    except Exception as e:
        log.warning("completion ledger write failed: %r", e)
    try:
        agent_state.register_helper_result(trace_id, task_id, result)
    except Exception as e:
        log.warning("agent state helper result write failed: %r", e)


async def _consume_pending_result(trace_id: str, task_id: str) -> dict | None:
    """主线程 collect 时调用:取出 result 并清缓存。返回 None 表示"还没完成"。"""
    key = (trace_id, task_id)
    async with _pending_results_lock:
        entry = _pending_results.pop(key, None)
        ev = _completion_events.get(key)
        if ev is not None:
            ev.clear()
    return entry["result"] if entry else None


def _ledger_result_for_collect(trace_id: str, task_id: str) -> dict | None:
    for entry in reversed(_get_completion_ledger(trace_id, last_n=_LEDGER_PER_TRACE_LIMIT)):
        if entry.get("task_id") != task_id:
            continue
        result = {
            "task_id": task_id,
            "ok": bool(entry.get("ok")),
            "terminal_reason": entry.get("terminal_reason") or "completed",
            "elapsed_sec": entry.get("elapsed_sec"),
            "files": entry.get("delivered_files") or [],
            "delivered_summary": entry.get("delivered_summary"),
            "outputs_check": {
                "outputs_complete": entry.get("outputs_complete"),
                "outputs_missing": entry.get("outputs_missing"),
                "quality_warnings": entry.get("quality_warnings") or [],
            },
            "internal_evidence_files": entry.get("internal_evidence_files") or [],
            "read_evidence_summary": entry.get("read_evidence_summary"),
            "report": (
                "Recovered from completion ledger after the helper finished but "
                "its pending result was no longer available. Use the delivered "
                "files/report artifacts listed here; do not respawn this task "
                "unless outputs are missing."
            ),
            "recovered_from": "completion_ledger",
        }
        retry_instruction = entry.get("retry_instruction")
        if retry_instruction:
            result["retry_instruction"] = retry_instruction
        next_action_type = entry.get("next_action_type")
        if next_action_type:
            result["next_action"] = {"type": next_action_type}
        return result
    return None


async def _recover_completed_result_for_collect(
    trace_id: str,
    task_id: str,
    *,
    main_owner: str,
    main_workspace: str | None = None,
) -> dict | None:
    h = await proc_registry().find_helper_by_task_id(
        task_id,
        same_trace_as=main_owner,
    )
    if h is not None:
        return None

    ledger_result = _ledger_result_for_collect(trace_id, task_id)
    if ledger_result is not None:
        return ledger_result

    disk_result = _disk_result_for_collect(task_id, main_workspace=main_workspace)
    if disk_result is not None:
        return disk_result

    if await proc_registry().was_recently_completed(task_id):
        return {
            "task_id": task_id,
            "ok": False,
            "terminal_reason": "completed_without_recoverable_result",
            "report": (
                "The helper is no longer active and the process registry marks "
                "this task as recently completed, but the pending result was "
                "not recoverable. This is not enough to accept the task as "
                "verified; inspect workspace artifacts or recover from a ledger "
                "entry if one exists."
            ),
            "files": [],
            "outputs_check": {
                "outputs_complete": False,
                "outputs_missing": [],
                "quality_warnings": [],
            },
            "recovered_from": "process_registry_recent_completion",
        }
    return None


async def _peek_pending_result(trace_id: str, task_id: str) -> dict | None:
    """poll 用:不消费,只看是否完成。"""
    async with _pending_results_lock:
        entry = _pending_results.get((trace_id, task_id))
    return entry["result"] if entry else None


def _ensure_completion_event(trace_id: str, task_id: str) -> asyncio.Event:
    """spawn_async 时创建对应 event,collect/wait_any 用它等。"""
    key = (trace_id, task_id)
    ev = _completion_events.get(key)
    if ev is None:
        ev = asyncio.Event()
        _completion_events[key] = ev
    return ev
