"""
聊天 API：SSE 流式输出 + per-user 互斥锁（2026-05-01 改造前为 per-group）。

锁语义：
- 同一 (archive_id, group_id, user_id) 同时只允许一个对话流程。
- 同群里**不同 user**的请求互不阻塞——别人的请求对当前用户的对话连续性
  没那么重要,后续把消息存入记忆即可,不用等。
- 新请求来时若**该 user**已有任务在跑,**立即返回 409 Conflict**（不等待）;
  bridge 侧据此判断是否给同 user 发 abort 信号。
- 锁覆盖整个生命周期：三轮调用 + 后台维护,全部完成才释放。
- 前端应在收到 SSE 的 'complete' 事件后,才允许**该用户**的下一次发送。

事件协议：
  event: meta       data: {"trace_id": "..."}
  event: progress   data: {"round": "loading|analyzing|planning|responding|maintaining"}
  event: progress   data: {"round": "planning", "tool_call": "python", "tool_call_count": 2}
  event: token      data: {"text": "..."}                 # 流式 token
  event: done       data: {"tendencies": {...}, "trace_id": "..."}   # 响应文本完成
  event: complete   data: {"trace_id": "..."}              # 含后台维护已完成
  event: error      data: {"code": "...", "message": "..."}

错误码（409 Conflict 响应体）：
  {"detail": {"code": "group_busy", "message": "...", "active_trace_id": "..."}}
"""
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

from app.schemas.api import ChatRequest
from app.memory import archive as archive_dao
from app.core.orchestrator import orchestrate
from app.core.locks import get_group_guard, GroupBusyError
from app.core import debug
from app.llm.tools import workspace as ws_tool


log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/chat", tags=["chat"])
SSE_RESPONSE_HEADERS = {"Content-Type": "text/event-stream; charset=utf-8"}


# ──────────────────────────────────────────────────────────────────────
# Patch 10: client_msg_id 幂等表
# ──────────────────────────────────────────────────────────────────────
# 客户端(napcat 等)网络抖动重发同一条消息,会让后端真切跑两遍三轮流水线 +
# 写两份 hot 记忆 + 触发两遍 abort。用进程内 LRU 30 秒短期幂等去重。
#
# (archive_id, group_id, user_id, client_msg_id) → (trace_id, ts)
_IDEMPOTENCY_TTL = 30.0  # 30 秒去重窗口
_IDEMPOTENCY_MAX = 5000  # 表上限
# 2026-05-02 review Bug T 修:_idempotency_cache value 扩展存 done + complete payload。
#   - 之前 _replay 重发只 emit meta + complete,缺 done 事件,前端如果用 done 事件
#     渲染最终回复(tendencies + files),duplicate 路径下 UI 会丢气泡。
#   - value 结构:(trace_id, ts, done_payload | None, complete_payload | None)
#     done/complete payload 在原始流跑完时由 event_gen 截获写入。
#   - 重发时 _replay 先吐 meta,再吐缓存的 done(若有)→ complete(若有,否则用回退)。
_idempotency_cache: "OrderedDict[tuple, tuple[str, float, dict | None, dict | None]]" = OrderedDict()


def _idempotency_check(req: ChatRequest) -> str | None:
    """
    返回:
      None — 是新请求,继续处理
      str  — 是重发,返回上次的 trace_id(SSE 流不复跑)
    """
    if not req.client_msg_id:
        return None  # 客户端没传 → 不去重(向后兼容)

    now = time.time()
    key = (req.archive_id, req.group_id, req.user_id, req.client_msg_id)

    # 清理过期(惰性,只看头几个)
    while _idempotency_cache:
        first_key = next(iter(_idempotency_cache))
        ts = _idempotency_cache[first_key][1]
        if now - ts > _IDEMPOTENCY_TTL:
            _idempotency_cache.popitem(last=False)
        else:
            break

    # 命中
    if key in _idempotency_cache:
        trace_id, ts, _done, _complete = _idempotency_cache[key]
        if now - ts <= _IDEMPOTENCY_TTL:
            return trace_id
    return None


