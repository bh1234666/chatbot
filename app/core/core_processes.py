"""
进程注册表 — 全局追踪所有活跃的 helper LLM 任务和 workspace 子进程。

设计目的:
  1. 让 LLM 能查询/杀死自己创建的进程,实现细粒度资源管理
  2. 严格 owner-based ACL:任何 LLM 只能管自己创立的资源
  3. 主线程是特权 owner,可以 kill 任何 helper(已有 abort_event 即此)

Owner ID 约定:
  - 主线程: "main:{trace_id}"  例如 "main:abc12345"
  - helper:  "helper:{trace_id}:{task_id}"  例如 "helper:abc12345:radix_sort"

进程类型:
  - "helper":     LLM helper task (asyncio.Task,通过 abort_event 协作中断)
  - "subprocess": workspace.run/python.exec 启动的 OS 子进程

线程安全:
  本模块用 asyncio.Lock 保护内部状态,单线程 asyncio 环境下足够。
  跨进程不通用——多 worker 部署需要外部协调(目前 starbot 单进程)。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("processes")

# ─── 常量 ────────────────────────────────────────────────
MAIN_OWNER_PREFIX = "main:"
HELPER_OWNER_PREFIX = "helper:"

PROC_TYPE_HELPER = "helper"
PROC_TYPE_SUBPROCESS = "subprocess"

# ─── Kill Gate: 所有 helper kill 必须提供合法 reason ────────
# 2026-05-05: 从 delegate.py 下沉到 processes.py,确保所有 kill 路径
# (delegate.kill / processes.kill / bg_force_kill / resume_race_protect)
# 都经过同一道门禁,不因入口工具不同而有差异。

# 四种允许的 kill 条件
KILL_REASON_SELF_CANT_DO = "self_report_cant_do"
KILL_REASON_SELF_DONE = "self_report_done"
KILL_REASON_SIBLING_DONE = "sibling_completed_first"
KILL_REASON_CONTENT_USELESS = "content_deemed_useless"
# 紧急例外:API 层卡死(60s 无流式 chunk)
KILL_REASON_API_STALL = "api_stall_emergency"

_VALID_KILL_REASONS: dict[str, str] = {
    KILL_REASON_SELF_CANT_DO: "helper自己报告无法完成任务",
    KILL_REASON_SELF_DONE: "helper自己报告任务完成",
    KILL_REASON_SIBLING_DONE: "同类helper先完成了相同任务",
    KILL_REASON_CONTENT_USELESS: "主进程判定内容已无用(需求变更/已有更好替代)",
    KILL_REASON_API_STALL: "API卡死超时(60s无流式chunk),紧急重试",
}


def validate_kill_reason(
    task_id: str,
    reason: str,
    *,
    helper_handle: Optional["ProcessHandle"] = None,
) -> tuple[bool, str]:
    """统一 kill gate: 校验 reason 是否在允许条件内。
    所有 helper kill 操作必须通过此校验。禁止任何路径绕过。

    2026-05-08 加语义校验(只对 api_stall_emergency 严格): 实测 LLM 用此 reason
    杀心跳健康(iter 在涨)的 helper, 校验若提供 helper_handle 就核对 last_progress_at。
    其他 reason 维持白名单匹配 (LLM 自报告依据 helper report 文本判断, 校验代价大)。

    Args:
        task_id: 目标 helper 的 task_id (仅用于日志)
        reason: kill 原因
        helper_handle: 可选 ProcessHandle, 提供则做语义校验

    Returns (allowed, message).
    """
    if reason not in _VALID_KILL_REASONS:
        return False, (
            f"kill DENIED for {task_id}: reason={reason!r} 不在允许条件内。"
            f" 有效reason: {list(_VALID_KILL_REASONS.keys())}"
        )
    # ── 语义校验: api_stall_emergency 必须真的 stall ──
    # api_stall = API 层 60s 无 chunk; 用 last_progress_at 反映 helper 心跳。
    # 心跳新鲜(<30s)说明 helper 在干活, 不该用此 reason 杀; 引导改用其他 reason。
    if reason == KILL_REASON_API_STALL and helper_handle is not None:
        try:
            import time as _t
            last_progress = helper_handle.last_progress_at or 0.0
            if last_progress > 0:
                age = _t.time() - last_progress
                if age < 30.0:
                    return False, (
                        f"kill DENIED for {task_id}: reason='api_stall_emergency' "
                        f"but the helper heartbeat is fresh (last_progress_age={age:.1f}s < 30s). "
                        "Fresh heartbeat means the helper is still thinking or using tools, not an API stall. "
                        "Use reason='content_deemed_useless' when the main process no longer needs this output, "
                        "or use a longer wait_window_sec when waiting is still appropriate. "
                        "心跳新鲜代表 helper 仍在工作，不属于 API stall；按需中断或延长等待。"
                    )
        except (AttributeError, TypeError):
            pass  # ProcessHandle 字段缺失时 fallback 到白名单
    return True, f"kill allowed ({task_id}): {_VALID_KILL_REASONS[reason]}"


# ─── Data Class ──────────────────────────────────────────
@dataclass
class ProcessHandle:
    """单个进程的注册信息。"""
    proc_id: str            # 短 uuid (10 chars), 给 LLM 看的 ID
    proc_type: str          # "helper" or "subprocess"
    owner: str              # 创建者 owner_id
    created_at: float
    description: str = ""   # 给 LLM 看的简短描述

    # for subprocess:
    pid: Optional[int] = None
    command: Optional[str] = None       # 截断到 100 字符
    workspace_dir: Optional[str] = None
    proc_obj: Optional[object] = None   # asyncio.subprocess.Process 引用,用于 kill

    # for helper:
    helper_task: Optional[asyncio.Task] = None
    helper_task_id: Optional[str] = None      # delegate 里用的 task_id
    helper_workspace: Optional[str] = None
    abort_event: Optional[asyncio.Event] = None
    helper_kind: str = ""
    archive_id: str = ""
    group_id: str = ""
    user_id: str = ""
    parent_helper_task_id: Optional[str] = None   # 谁 spawn 了我(用于深度计算)

    # ── helper 实时进度心跳 (2026-05-02 加, 配合 timeout 30min 提供观测性) ──
    # 主线程通过 processes.list 看到这些字段,判断"helper 在工作还是卡住",
    # 决定要不要 kill。没有这套字段就是失明 30 分钟。
    last_progress_at: float = 0.0           # 上次更新心跳的时间 (time.time())
    last_iter: int = 0                       # helper 当前 iter 数
    recent_tools: list[str] = field(default_factory=list)   # 最近 N 次调的工具名 (≤8)
    last_thought_preview: str = ""           # 最近一次 assistant 思考截断 (≤200 字符)
    last_progress_note: str = ""             # 自由格式状态字段 (≤120 字符)
    progress_summary: str = ""              # lite 模型每 15s 生成的中文进度摘要

    def update_helper_progress(
        self,
        *,
        iter_num: int | None = None,
        tool_name: str | None = None,
        thought: str | None = None,
        note: str | None = None,
    ) -> None:
        """更新 helper 实时进度心跳。线程不安全,调用方需在持有 registry lock 时调,
        或者保证仅 helper 自己写 / registry list_owned_by 只读访问(单 asyncio loop 下安全)。
        """
        self.last_progress_at = time.time()
        if iter_num is not None:
            self.last_iter = iter_num
        if tool_name is not None:
            # 保留最近 8 个工具名
            self.recent_tools.append(tool_name)
            if len(self.recent_tools) > 8:
                del self.recent_tools[: len(self.recent_tools) - 8]
        if thought is not None:
            self.last_thought_preview = thought[:200]
        if note is not None:
            self.last_progress_note = note[:120]

    def to_public_dict(self) -> dict:
        """LLM 可见的安全字段。"""
        d = {
            "proc_id": self.proc_id,
            "proc_type": self.proc_type,
            "trace_id": ProcessRegistry.trace_id_of(self.owner) or "",
            "elapsed_seconds": round(time.time() - self.created_at, 1),
            "description": self.description,
        }
        if self.proc_type == PROC_TYPE_SUBPROCESS:
            d["pid"] = self.pid
            d["command"] = (self.command or "")[:100]
            if self.pid:
                try:
                    import psutil

                    root = psutil.Process(self.pid)
                    procs = [root] + root.children(recursive=True)
                    rss = 0
                    for proc in procs:
                        try:
                            rss += int(proc.memory_info().rss)
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            continue
                    d["rss_gib"] = round(rss / (1024 * 1024 * 1024), 3)
                    d["process_tree_count"] = len(procs)
                except Exception:
                    pass
        else:
            d["task_id"] = self.helper_task_id
            d["helper_kind"] = self.helper_kind
            d["archive_id"] = self.archive_id
            d["group_id"] = self.group_id
            d["user_id"] = self.user_id
            # ── helper 实时心跳信息 (主线程用以判断是否要 kill) ──
            now = time.time()
            d["iter"] = self.last_iter
            d["recent_tools"] = list(self.recent_tools[-4:])  # L7-1: 限 4 个节省 token
            d["last_thought"] = self.last_thought_preview
            d["last_note"] = self.last_progress_note
            d["progress_summary"] = self.progress_summary
            # L1-2 (2026-05-09): 增强心跳字段
            d["what_doing"] = _compute_what_doing(self)
            d["estimated_remaining_sec"] = _compute_remaining_estimate(self)
            d["wait_or_continue"] = _compute_wait_or_continue(self)
            # 2026-05-11 B3: iter runaway 显式标记,让主线程一眼看出"该 kill"
            if self.last_iter > 100:
                d["_runaway"] = True
                d["_runaway_reason"] = f"iter {self.last_iter} > 100 (几乎必死循环)"
            elif self.last_iter > 80 and not _helper_has_files(self):
                d["_runaway"] = True
                d["_runaway_reason"] = (
                    f"iter {self.last_iter} > 80 且无产出文件(疑似兜圈),"
                    f"建议协作 kill 让 helper 出进度报告,再按报告同 task_id resume 或 mode='hard' 续作"
                )
            if self.last_progress_at > 0:
                age = round(now - self.last_progress_at, 1)
                d["last_heartbeat_age_sec"] = age
                # 心跳超过 90 秒视为可疑(LLM 单轮通常 30-60s),120s 视为停滞
                d["heartbeat_status"] = (
                    "stale" if age > 120
                    else "slow" if age > 90
                    else "fresh"
                )
            else:
                d["last_heartbeat_age_sec"] = None
                d["heartbeat_status"] = "no_heartbeat_yet"
        return d


# ─── L1-2 (2026-05-09): 心跳增强辅助函数 ─────────────────

def _extract_progress_pct(handle: ProcessHandle | str) -> int | None:
    """从 progress_note 或 thought 中提取进度百分比(如 '65%' 或 '3/5')。"""
    import re as _re
    if isinstance(handle, str):
        sources = (handle, "")
    else:
        sources = (handle.last_progress_note, handle.last_thought_preview)
    for src in sources:
        if not src:
            continue
        m = _re.search(r"(\d+)\s*%", src)
        if m:
            return int(m.group(1))
        m = _re.search(r"(\d+)\s*/\s*(\d+)", src)
        if m:
            return int(100 * int(m.group(1)) / max(int(m.group(2)), 1))
    return None


def _compute_what_doing(handle: ProcessHandle | list[str], last_thought: str = "") -> str:
    """从最近工具 + progress_note 推断 helper 当前在做什么。"""
    if isinstance(handle, list):
        tools = handle[-3:]
        if not tools:
            return last_thought[:80] if last_thought else "idle"
        last_tool = tools[-1]
        note = last_thought[:60]
    else:
        tools = handle.recent_tools[-3:] if handle.recent_tools else []
        if not tools:
            return handle.last_progress_note[:80] if handle.last_progress_note else "启动中"
        last_tool = tools[-1]
        note = handle.last_progress_note[:60] if handle.last_progress_note else ""
    # 简单映射
    tool_map = {
        "edit_file": "编辑文件",
        "workspace": "执行命令",
        "python": "运行Python",
        "delegate": "派发子任务",
        "read_file": "阅读文件",
        "search_in_file": "搜索代码",
        "code_index": "检索符号",
        "office": "生成文档",
    }
    action = tool_map.get(last_tool, last_tool)
    return f"{action}" + (f": {note}" if note else "")


def _compute_remaining_estimate(handle: ProcessHandle = None, *, iter: int | None = None, elapsed: float | None = None, last_thought: str = "") -> int | None:
    """估算 helper 剩余时间(秒)。基于 iter 增速和进度百分比。"""
    if handle is None:
        if iter is None or elapsed is None:
            return None
        if iter <= 0 or elapsed <= 0:
            return None
        return max(1, int(elapsed / max(iter, 1)))
    pct = _extract_progress_pct(handle)
    if pct is not None and pct > 0:
        elapsed = time.time() - handle.created_at
        if elapsed > 10:
            est_total = elapsed * 100 / pct
            remaining = int(est_total - elapsed)
            return max(0, remaining)
    return None


def _compute_wait_or_continue(handle: ProcessHandle = None, *, last_heartbeat_age_sec: float | None = None, iter: int | None = None, recent_tools: list[str] | None = None) -> str:
    """建议主线程: 继续等(wait)还是介入(kill/resume)。

    2026-05-11 B3 加: iter runaway 检测。
    实测 trace 822f2aaa: bptree iter 100, rbtree iter 94 主线程都没 kill,
    单 helper 死循环 25+ 分钟。新规则:
      - iter > 80 + 心跳活跃但无产出文件 → "kill" (建议主线程介入)
      - iter > 50 + elapsed > 600s + 心跳活跃 → "check" (主动查看)
      - iter > 100 + 任何状态 → "kill" 强烈建议(单 helper 不该超过 100 iter)
    """
    if handle is None:
        age = last_heartbeat_age_sec
        if age is not None:
            if age > 120:
                return "kill"
            if age > 90:
                return "check"
        if iter is not None and iter > 100:
            return "kill"
        return "wait"
    if handle.last_progress_at > 0:
        age = time.time() - handle.last_progress_at
        if age > 120:
            return "kill"  # 心跳过旧
        if age > 90:
            return "check"  # 可疑,需主动查
    # 2026-05-11 B3 新增 iter-based 检测
    iter_count = handle.last_iter or 0
    elapsed = time.time() - handle.created_at if handle.created_at else 0
    # 100+ iter 几乎必死循环
    if iter_count > 100:
        return "kill"
    # 80+ iter + 长跑 + 没产出文件 = 兜圈
    if iter_count > 80 and not _helper_has_files(handle):
        return "kill"
    # 50+ iter + 10min + 心跳活但慢 = 检查
    if iter_count > 50 and elapsed > 600:
        return "check"
    pct = _extract_progress_pct(handle)
    if pct is not None:
        if pct < 10 and handle.last_iter > 20:
            return "check"  # 跑了很多 iter 但进度 < 10%
    return "wait"


def _helper_has_files(handle: ProcessHandle) -> bool:
    """检测 helper 是否已产出文件(用于判断"iter 多但没干活")。

    检查 helper 的 workspace 目录里有没有产物文件。
    判定保守:任何非 dotfile / 非内部记录文件都算"有产出"。
    内部记录文件清单(不算):
      .helper_summary.txt / .session_tag / .read_history.json / .edit_history.json
      / .rewrite_count.json / .todo.json / Makefile 这类编译副产物可视为产物
    """
    _INTERNAL_FILES = {
        ".helper_summary.txt",
        ".session_tag",
        ".read_history.json",
        ".edit_history.json",
        ".rewrite_count.json",
        ".todo.json",
    }
    try:
        ws = getattr(handle, "workspace_dir", None) or getattr(handle, "ws_dir", None)
        if not ws:
            return False
        import os
        if not os.path.isdir(ws):
            return False
        for name in os.listdir(ws):
            # 跳过 dotfile / 内部记录文件
            if name.startswith("."):
                continue
            if name in _INTERNAL_FILES:
                continue
            # 跳过空目录? 不,只要 listdir 列到就算
            # 任何其他文件/目录都算产出(.c/.py/.txt/.json/.exe/result/ 任意类型)
            return True
        return False
    except (OSError, AttributeError):
        return False


# ─── Registry ────────────────────────────────────────────
class ProcessRegistry:
    """单例注册表。"""

    def __init__(self):
        self._procs: dict[str, ProcessHandle] = {}
        self._lock = asyncio.Lock()
        # task_id → time.time() — 记录最近被 kill 的 helper，用于 kill 幂等
        self._recently_killed: dict[str, float] = {}
        # task_id → time.time() — 记录最近完成(ok=true)的 helper，防止 LLM 重复 spawn
        self._recently_completed: dict[str, float] = {}
        # 2026-05-08 Fix(Bug 6 ext): proc_id → {task_id, proc_type, at, reason}
        # 记录最近从 _procs 移除的 proc_id, 让 processes.kill / 任何按 proc_id
        # 的查询能在"刚结束"窗口内给出友好响应而不是 ERROR(LLM 看到 ERROR 会重试)。
        self._recently_gone_procs: dict[str, dict] = {}

    @staticmethod
    def _new_proc_id() -> str:
        # 10 字符紧凑 uuid——足够区分 + LLM 容易复制
        return uuid.uuid4().hex[:10]

    @staticmethod
    def is_main_owner(owner: str) -> bool:
        return owner.startswith(MAIN_OWNER_PREFIX)

    @staticmethod
    def make_main_owner(trace_id: str) -> str:
        return f"{MAIN_OWNER_PREFIX}{trace_id}"

    @staticmethod
    def make_helper_owner(trace_id: str, task_id: str) -> str:
        return f"{HELPER_OWNER_PREFIX}{trace_id}:{task_id}"

    @staticmethod
    def helper_depth(owner: str) -> int:
        """返回 helper 的递归深度。
        主线程: 0
        helper: 1 (主线程 spawn 的)
        helper-spawned helper: 2
        ...

        从 owner 字符串算 — 每多一层 spawn 就在 task_id 末尾加 "_fork" 后缀,
        所以数 fork 个数即可(简单可靠)。
        """
        if owner.startswith(MAIN_OWNER_PREFIX):
            return 0
        # helper:trace_id:task_id, task_id 可能含 .fork 后缀
        try:
            task_part = owner.split(":", 2)[2]
        except IndexError:
            return 1
        return 1 + task_part.count(".fork")

    @staticmethod
    def trace_id_of(owner: str) -> Optional[str]:
        """从 owner 字符串提取 trace_id(主进程 / helper 都适用)。

        多用户并发场景下,trace_id 用于 ACL 隔离 — 主线程 A 不应该能看到/杀死
        主线程 B 的 helper(即使两者都是 main owner)。
        """
        if owner.startswith(MAIN_OWNER_PREFIX):
            return owner[len(MAIN_OWNER_PREFIX):]
        if owner.startswith(HELPER_OWNER_PREFIX):
            try:
                return owner.split(":", 2)[1]
            except IndexError:
                return None
        return None

    # ─── 注册 ─────────────────────────────────────────
    async def register_subprocess(
        self,
        *,
        owner: str,
        proc_obj: object,            # asyncio.subprocess.Process
        pid: Optional[int],
        command: str,
        workspace_dir: str,
    ) -> str:
        """注册一个 subprocess。返回 proc_id。"""
        async with self._lock:
            proc_id = self._new_proc_id()
            self._procs[proc_id] = ProcessHandle(
                proc_id=proc_id,
                proc_type=PROC_TYPE_SUBPROCESS,
                owner=owner,
                created_at=time.time(),
                description=f"subprocess: {command[:60]}",
                pid=pid,
                command=command,
                workspace_dir=workspace_dir,
                proc_obj=proc_obj,
            )
            return proc_id

    async def register_helper(
        self,
        *,
        owner: str,                       # 谁创建的(主线程 or 父 helper)
        task: asyncio.Task,
        helper_task_id: str,
        helper_workspace: str,
        abort_event: asyncio.Event,
        description: str = "",
        parent_helper_task_id: Optional[str] = None,
        helper_kind: str = "",
        archive_id: str = "",
        group_id: str = "",
        user_id: str = "",
    ) -> str:
        """注册 helper 到 registry。返回 proc_id。

        注意: 本方法不挂 task done_callback。生命周期管理(自动 unregister)
        交给上层 _register_helper_with_autoclean 包装器处理 — 保持注册表本身
        是纯数据结构,与 lifecycle hook 解耦,便于测试和 fixture。
        """
        async with self._lock:
            proc_id = self._new_proc_id()
            self._procs[proc_id] = ProcessHandle(
                proc_id=proc_id,
                proc_type=PROC_TYPE_HELPER,
                owner=owner,
                created_at=time.time(),
                description=description or f"helper: {helper_task_id}",
                helper_task=task,
                helper_task_id=helper_task_id,
                helper_workspace=helper_workspace,
                abort_event=abort_event,
                parent_helper_task_id=parent_helper_task_id,
                helper_kind=helper_kind,
                archive_id=archive_id,
                group_id=group_id,
                user_id=user_id,
            )
            return proc_id

    async def unregister(self, proc_id: str) -> Optional[ProcessHandle]:
        """清理进程登记。返回被移除的 handle(供调用方查最终状态)。"""
        async with self._lock:
            h = self._procs.pop(proc_id, None)
            if h is not None:
                # 2026-05-08 Fix(Bug 6 ext): 记录 proc_id → 上下文,
                # 供 kill 幂等检查使用。"natural" 表示自然结束(非 kill)。
                self._recently_gone_procs[proc_id] = {
                    "task_id": h.helper_task_id if h.proc_type == PROC_TYPE_HELPER else None,
                    "proc_type": h.proc_type,
                    "at": time.time(),
                    "reason": "natural",
                }
            return h

    # ─── 查询 ─────────────────────────────────────────
    async def list_owned_by(self, owner: str) -> list[dict]:
        """返回 owner 创建的所有活跃进程(LLM 可见字段)。

        主线程额外有完整 transparency:能看所有 helper + 所有 subprocess
        (含 owner 字段方便审计)。helper 只能看自己直接创建的(严格 ACL)。

        多用户并发隔离:主线程的 transparency 仅及于同 trace_id 的进程,
        不会跨用户看到别人的 helper(每个 round2 调用有独立 trace_id)。
        """
        async with self._lock:
            results = []
            is_main = self.is_main_owner(owner)
            my_trace = self.trace_id_of(owner)
            for h in self._procs.values():
                if h.owner == owner:
                    results.append(h.to_public_dict())
                elif is_main and self.trace_id_of(h.owner) == my_trace:
                    # 主线程能看到同 trace_id 的所有进程(含别人创建的 subprocess + helper)
                    # 跨 trace 的进程(其他用户的 round2)不可见 — multi-tenant ACL 隔离
                    d = h.to_public_dict()
                    d["owner"] = h.owner
                    results.append(d)
            # 按创建时间排序,新的在前
            results.sort(key=lambda x: x.get("elapsed_seconds", 0))
            return results

    async def list_all_public(self) -> list[dict]:
        """Return public process fields for external workflow monitors."""
        async with self._lock:
            results = []
            for h in self._procs.values():
                d = h.to_public_dict()
                d["owner"] = h.owner
                results.append(d)
        results.sort(key=lambda x: x.get("elapsed_seconds", 0))
        return results

    async def cancel_all_helpers_for_owner(self, owner: str) -> int:
        """2026-05-10 Patch 55: 取消 owner 下所有未完成的 helper。

        替代旧 P50 cancel_orphan_auto_spawned(只 cancel _auto_final/_verify 后缀)。
        用户原话:"主进程结束后向用户回复,此时所有子进程都应该结束,没有意义了"。

        触发场景:**chat 回合彻底结束**(orchestrator 的 finally 块):
          - 主进程已 yield 'done',用户已拿到回复
          - 任何还在跑的 helper 都没有意义(没人看产出)
          - 子进程"persist 跨回合"是错误用例 — 用户下次问相关任务时,主进程
            可以通过 resume=True 重新拉起 task 状态(workspace 仍在)

        与 P50 区别:
          - P50:只 cancel auto_spawned(命名约定后缀)
          - P55:cancel **所有** owner 名下 helper,不分系统派 vs 用户派
          - 触发时机也变:从 _dynamic_wait_loop 退出 → orchestrate finally

        识别规则:
          - h.owner == owner(主线程的 owner)
          - h.proc_type == "helper"
          - h.abort_event 未 set(还在跑)

        返回:被 cancel 的 helper 数量。

        ⚠️ 局限:此方法只 cancel **直接** owner 名下的 helper。如果 helper 内部又派了
        sub-helper(如历史自动 paired helper、helper 调 delegate spawn 子任务),
        sub-helper 的 owner 是 `helper:{trace_id}:{parent_task_id}`,**不是** main_owner,
        会漏掉。chat 回合结束彻底清场请用 cancel_all_helpers_in_trace。
        """
        cancelled = 0
        async with self._lock:
            for h in self._procs.values():
                if h.owner != owner:
                    continue
                if h.proc_type != "helper":
                    continue
                if h.abort_event is None or h.abort_event.is_set():
                    continue
                try:
                    h.abort_event.set()
                    cancelled += 1
                except Exception:
                    pass
        return cancelled

    async def cancel_all_helpers_in_trace(self, trace_id: str) -> int:
        """2026-05-10 Patch 59: chat 回合结束时按 trace_id cancel **整棵 helper 树**。

        病因(trace f973df3770544567):P55 在 finally 块调 cancel_all_helpers_for_owner
        (main_owner=main:{trace_id}),但 woat_impl_auto_final 是 P40 在 helper woat_impl
        内部派的 sub-helper,owner 是 helper:{trace_id}:woat_impl,**不是 main_owner**。
        cancel 只看 main_owner 漏掉了 sub-helper → woat_impl_auto_final 在 user.released
        后还跑了 12 分钟孤儿空跑(09:26 → 10:27),而 main 流程 10:15 就结束了。

        修法:owner 字符串嵌了 trace_id(`main:{trace_id}` / `helper:{trace_id}:{task_id}`),
        按 owner 字符串里是否含该 trace_id 过滤,cancel 所有深度的 helper。

        识别规则:
          - h.proc_type == "helper"
          - h.owner 含该 trace_id(任何深度,主线程 owner / 任意 helper owner)
          - h.abort_event 未 set(还在跑)
        """
        cancelled = 0
        # 主线程 owner 与 helper owner 的 trace_id 嵌入位置不同,但都是 ":<trace_id>"
        # 形式(main:<trace_id> 末尾 / helper:<trace_id>:<task> 中间)
        _trace_marker_main = f"main:{trace_id}"
        _trace_marker_helper = f"helper:{trace_id}:"
        async with self._lock:
            for h in self._procs.values():
                if h.proc_type != "helper":
                    continue
                _belongs = (
                    h.owner == _trace_marker_main
                    or h.owner.startswith(_trace_marker_helper)
                )
                if not _belongs:
                    continue
                if h.abort_event is None or h.abort_event.is_set():
                    continue
                try:
                    h.abort_event.set()
                    cancelled += 1
                except Exception:
                    pass
        return cancelled

    # 向后兼容别名(P50 旧名,逻辑已升级为 cancel 所有 helper)
    async def cancel_orphan_auto_spawned(self, owner: str) -> int:
        return await self.cancel_all_helpers_for_owner(owner)

    async def get(self, proc_id: str) -> Optional[ProcessHandle]:
        async with self._lock:
            return self._procs.get(proc_id)

    async def find_by_proc_id(self, proc_id: str) -> Optional[ProcessHandle]:
        """Compatibility alias for older helper lifecycle code."""
        return await self.get(proc_id)

    async def find_helper_by_task_id(
        self, helper_task_id: str, *, owner: Optional[str] = None,
        same_trace_as: Optional[str] = None,
        exclude_proc_id: Optional[str] = None,  # ── 2026-05-04 Bug #19 修复 ──
    ) -> Optional[ProcessHandle]:
        """按 task_id 查 helper。

        Args:
            helper_task_id: 目标 task_id
            owner: 严格 ACL — 仅匹配此 owner 创建的(helper 用此模式)
            same_trace_as: 仅匹配同 trace_id 的(主线程 kill helper 用此模式,
                防止跨用户误杀)。如果 owner 已设置,此参数被忽略。
            exclude_proc_id: 跳过此 proc_id。防止新 helper resume 时 find 到自己,
                把自己当"旧 helper"杀掉(Bug #19 self-kill)。

        Returns: 第一个匹配的 ProcessHandle,或 None
        """
        async with self._lock:
            target_trace = (
                None if owner is not None else
                (self.trace_id_of(same_trace_as) if same_trace_as else None)
            )
            for h in self._procs.values():
                if h.proc_type != PROC_TYPE_HELPER:
                    continue
                if h.helper_task_id != helper_task_id:
                    continue
                if exclude_proc_id is not None and h.proc_id == exclude_proc_id:
                    continue
                if owner is not None:
                    if h.owner == owner:
                        return h
                elif target_trace is not None:
                    if self.trace_id_of(h.owner) == target_trace:
                        return h
                else:
                    # 完全开放 — 内部 cleanup 用,生产 LLM 路径不应该走这里
                    return h
            return None

    async def was_recently_killed(
        self, task_id: str, *, within_sec: float = 60.0,
    ) -> bool:
        """检查 task_id 是否在最近 within_sec 秒内被 kill。
        供 kill handler 实现幂等——重复 kill 应返回友好成功而非 error。"""
        async with self._lock:
            killed_at = self._recently_killed.get(task_id)
            if killed_at is None:
                return False
            return (time.time() - killed_at) < within_sec

    async def was_recently_completed(
        self, task_id: str, *, within_sec: float = 600.0,
    ) -> bool:
        """检查 task_id 是否在最近 within_sec 秒内成功完成。
        防止 LLM 在 repair_pairing 混淆后重复 spawn 已完成的任务。"""
        async with self._lock:
            completed_at = self._recently_completed.get(task_id)
            if completed_at is None:
                return False
            return (time.time() - completed_at) < within_sec

    async def mark_recently_completed(self, task_id: str) -> None:
        """记录 task_id 已完成(ok=true)。"""
        async with self._lock:
            self._recently_completed[task_id] = time.time()

    async def count_active_helpers(self) -> int:
        async with self._lock:
            return sum(
                1 for h in self._procs.values()
                if h.proc_type == PROC_TYPE_HELPER
            )

    async def count_active_helpers_for_trace(self, owner: str) -> int:
        """Count active helpers in the same trace/agent as owner."""
        async with self._lock:
            trace_id = self.trace_id_of(owner)
            return sum(
                1 for h in self._procs.values()
                if h.proc_type == PROC_TYPE_HELPER and self.trace_id_of(h.owner) == trace_id
            )

    async def cancel_all_for_tests(self, *, timeout: float = 0.5) -> int:
        """Best-effort cleanup for test teardown.

        Normal request cleanup goes through trace/owner scoped cancellation so
        helpers can finish gracefully. Tests often monkeypatch delegate paths and
        leave registry handles behind; this helper force-cancels those handles
        and drains their tasks to avoid cross-test leakage.
        """
        async with self._lock:
            items = list(self._procs.items())
            self._procs.clear()

        tasks: list[asyncio.Task] = []
        cleaned = 0
        for proc_id, h in items:
            cleaned += 1
            self._recently_gone_procs[proc_id] = {
                "task_id": h.helper_task_id if h.proc_type == PROC_TYPE_HELPER else None,
                "proc_type": h.proc_type,
                "at": time.time(),
                "reason": "test_cleanup",
            }
            if h.proc_type == PROC_TYPE_HELPER:
                if h.abort_event is not None:
                    h.abort_event.set()
                if h.helper_task is not None and not h.helper_task.done():
                    h.helper_task.cancel()
                    tasks.append(h.helper_task)
            elif h.proc_type == PROC_TYPE_SUBPROCESS:
                proc_obj = h.proc_obj
                try:
                    if proc_obj is not None and getattr(proc_obj, "returncode", None) is None:
                        proc_obj.kill()
                        wait = getattr(proc_obj, "wait", None)
                        if wait is not None:
                            await asyncio.wait_for(wait(), timeout=timeout)
                except Exception:
                    pass

        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        return cleaned

    async def update_helper_progress(
        self,
        proc_id: str,
        *,
        iter_num: int | None = None,
        tool_name: str | None = None,
        thought: str | None = None,
        note: str | None = None,
    ) -> bool:
        """供 helper 自己写进度心跳。返回是否成功(proc_id 不存在或已注销时 False)。

        helper 周期性调本方法,把 ProcessHandle 的 last_progress_at / last_iter /
        recent_tools / last_thought_preview 字段更新,主线程调 processes.list 时
        能看到这些字段判断 helper 是否在工作还是卡住,基于此决策是否 kill。

        线程安全:走 registry 的 asyncio.Lock 与 register/kill 同步。
        """
        async with self._lock:
            h = self._procs.get(proc_id)
            if h is None or h.proc_type != PROC_TYPE_HELPER:
                return False
            h.update_helper_progress(
                iter_num=iter_num, tool_name=tool_name,
                thought=thought, note=note,
            )
            payload = h.to_public_dict()
        try:
            from app.core.environment_events import publish_workflow_event

            publish_workflow_event({"kind": "helper_progress", **payload})
        except Exception:
            pass
        return True

    async def update_progress_summary(self, proc_id: str, summary: str) -> bool:
        """更新 helper 的 progress_summary 字段(供 lite summarizer 写入)。"""
        async with self._lock:
            h = self._procs.get(proc_id)
            if h is None or h.proc_type != PROC_TYPE_HELPER:
                return False
            h.progress_summary = summary[:120]
            return True

    # ─── 杀死 ─────────────────────────────────────────
    async def kill(
        self, proc_id: str, *, requested_by: str,
        reason: str = "",
        force: bool = False,
    ) -> dict:
        """杀死指定进程,带 owner 校验 + kill gate(仅 helper)。

        2026-05-05: helper kill 必须传合法的 reason。kill gate 下沉到
        ProcessRegistry 层确保所有 kill 入口(delegate/processes/bg_force_kill)
        统一经过同一道门禁。

        Returns:
            {"ok": bool, "error"?: str, "killed_proc_id"?: str, "proc_type"?: str}
        """
        async with self._lock:
            h = self._procs.get(proc_id)
            if h is None:
                # 2026-05-08 Fix(Bug 6 ext): proc_id 不在 registry 时,改为幂等成功。
                # 旧版返回 ok=False ERROR,实测 LLM 完全忽略 ERROR 反复重试 kill。
                # 新版查 _recently_gone_procs 给出明确"已经结束,无需 kill"的友好响应,
                # 让 LLM 不再把这当作"待修复的失败"。
                gone = self._recently_gone_procs.get(proc_id)
                if gone is not None:
                    _gone_reason = gone.get("reason", "?")
                    _gone_tid = gone.get("task_id")
                    _age_sec = round(time.time() - float(gone.get("at") or 0), 1)
                    return {
                        "ok": True,
                        "already_gone": True,
                        "killed_proc_id": proc_id,
                        "task_id": _gone_tid,
                        "proc_type": gone.get("proc_type", "?"),
                        "gone_reason": _gone_reason,  # "natural" | "killed"
                        "gone_age_sec": _age_sec,
                        "note": (
                            f"proc_id {proc_id!r} 已经"
                            + ("被 kill 并注销" if _gone_reason == "killed"
                               else "自然结束并注销")
                            + f"({_age_sec}s 前)。"
                            + (f"对应 task_id={_gone_tid}。" if _gone_tid else "")
                            + "**这不是错误**——进程已不存在 = 已经被处理过了,无需任何操作。"
                            "**不要重复 kill**,直接继续主流程。"
                        ),
                    }
                # 完全没记录(可能是 LLM 凭空写的 proc_id,或超出追踪窗口)
                # 同样用 ok=True 让 LLM 不要重试,但标记 unknown
                return {
                    "ok": True,
                    "already_gone": True,
                    "killed_proc_id": proc_id,
                    "gone_reason": "unknown",
                    "note": (
                        f"proc_id {proc_id!r} is not in the registry. It may have ended earlier, been mistyped, "
                        "or belonged to another session. Treat it as already handled; use processes(action='list') "
                        "before any further kill attempt.\n"
                        "进程不存在时视为已处理，先 list 再决定是否需要 kill。"
                    ),
                }

            # ── Kill gate: helper 必须校验 reason ──
            # 2026-05-08: 传 helper_handle 让 validate_kill_reason 做语义校验
            # (api_stall_emergency 要核对心跳)
            if h.proc_type == PROC_TYPE_HELPER:
                allowed, gate_msg = validate_kill_reason(
                    h.helper_task_id or proc_id, reason,
                    helper_handle=h,
                )
                if not allowed:
                    return {"ok": False, "error": gate_msg}

            # ── ACL 校验 ──
            # 直接 owner 匹配:本人 kill 本人创建的 → 允许
            # 主线程 kill 同 trace_id 的进程 → 允许(super-user within tenant)
            # 主线程 kill 跨 trace_id 进程 → 拒绝(防止多用户场景的越权)
            allow = False
            if h.owner == requested_by:
                allow = True
            elif self.is_main_owner(requested_by):
                # main 必须与目标在同一个 trace 才能 kill
                if self.trace_id_of(h.owner) == self.trace_id_of(requested_by):
                    allow = True
                    # 主线程 kill 别人(non-self) helper 时记 warning 便于审计
                    log.warning(
                        "main thread killing helper %s owned by %s (same trace)",
                        proc_id, h.owner,
                    )

            if not allow:
                return {
                    "ok": False,
                    "error": (
                        f"Permission denied: proc_id {proc_id!r} is outside your session "
                        f"(owner trace={self.trace_id_of(h.owner)}, your trace={self.trace_id_of(requested_by)}). "
                        f"You may only kill processes you own, or same-session helpers from the main process.\n"
                        f"权限拒绝，只能终止自己或同会话内允许的进程。"
                    ),
                }

        # ── 实际 kill 操作(在 lock 外做,避免长时间持锁)──
        kill_result = {
            "ok": True,
            "killed_proc_id": proc_id,
            "proc_type": h.proc_type,
            "description": h.description,
            "kill_reason": reason,
        }

        if h.proc_type == PROC_TYPE_SUBPROCESS:
            try:
                if h.proc_obj is not None and h.proc_obj.returncode is None:
                    h.proc_obj.kill()
                    # 异步等待回收(短 timeout)
                    try:
                        await asyncio.wait_for(h.proc_obj.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        log.warning(
                            "subprocess %s did not exit 3s after kill (pid=%s)",
                            proc_id, h.pid,
                        )
                kill_result["pid"] = h.pid
            except (ProcessLookupError, OSError) as e:
                # 进程可能已经结束
                kill_result["note"] = f"process already gone: {type(e).__name__}"

        elif h.proc_type == PROC_TYPE_HELPER:
            # 优先用协作中断:set abort_event,让 helper 自然出最后一轮总结
            # force=True 才直接 cancel asyncio.Task(无总结,工作区可能不完整)
            if h.abort_event is not None:
                h.abort_event.set()
                kill_result["mode"] = "cooperative_abort"
                kill_result["task_id"] = h.helper_task_id
                if force:
                    if h.helper_task and not h.helper_task.done():
                        h.helper_task.cancel()
                    kill_result["mode"] = "force_cancel"
            else:
                # fallback:没 abort_event 直接 cancel
                if h.helper_task and not h.helper_task.done():
                    h.helper_task.cancel()
                kill_result["mode"] = "force_cancel"
                kill_result["task_id"] = h.helper_task_id

        # 注销 — kill 后从 registry 移除(后台 task 也可能并行 unregister,pop 用 None)
        async with self._lock:
            self._procs.pop(proc_id, None)
            # 记录 task_id → 时间，供 _handle_main_kill_helper 幂等检查
            if h.proc_type == PROC_TYPE_HELPER and h.helper_task_id:
                self._recently_killed[h.helper_task_id] = time.time()
            # 2026-05-08 Fix(Bug 6 ext): 记录 proc_id → 上下文, 供后续 kill 幂等检查
            self._recently_gone_procs[proc_id] = {
                "task_id": h.helper_task_id if h.proc_type == PROC_TYPE_HELPER else None,
                "proc_type": h.proc_type,
                "at": time.time(),
                "reason": "killed",
            }

        if h.proc_type == PROC_TYPE_HELPER:
            kill_result["task_id"] = h.helper_task_id
        return kill_result

    # ─── 维护 ─────────────────────────────────────────
    async def cleanup_dead(self) -> int:
        """清理已经 done 的 helper task 和已经退出的 subprocess。
        返回清理的数量。供后台周期清理 task 调用。"""
        async with self._lock:
            dead = []
            for pid, h in self._procs.items():
                if h.proc_type == PROC_TYPE_HELPER:
                    if h.helper_task is None or h.helper_task.done():
                        dead.append(pid)
                elif h.proc_type == PROC_TYPE_SUBPROCESS:
                    if h.proc_obj is None or getattr(h.proc_obj, "returncode", None) is not None:
                        dead.append(pid)
            for pid in dead:
                # 2026-05-08 Fix(Bug 6 ext): 记录 proc_id 上下文, 让后续 kill 幂等
                _h = self._procs.pop(pid, None)
                if _h is not None:
                    self._recently_gone_procs[pid] = {
                        "task_id": _h.helper_task_id if _h.proc_type == PROC_TYPE_HELPER else None,
                        "proc_type": _h.proc_type,
                        "at": time.time(),
                        "reason": "natural",
                    }
            # 2026-05-08 Fix(memory): TTL sweep on recently_* trackers, 防长期运行下
            # _recently_killed / _recently_completed / _recently_gone_procs 无限累积。
            # 检查窗口分别为 60s/600s/300s,sweep 用更宽的 TTL 避免误伤还在窗口内的查询。
            _now = time.time()
            _ttl_killed = 600.0     # was_recently_killed within=60, 留 10x buffer
            _ttl_completed = 1800.0  # was_recently_completed within=600, 留 3x buffer
            _ttl_gone = 1800.0       # 与 completed 同
            for tid, ts in list(self._recently_killed.items()):
                if (_now - ts) > _ttl_killed:
                    self._recently_killed.pop(tid, None)
            for tid, ts in list(self._recently_completed.items()):
                if (_now - ts) > _ttl_completed:
                    self._recently_completed.pop(tid, None)
            for pid, info in list(self._recently_gone_procs.items()):
                _at = float(info.get("at") or 0)
                if (_now - _at) > _ttl_gone:
                    self._recently_gone_procs.pop(pid, None)
            return len(dead)

    async def stats(self) -> dict:
        async with self._lock:
            n_helpers = sum(
                1 for h in self._procs.values() if h.proc_type == PROC_TYPE_HELPER
            )
            n_subs = sum(
                1 for h in self._procs.values() if h.proc_type == PROC_TYPE_SUBPROCESS
            )
            return {
                "active_helpers": n_helpers,
                "active_subprocesses": n_subs,
                "total": len(self._procs),
            }


# ─── 单例 ────────────────────────────────────────────────
_registry: Optional[ProcessRegistry] = None


def registry() -> ProcessRegistry:
    """返回全局单例。首次调用懒初始化。"""
    global _registry
    if _registry is None:
        _registry = ProcessRegistry()
    return _registry


# ─── ContextVar:当前 dispatch context 的 owner_id ──────────
# tool dispatch 时需要知道是谁在调(主线程 or 哪个 helper)。
# 用 ContextVar 在 asyncio 环境下天然 task-local 隔离,无并发污染风险。
import contextvars

_current_owner: contextvars.ContextVar[str] = contextvars.ContextVar(
    "process_owner", default="main:unknown",
)


def current_owner() -> str:
    """返回当前 LLM 调用的 owner_id。"""
    return _current_owner.get()


def set_current_owner(owner: str):
    """设置当前 owner_id,返回 token 供 reset。

    用法:
        token = set_current_owner("helper:abc:radix_sort")
        try:
            ... await tool dispatch ...
        finally:
            reset_current_owner(token)
    """
    return _current_owner.set(owner)


def reset_current_owner(token):
    _current_owner.reset(token)


# 2026-05-11 P9: helper kind ContextVar (用于工具层判断当前调用方角色)
# 病因(实测 trace 18:46-19:09): paper/pptx helper kind="edit", 但越权写
# matplotlib 重画图, 引入算法名错配 bug (B+Tree/RedBlack 不存在于 CSV)。
# 修法: workspace.write / python 工具层根据 kind 做差异化拒绝。
# 默认空字符串, 主线程 / 老调用方不受影响。
_current_helper_kind: contextvars.ContextVar[str] = contextvars.ContextVar(
    "helper_kind", default="",
)


def current_helper_kind() -> str:
    """返回当前 helper 的 kind ('code'/'edit'/'read'/'verify'/'draw'/'')。

    主线程返回 '' (空字符串, 表示非 helper 上下文)。
    helper 内调用工具时, 通过此 ContextVar 知道自己的角色。
    """
    return _current_helper_kind.get()


def set_current_helper_kind(kind: str):
    """设置当前 helper kind, 返回 token 供 reset。

    在 _run_one_helper 入口 set, 退出时 reset。
    """
    return _current_helper_kind.set(kind or "")


def reset_current_helper_kind(token):
    _current_helper_kind.reset(token)


_current_helper_expected_outputs: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "helper_expected_outputs", default=(),
)


def current_helper_expected_outputs() -> tuple[str, ...]:
    """Return expected output paths declared for the current helper.

    The value is empty outside helper execution and for helpers without an
    explicit output contract.
    """
    return _current_helper_expected_outputs.get()


def set_current_helper_expected_outputs(paths):
    cleaned: list[str] = []
    for path in paths or ():
        value = str(path or "").replace("\\", "/").lstrip("./").strip()
        if value:
            cleaned.append(value)
    return _current_helper_expected_outputs.set(tuple(cleaned))


def reset_current_helper_expected_outputs(token):
    _current_helper_expected_outputs.reset(token)


_current_helper_write_scopes: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "helper_write_scopes", default=(),
)


def current_helper_write_scopes() -> tuple[str, ...]:
    """Return staged project paths or directories the current helper may edit."""
    return _current_helper_write_scopes.get()


def set_current_helper_write_scopes(paths):
    cleaned: list[str] = []
    for path in paths or ():
        value = str(path or "").replace("\\", "/").lstrip("./").strip().rstrip("/")
        if value:
            cleaned.append(value)
    return _current_helper_write_scopes.set(tuple(cleaned))


def reset_current_helper_write_scopes(token):
    _current_helper_write_scopes.reset(token)


# ─── Spawn Queue:legacy dispatcher queue; helper-side spawn is disabled ─
# 历史上 helper-side spawn 通过这个 ContextVar 找到 dispatcher queue。
# 当前 helper 已不能 spawn/wait/resume/kill 其它 helper;变量只保留给旧入口、
# 测试和渐进迁移代码读取,避免破坏仍 import 这些函数的调用点。
_spawn_queue: contextvars.ContextVar[Optional[asyncio.Queue]] = contextvars.ContextVar(
    "spawn_queue", default=None,
)


def current_spawn_queue() -> Optional[asyncio.Queue]:
    return _spawn_queue.get()


def set_current_spawn_queue(queue: Optional[asyncio.Queue]):
    return _spawn_queue.set(queue)


def reset_current_spawn_queue(token):
    _spawn_queue.reset(token)


# ─── Abort Event:让长跑工具(workspace.run)中途响应 abort ────────────────
# (Phase 5++ — trace 9ca732f4 教训: 用户 abort 但 tool 还在跑 6s,然后
#  forced_finalize 又 100s,user 等了 ~2 分钟才停。)
# ContextVar 让任意上下文的工具调用都能拿到当前作用域的 abort:
#   - 主线程: 共享 group abort(用户 /abort 触发)
#   - helper: local abort(stuck 触发)+ shared abort 桥接(用户 abort 触发)
_current_abort: contextvars.ContextVar[Optional[asyncio.Event]] = contextvars.ContextVar(
    "current_abort_event", default=None,
)

def current_abort_event() -> Optional[asyncio.Event]:
    return _current_abort.get()


def set_current_abort_event(event: Optional[asyncio.Event]):
    return _current_abort.set(event)


def reset_current_abort_event(token):
    _current_abort.reset(token)


# ─── Helper Self Proc ID ContextVar ─────────────────────────────────────
# 让 helper 内部代码(chat_with_tools_loop 的进度心跳回调)能拿到自己的 proc_id,
# 用以调 registry().update_helper_progress(proc_id, ...) 主动汇报状态。
# delegate.py 在 register helper 后立即 set 这个 ContextVar(只对 helper 自己的
# asyncio task context 可见,主线程 / 兄弟 helper 看不到 — ContextVar 在 asyncio
# 子任务里天然隔离)。
_current_helper_proc_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_helper_proc_id", default=None,
)


def current_helper_proc_id() -> Optional[str]:
    """helper 内调用,返回自己的 proc_id(供 update_helper_progress 用)。
    主线程或未注册的上下文返回 None。"""
    return _current_helper_proc_id.get()


def set_current_helper_proc_id(proc_id: Optional[str]):
    return _current_helper_proc_id.set(proc_id)


def reset_current_helper_proc_id(token):
    _current_helper_proc_id.reset(token)


# ─── Thread Context — 让 recall_thread 工具能拿到原始任务 ──────────────
# 2026-05-03: orchestrator 在 _drive_round2 入口处 set,helper spawn 时
# delegate.py 把父线程的 plan 带进 helper 上下文。helper 调 recall_thread
# 能回忆起"最终用户要什么"而不是只看到自己被分配的那一小块。

@dataclass
class ThreadContext:
    """单轮对话的线程上下文——原始用户任务 + 当前 plan 快照。"""
    user_message: str = ""           # 用户原始消息(全文)
    plan_intent: str = ""            # plan.intent 的当前版
    plan_key_points: list = field(default_factory=list)
    plan_deliverables: list = field(default_factory=list)
    role_label: str = "main"         # "main" | "helper"


_current_thread_context: contextvars.ContextVar[Optional[ThreadContext]] = \
    contextvars.ContextVar("current_thread_context", default=None)


def set_current_thread_context(ctx: Optional[ThreadContext]):
    return _current_thread_context.set(ctx)


def get_current_thread_context() -> Optional[ThreadContext]:
    return _current_thread_context.get()


def reset_current_thread_context(token):
    _current_thread_context.reset(token)


async def update_thread_plan(*, intent: str = "", key_points: list | None = None,
                             deliverables: list | None = None) -> bool:
    """更新当前线程上下文的 plan 快照(round2 各阶段回写用)。"""
    ctx = _current_thread_context.get()
    if ctx is None:
        return False
    if intent:
        ctx.plan_intent = intent
    if key_points is not None:
        ctx.plan_key_points = list(key_points)
    if deliverables is not None:
        ctx.plan_deliverables = list(deliverables)
    return True


async def report_helper_progress(
    *,
    iter_num: int | None = None,
    tool_name: str | None = None,
    thought: str | None = None,
    note: str | None = None,
) -> bool:
    """helper 自报进度的便捷函数 (从 ContextVar 自动拿 proc_id)。

    主线程上下文调用是 no-op(返回 False)。
    """
    pid = _current_helper_proc_id.get()
    if not pid:
        return False
    return await registry().update_helper_progress(
        pid,
        iter_num=iter_num, tool_name=tool_name,
        thought=thought, note=note,
    )