def _idempotency_register(req: ChatRequest, trace_id: str) -> None:
    if not req.client_msg_id:
        return
    key = (req.archive_id, req.group_id, req.user_id, req.client_msg_id)
    _idempotency_cache[key] = (trace_id, time.time(), None, None)
    if len(_idempotency_cache) > _IDEMPOTENCY_MAX:
        _idempotency_cache.popitem(last=False)


def _idempotency_record_event(
    req: ChatRequest, event_name: str, payload: dict,
) -> None:
    """流跑到 done / complete 时把 payload 写回 cache,供后续 _replay 重放。

    2026-05-02 review Bug T 修:之前 _replay 只重放 meta + complete,缺 done,
    前端如果用 done.tendencies / done.files 渲染最终气泡 — duplicate 路径下
    UI 会丢气泡。改成把 done 和 complete 的 payload 都缓存,_replay 时完整回放。
    """
    if not req.client_msg_id:
        return
    key = (req.archive_id, req.group_id, req.user_id, req.client_msg_id)
    entry = _idempotency_cache.get(key)
    if not entry:
        return
    trace_id, ts, done_payload, complete_payload = entry
    if event_name == "done":
        done_payload = payload
    elif event_name == "complete":
        complete_payload = payload
    else:
        return
    _idempotency_cache[key] = (trace_id, ts, done_payload, complete_payload)


# ──────────────────────────────────────────────────────────────────────
# Patch 11(简化版):可执行文件直接拒绝,不再用 deliverables 授权机制
# ──────────────────────────────────────────────────────────────────────
# 旧设计:.exe 等 RISKY_EXTENSIONS 必须出现在 plan.deliverables 才能下,
#         napcat 端发警告链接由用户主动同意。
# 实际问题(用户反馈):
#   1. 角色扮演场景里发"⚠️ 可执行文件,请确认"会瞬间破坏人设,
#      正常用户体验严重受损
#   2. 真有恶意意图的攻击者一定会点"同意",所以警告对真威胁毫无防护作用
#   3. UAC-style 确认弹窗在两个维度都失败 → 应当直接禁止
# 新策略:download_file 对所有可执行扩展名直接 403 拒绝。
# 用户如果真的需要二进制产物,可以下载源码自己编译。
@router.post("/stream")
async def chat_stream(req: ChatRequest):
    if not await archive_dao.get_archive(req.archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")

    # ── Patch 10: 幂等去重 ──
    duplicate_trace = _idempotency_check(req)
    if duplicate_trace is not None:
        debug.set_trace_id(duplicate_trace)
        debug.log(
            "chat.duplicate",
            f"client_msg_id={req.client_msg_id!r} reused trace={duplicate_trace}",
        )
        # 取出缓存的 done / complete payload(可能为 None,如果首次流还没跑到那一步)
        _key = (req.archive_id, req.group_id, req.user_id, req.client_msg_id)
        _entry = _idempotency_cache.get(_key)
        _cached_done = _entry[2] if _entry else None
        _cached_complete = _entry[3] if _entry else None

        async def _replay():
            yield {
                "event": "meta",
                "data": json.dumps({"trace_id": duplicate_trace, "duplicate": True}),
            }
            # 2026-05-02 review Bug T 修:replay 也吐 done,前端 UI 用 done.tendencies / done.files
            # 渲染最终气泡,不能少这一帧
            if _cached_done is not None:
                yield {
                    "event": "done",
                    "data": json.dumps(
                        {**_cached_done, "duplicate": True}, ensure_ascii=False,
                    ),
                }
            yield {
                "event": "complete",
                "data": json.dumps(
                    {**(_cached_complete or {}), "trace_id": duplicate_trace, "duplicate": True},
                    ensure_ascii=False,
                ),
            }
        return EventSourceResponse(_replay(), headers=SSE_RESPONSE_HEADERS)

    trace_id = uuid.uuid4().hex[:16]
    _idempotency_register(req, trace_id)
    guard = get_group_guard()

    # 同步 try-acquire（per-user 锁）：失败立刻 409,不进入 SSE 流
    try:
        await guard.acquire(
            req.archive_id, req.group_id, req.user_id, trace_id,
            user_name=req.user_name,
        )
    except GroupBusyError as e:
        debug.set_trace_id(trace_id)
        debug.log(
            "user.busy",
            f"rejected: archive={req.archive_id} group={req.group_id} "
            f"user={req.user_id} holder={e.holder_trace}",
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                # 错误码兼容旧前端，但语义是 per-user busy
                "code": "user_busy",
                "message": str(e),
                "active_trace_id": e.holder_trace,
                "trace_id": trace_id,
            },
        )

    debug.set_trace_id(trace_id)
    debug.log(
        "user.acquired",
        f"archive={req.archive_id} group={req.group_id} user={req.user_id}",
    )

    async def event_gen():
        try:
            async for event_name, payload in orchestrate(req, trace_id=trace_id):
                # 2026-05-02 review Bug T 修:把 done / complete 缓存,供 _replay 重放
                if event_name in ("done", "complete") and isinstance(payload, dict):
                    _idempotency_record_event(req, event_name, payload)
                yield {
                    "event": event_name,
                    "data": json.dumps(payload, ensure_ascii=False),
                }
        except Exception as e:
            log.exception("chat_stream failed")
            debug.error(f"chat stream failed: {type(e).__name__}: {e}")
            yield {
                "event": "error",
                "data": json.dumps(
                    {"code": "internal_error", "message": str(e)},
                    ensure_ascii=False,
                ),
            }
        finally:
            released = await guard.release(
                req.archive_id, req.group_id, req.user_id, trace_id,
            )
            debug.log(
                "user.released",
                f"released={released} archive={req.archive_id} "
                f"group={req.group_id} user={req.user_id}",
            )

    return EventSourceResponse(event_gen(), headers=SSE_RESPONSE_HEADERS)


# ── 调试/运营：查看当前活跃的用户锁 ────────────────────────────
@router.get("/active")
async def list_active() -> dict:
    """列出当前所有持锁的 (archive_id, group_id, user_id) 及其 trace_id。"""
    holders = await get_group_guard().active_holders()
    return {
        "items": [
            {
                "archive_id": k[0], "group_id": k[1], "user_id": k[2],
                "trace_id": v,
            }
            for k, v in holders.items()
        ]
    }


# ── 打断正在进行的对话流程 ──────────────────────────────────────
@router.post("/abort")
async def abort_chat(body: dict):
    """
    发送打断信号给 (archive, group, user) 三元组对应的活跃任务。

    Body 必须含 archive_id / group_id / user_id。bridge 在同 user 又发新消息
    时调用本接口；不同 user 的消息**不应**调用本接口（per-user 串行下别人
    的任务对自己不构成阻塞，没必要打断）。

    2026-05-02 Bug L 修:缺 user_id 之前返 200 + ok=False,bridge 容易把
    HTTP 200 当成 success 处理(忽略 body 的 ok 字段)。改成正经的 422,
    让 bridge 在网络层就感知到客户端用法错误。
    """
    guard = get_group_guard()
    user_id = body.get("user_id")
    archive_id = body.get("archive_id")
    group_id = body.get("group_id")
    missing = [
        name for name, val in
        (("archive_id", archive_id), ("group_id", group_id), ("user_id", user_id))
        if not val
    ]
    if missing:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=422,
            detail=f"missing required fields: {', '.join(missing)}",
        )
    # 2026-05-02 加:abort 端点缺日志导致 trace 难诊断。
    # log debug_20260502_153904 中 abort 在 15:49:18 set 但没人知道是哪来的,
    # 后端日志里完全找不到调用记录。补这一行让以后排查方便。
    debug.log(
        "chat.abort.received",
        f"archive={archive_id} group={group_id} user={user_id}",
    )
    ok = await guard.signal_abort(
        archive_id=archive_id,
        group_id=group_id,
        user_id=user_id,
    )
    debug.log("chat.abort.done", f"ok={ok}")
    return {"ok": ok}


# ── 工作区文件下载 ────────────────────────────────────────────
_MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

# 允许下载的扩展名白名单(纯安全 + 纯文本/源码)
_SAFE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp",  # 图片
    ".csv", ".json", ".xml", ".yaml", ".yml", ".toml",          # 数据
    ".html", ".htm", ".css", ".js", ".md", ".txt", ".log",      # 文档文本
    ".pdf", ".zip", ".tar", ".gz", ".7z",                        # 文档压缩
    ".mp3", ".wav", ".ogg", ".mp4", ".webm",                     # 音视频
    ".py", ".ipynb",                                             # 代码(纯文本,得自己跑)
    ".c", ".h", ".cpp", ".hpp", ".cxx", ".hxx",                  # C/C++ 源码
}

# Patch 11: 可执行文件黑名单 — 直接 403 拒绝,不论 plan.deliverables 怎么写
# 设计理由(用户反馈):
#   - 询问/警告确认在角色扮演场景里破坏人设
#   - 真有恶意意图的攻击者一定会点"同意",警告对真威胁无效
#   - UAC-style 确认在两个维度都失败 → 直接禁止比"询问"更安全更简洁
# 用户如果真需要二进制产物,可以下载源码(.c/.py/.cpp)自己编译。
_BLOCKED_EXTENSIONS = {
    ".exe", ".dll", ".out", ".o", ".obj",
    ".bat", ".cmd", ".ps1", ".sh",   # 脚本执行类
    ".msi", ".com", ".scr",          # Windows 各种执行格式
    ".jar", ".apk", ".dmg", ".deb", ".rpm",  # 跨平台可执行包
}


@router.get("/files/{archive_id}/{group_id}/{filename:path}")
async def download_file(archive_id: str, group_id: str, filename: str):
    """下载当前活跃工作区中 AI 生成的文件。仅限沙箱内文件,拒绝路径遍历。

    Patch 11 (2026-05-02): 扩展名分两档:
      - _SAFE_EXTENSIONS:允许下载
      - _BLOCKED_EXTENSIONS(可执行文件):一律 403,无任何例外
      - 其他扩展名:也 403(默认拒绝原则)
    """
    # 1. 查找活跃工作区（优先内存注册表，回退到持久路径）
    group_key = f"{archive_id}:{group_id}"
    ws_dir = ws_tool.get_workspace(group_key)
    if not ws_dir:
        persistent = ws_tool.get_persistent_workspace_path(archive_id, group_id)
        if os.path.isdir(persistent):
            ws_dir = persistent
    if not ws_dir:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no active workspace for this group")

    # 2. 安全解析（防路径遍历、绝对路径）
    try:
        file_path = ws_tool._safe_resolve(ws_dir, filename)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid path")

    # 3. 解析后再次确认在 workspace 内（纵深防御）
    try:
        Path(file_path).resolve().relative_to(Path(ws_dir).resolve())
    except ValueError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "access denied")

    # 4. 必须是普通文件，拒绝目录/符号链接
    if not os.path.isfile(file_path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"file not found: {filename}")
    if os.path.islink(file_path):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "symlinks not allowed")

    # 5. 大小限制
    if os.path.getsize(file_path) > _MAX_DOWNLOAD_BYTES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "file too large")

    # 6. 扩展名检查 — 黑名单优先,白名单允许
    ext = os.path.splitext(file_path)[1].lower()

    if ext in _BLOCKED_EXTENSIONS:
        # 可执行文件一律拒绝(无论 plan 里是否声明)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"executable files are not downloadable: {ext}",
        )

    if ext not in _SAFE_EXTENSIONS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"extension not allowed: {ext}",
        )

    return FileResponse(file_path, filename=os.path.basename(file_path))
