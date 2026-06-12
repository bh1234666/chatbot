r"""
NapCat ↔ Chatbot Bridge
========================
Receives NapCat HTTP callbacks, routes messages through Chatbot API.

Two modes per group:
  - Observe (default): save messages to KB, no response
  - Participate: run full chat pipeline and reply

User identity: QQ number = user_id (for memory), nickname = user_name (for AI display)

Start: .venv\Scripts\python.exe napcat_bridge.py
Port: 8090
"""
import asyncio
import html
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import ulid
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx

from app.core.file_policy import classify_file_for_delivery
from app.core.sanitize import sanitize_headline, sanitize_summary
from app.db.pool import pool

# Ensure botctl_helper is importable
_src = os.path.dirname(os.path.abspath(__file__))
if _src not in sys.path:
    sys.path.insert(0, _src)
from botctl_helper import run_command, set_pending_input

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [bridge] %(message)s",
)
for noisy in ("httpx", "httpcore", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("bridge")


# ── Patch 13: 模块级共享 httpx.AsyncClient + lifespan ──
# 旧版每个 napcat callback `async with httpx.AsyncClient(timeout=120.0):`
# 1) 每次新建/销毁 client,无 connection pool 跨请求复用
# 2) timeout=120 导致 SSE 流过 2 分钟就断,helper safety timeout 是 30 分钟,
#    实测 trace df5ec70a 9 分钟完成,bridge 早就断流,reply 丢失
# 新版:
#   - timeout 拆分:connect=10s, read=None(SSE 长任务), write=30s
#   - 模块级共享 client,在 FastAPI lifespan 里建立/关闭
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
_HTTP_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)
_shared_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """返回模块级共享的 httpx.AsyncClient。"""
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            limits=_HTTP_LIMITS,
        )
    return _shared_client


async def close_http_client() -> None:
    """lifespan shutdown 时关掉。"""
    global _shared_client
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None


async def _start_cleanup() -> None:
    from app.core.bg_tasks import schedule

    schedule(_periodic_cleanup(), name="bridge.cleanup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_http_client()
    await _start_cleanup()
    log.info("bridge started; shared http client ready")
    try:
        yield
    finally:
        await close_http_client()
        log.info("bridge stopped; http client closed")


app = FastAPI(title="NapCat-Chatbot Bridge", lifespan=lifespan)

from app.config import settings

CHATBOT_URL = settings.chatbot_url
NAPCAT_URL = settings.napcat_url

# ── 缓存（带 TTL 上限，防止内存泄漏） ──
_CACHE_TTL_SEC = 600   # 10 分钟后清理未访问的缓存条目
_MAX_CACHE_ENTRIES = 500

_obs_archive_cache: dict[str, tuple[str, float]] = {}   # group_id → (archive_id, last_access)
_chat_archive_cache: dict[str, tuple[str, float]] = {}
_participate_cache: dict[str, tuple[bool, float]] = {}  # group_id → (participate, last_access)
_admin_group_cache: tuple[str, float] | None = None  # (group_id, timestamp)
_persona_ensured: set[str] = set()  # archive_ids 已确认有 persona，跳过重复 GET

# 2026-05-01 改造: per-user 路由
# 旧版以 group_id 为键: 同群所有用户排成一队,trace ea1a8826 的 RL 训练任务
# 阻塞 13 分钟把整群所有人的请求都堵死。
# 新版以 (group_id, user_id) 为键: 单用户串行(保对话连续性), 不同用户并行
# (别人的请求对自己不构成阻塞)。
# abort 信号也跟着改成 per-user: 用户只能打断自己的任务,不影响别人。
_PendKey = tuple[str, str]   # (group_id, user_id)
_pending_queue: defaultdict[_PendKey, deque] = defaultdict(deque)
_processing_lock: defaultdict[_PendKey, asyncio.Lock] = defaultdict(asyncio.Lock)
_currently_processing: set[_PendKey] = set()   # 哪些 (group, user) 正在跑 chat 流
_abort_injected_messages: defaultdict[_PendKey, deque[str]] = defaultdict(deque)
_RECENT_OBSERVED_MEDIA: dict[_PendKey, tuple[float, str]] = {}
_RECENT_OBSERVED_MEDIA_TTL = 300.0

# Multi-turn botctl sessions: user_id → (timestamp, stage_cmd, stage_args)
_botctl_sessions: dict[str, tuple[float, str, list[str]]] = {}
_SESSION_TTL = 300  # 5 minutes

_KNOWN_BOTCTL_CMDS = {
    "create", "list", "ls", "list-groups", "list-personas",
    "switch", "sw", "leave", "recent", "quick", "join",
    "help", "info", "admin", "archives", "delete-archive", "cleanup", "cleanup-kb-placeholders", "del",
}


# ── 后台周期清理 ──
# 旧版只在用户下次发消息时检查 _botctl_sessions / _pending_queue / _processing_lock TTL,
# 闲置 session/lock 永远不清理。每个 (group, user) 一把锁,长期运行后能积累几千个空 lock,
# 占用内存 + 让 _processing_lock.keys() 遍历变慢。
# 这里加一个独立 task 周期扫描:每 60s 清一次过期 session、空队列和空闲锁。
_CLEANUP_INTERVAL_SEC = 60.0
_LOCK_IDLE_TTL_SEC = 1800.0  # 30 分钟内没用过的锁就清掉(同 user 30 分钟内没发消息)
_pkey_last_seen: dict[_PendKey, float] = {}   # last activity timestamp per (group, user)


def _touch_pkey(pkey: _PendKey) -> None:
    """每次该 (group, user) 有任何活动时调用,刷新最后活跃时间。"""
    _pkey_last_seen[pkey] = time.time()


async def _periodic_cleanup() -> None:
    """后台清理 task: 每 60s 跑一次,清过期 session/empty queue/idle lock。"""
    log.info("bridge cleanup task started (interval=%ds)", _CLEANUP_INTERVAL_SEC)
    while True:
        try:
            await asyncio.sleep(_CLEANUP_INTERVAL_SEC)
            now = time.time()

            # 1. 过期 botctl session
            expired_sessions = [
                uid for uid, (ts, *_rest) in _botctl_sessions.items()
                if now - ts > _SESSION_TTL
            ]
            for uid in expired_sessions:
                _botctl_sessions.pop(uid, None)

            # 2. 空闲 (group, user) 锁----超过 _LOCK_IDLE_TTL_SEC 没活动且没在处理 + 队列空 → 清
            idle_pkeys: list[_PendKey] = []
            for pkey, lock in list(_processing_lock.items()):
                last = _pkey_last_seen.get(pkey, 0.0)
                if (now - last > _LOCK_IDLE_TTL_SEC
                        and pkey not in _currently_processing
                        and not _pending_queue.get(pkey)
                        and not lock.locked()):
                    idle_pkeys.append(pkey)
            for pkey in idle_pkeys:
                _processing_lock.pop(pkey, None)
                _pending_queue.pop(pkey, None)
                _pkey_last_seen.pop(pkey, None)

            # 3. 空 deque 也清掉(很可能与 lock 不同步,如曾经处理过现在留个空 deque)
            empty_queues = [
                pkey for pkey, dq in list(_pending_queue.items())
                if not dq and pkey not in _currently_processing
                and pkey not in _processing_lock
            ]
            for pkey in empty_queues:
                _pending_queue.pop(pkey, None)

            # 4. 各种 cache 也定期 prune
            _cache_prune(_obs_archive_cache)
            _cache_prune(_chat_archive_cache)
            _cache_prune(_participate_cache)

            # 5. ProcessRegistry 防御性清理:正常情况 helper 完成时 done_callback
            #    会自动 unregister。这里多扫一遍捕捉 callback 失败的边缘情况
            #    (event loop 异常、回调被 cancel 等)
            try:
                from app.core.core_processes import registry as _proc_registry
                n_dead = await _proc_registry().cleanup_dead()
                if n_dead:
                    log.info("ProcessRegistry: cleaned %d dead handles", n_dead)
            except Exception:
                log.exception("ProcessRegistry cleanup failed (continuing)")

            # 6. Stale _delegate_* 子目录清理 (Phase 5++ -- workspace 爆炸 bug)
            #    实测: 22 个 _delegate_*  + 大数据文件 = 45GB 工作区。
            #    helper 完成后理论上应自己清,但失败/abort/panic 时会留 stale 目录。
            #    超过 _DELEGATE_DIR_TTL 没活动(mtime 老于此)的强删。
            try:
                n_dirs, n_bytes = await _cleanup_stale_delegate_dirs()
                if n_dirs:
                    log.info(
                        "delegate cleanup: removed %d stale _delegate_* dirs "
                        "(freed %.1f MB)",
                        n_dirs, n_bytes / 1024 / 1024,
                    )
            except Exception:
                log.exception("delegate dir cleanup failed (continuing)")

            if expired_sessions or idle_pkeys or empty_queues:
                log.info(
                    "cleanup: sessions=%d idle_pkeys=%d empty_queues=%d "
                    "(remaining: sessions=%d locks=%d queues=%d)",
                    len(expired_sessions), len(idle_pkeys), len(empty_queues),
                    len(_botctl_sessions), len(_processing_lock), len(_pending_queue),
                )
        except asyncio.CancelledError:
            log.info("bridge cleanup task cancelled")
            break
        except Exception:
            log.exception("bridge cleanup task error (continuing)")


# ─── Stale _delegate_* directory cleanup ─────────────────────────────────
# 防 workspace 爆炸: 实测 trace 8b60c2 后续 22 个 _delegate_* + 大数据 = 45GB
_DELEGATE_DIR_TTL_SEC = 1800.0  # 30 分钟无活动就视为 stale,删(原 1 小时太宽,实测 45GB 爆炸)
_DELEGATE_STARTUP_SCAN_DONE = False  # 启动后扫描标记,首轮跑全扫描(忽略 TTL)
_DELEGATE_DIR_SCAN_DEPTH = 2     # 只扫 workspaces/<archive>/<group>/_delegate_* 这层


async def _cleanup_stale_delegate_dirs() -> tuple[int, int]:
    """扫所有 workspace 根目录,删超过 TTL 没活动的 _delegate_* 子目录。

    Returns: (删除的目录数, 释放的字节数估算)

    特殊行为:
      - 启动后第一次扫描 (_DELEGATE_STARTUP_SCAN_DONE=False): **忽略 TTL**,删所有
        _delegate_* (因为 bot 刚启动,任何 _delegate_* 都是上次运行残留 = 100% orphan)
      - 后续扫描: 走 TTL 检查
      - 单 archive_id/group_id 下 _delegate_* 总占用 > 5GB 时,**触发紧急清理**
        (无视 TTL,按 mtime 升序删,直到降到阈值下)
    """
    global _DELEGATE_STARTUP_SCAN_DONE
    import os
    import shutil
    from pathlib import Path
    try:
        from app.llm.tools.workspace import _get_workspace_root
        root = _get_workspace_root()
    except Exception:
        return (0, 0)

    if not root.exists():
        _DELEGATE_STARTUP_SCAN_DONE = True
        return (0, 0)

    now = time.time()
    n_removed = 0
    bytes_freed = 0
    is_startup_scan = not _DELEGATE_STARTUP_SCAN_DONE
    if is_startup_scan:
        log.info(
            "delegate cleanup: STARTUP scan (will remove all _delegate_* dirs as orphans)",
        )

    # ── 紧急清理阈值:单 group 下 _delegate_* 超 5GB,无视 TTL 按 mtime 升序清 ──
    EMERGENCY_GROUP_BYTES = 5 * 1024 * 1024 * 1024  # 5GB

    # 扫 archive_id/group_id/_delegate_*/  (最多两层)
    try:
        for archive_dir in root.iterdir():
            if not archive_dir.is_dir():
                continue
            for group_dir in archive_dir.iterdir():
                if not group_dir.is_dir():
                    continue

                # 收集本 group 下所有 _delegate_* + 大小
                delegate_entries = []
                for delegate_dir in group_dir.iterdir():
                    if not delegate_dir.is_dir():
                        continue
                    if not delegate_dir.name.startswith("_delegate_"):
                        continue
                    try:
                        mtime = delegate_dir.stat().st_mtime
                        size = sum(
                            f.stat().st_size for f in delegate_dir.rglob("*")
                            if f.is_file()
                        )
                    except OSError:
                        continue
                    delegate_entries.append((mtime, size, delegate_dir))

                group_total = sum(e[1] for e in delegate_entries)
                emergency = group_total > EMERGENCY_GROUP_BYTES

                if emergency:
                    log.warning(
                        "delegate emergency cleanup: %s/%s _delegate_* total %d bytes "
                        "(> %d) -- purging by mtime",
                        archive_dir.name, group_dir.name,
                        group_total, EMERGENCY_GROUP_BYTES,
                    )

                # 排序:旧的先删
                delegate_entries.sort(key=lambda e: e[0])

                for mtime, size, delegate_dir in delegate_entries:
                    age = now - mtime
                    # 决定是否删
                    if is_startup_scan:
                        do_delete = True  # 启动后第一次,全删
                    elif emergency and group_total > EMERGENCY_GROUP_BYTES:
                        do_delete = True  # 紧急清理,直到降到阈值下
                    elif age > _DELEGATE_DIR_TTL_SEC:
                        do_delete = True  # 过 TTL
                    else:
                        do_delete = False

                    if not do_delete:
                        continue

                    # ── Belt-and-suspenders 安全检查 (Phase 5++ v3) ──
                    # 即使 startswith("_delegate_") 通过,也再次确认路径"可清"
                    # 防止任何配置错误或路径注入误删主工作区
                    try:
                        from app.llm.tools.workspace import _is_safe_to_wipe
                        if not _is_safe_to_wipe(str(delegate_dir)):
                            log.error(
                                "STALE CLEANUP REFUSED: %s failed _is_safe_to_wipe",
                                delegate_dir,
                            )
                            continue
                    except ImportError:
                        pass  # workspace 模块未加载,继续(原检查已够)

                    try:
                        shutil.rmtree(str(delegate_dir), ignore_errors=True)
                        # rmtree(ignore_errors=True) 不抛异常但可能部分失败,
                        # 检查是否真删干净
                        if delegate_dir.exists():
                            log.warning(
                                "rmtree partial fail: %s still exists",
                                delegate_dir,
                            )
                            continue
                        n_removed += 1
                        bytes_freed += size
                        group_total -= size
                    except OSError:
                        log.warning(
                            "failed to remove stale delegate dir: %s",
                            delegate_dir,
                        )
    except OSError:
        log.exception("stale delegate scan failed")

    _DELEGATE_STARTUP_SCAN_DONE = True
    return (n_removed, bytes_freed)



def _cache_prune(cache: dict, max_entries: int = _MAX_CACHE_ENTRIES, ttl: float = _CACHE_TTL_SEC) -> None:
    """淘汰过期和超量的缓存条目。"""
    now = time.time()
    expired = [k for k, v in cache.items() if now - v[1] > ttl]
    for k in expired:
        del cache[k]
    if len(cache) > max_entries:
        oldest = sorted(cache.items(), key=lambda x: x[1][1])[:len(cache) - max_entries]
        for k, _ in oldest:
            del cache[k]


def _load_persona() -> str:
    from app.memory.persona_files import get_default_persona_content
    return get_default_persona_content()


async def _get_obs_archive(client: httpx.AsyncClient, group_id: str) -> str:
    """Get or create the observer archive for this group (for KB accumulation)."""
    now = time.time()
    entry = _obs_archive_cache.get(group_id)
    if entry:
        _obs_archive_cache[group_id] = (entry[0], now)
        return entry[0]
    r = await client.post(f"{CHATBOT_URL}/v1/archives", json={"name": f"kb-{group_id}"})
    if r.status_code in (200, 201):
        aid = r.json()["archive_id"]
    else:
        aid = f"obs_{group_id}"
    _obs_archive_cache[group_id] = (aid, now)
    _cache_prune(_obs_archive_cache)
    return aid


async def _ensure_persona(client: httpx.AsyncClient, archive_id: str) -> None:
    """Ensure persona is set on the given archive."""
    if archive_id in _persona_ensured:
        return
    r = await client.get(f"{CHATBOT_URL}/v1/archives/{archive_id}/persona")
    if r.status_code == 404:
        await client.put(
            f"{CHATBOT_URL}/v1/archives/{archive_id}/persona",
            json={"content": _load_persona()},
        )
        log.info("persona set for archive %s", archive_id)
    _persona_ensured.add(archive_id)


async def _check_participate(client: httpx.AsyncClient, group_id: str) -> tuple[bool, str | None]:
    """
    Check if bot should participate in this group.
    Returns (participate: bool, active_archive_id: str | None).
    Cache result for 10 seconds.
    """
    now = time.time()
    entry = _participate_cache.get(group_id)
    if entry and now - entry[1] < 2:
        return entry[0], _chat_archive_cache.get(group_id, (None, 0))[0]

    try:
        r = await client.get(f"{CHATBOT_URL}/v1/bot/groups/{group_id}")
        if r.status_code == 200:
            data = r.json()
            part = data.get("participate", False)
            aid = data.get("active_archive_id")
            _participate_cache[group_id] = (part, now)
            if aid:
                _chat_archive_cache[group_id] = (aid, now)
            _cache_prune(_participate_cache)
            _cache_prune(_chat_archive_cache)
            return part, aid
    except Exception:
        pass
    return False, None


def _message_addressed_to_bot(body: dict, bot_qq: str = "") -> bool:
    """Check if message is addressed to the bot via @mention.

    Checks both raw_message (string) and message (array) formats.
    Only returns True if the bot's QQ is specifically the target.
    """
    if not bot_qq:
        # Cannot determine bot QQ -- don't reply at all (safe fallback)
        return False

    # 1. Check message array format (more reliable)
    msg_array = body.get("message", [])
    if isinstance(msg_array, list):
        for seg in msg_array:
            if isinstance(seg, dict) and seg.get("type") == "at":
                qq = str(seg.get("data", {}).get("qq", ""))
                if qq == bot_qq:
                    return True

    # 2. Check raw_message text format
    raw = body.get("raw_message", "")
    if isinstance(raw, str) and raw:
        if f"[CQ:at,qq={bot_qq}]" in raw or f"[CQ:at,qq={bot_qq} " in raw:
            return True

    return False


async def _get_admin_group(client: httpx.AsyncClient) -> str | None:
    """Get the configured admin group ID. Cached for 30 seconds."""
    global _admin_group_cache
    import time
    now = time.time()
    if _admin_group_cache and (now - _admin_group_cache[1]) < 30:
        return _admin_group_cache[0]
    try:
        r = await client.get(f"{CHATBOT_URL}/v1/bot/admin-group")
        if r.status_code == 200:
            gid = r.json().get("admin_group_id")
            _admin_group_cache = (gid, now)
            return gid
    except Exception:
        pass
    return None


_CQ_MEDIA_RE = re.compile(r"\[CQ:(image|record|video|file),([^\]]*)\]")
_CQ_KV_RE = re.compile(r"(?:^|,)([a-zA-Z_][a-zA-Z0-9_]*)=([^,\]]*)")
_MEDIA_TYPE_LABEL = {"image": "图片", "record": "语音", "video": "视频", "file": "文件"}
_MEDIA_DEFAULT_EXT = {"image": "png", "record": "amr", "video": "mp4", "file": "bin"}
_MEDIA_PREFIX = {"image": "img", "record": "voice", "video": "video", "file": "file"}
_PIC_DATA_BY_FILE: dict[str, dict] = {}


def _message_contains_local_media(message: str) -> bool:
    msg = (message or "").lower()
    return any(tag in msg for tag in ("[本地image:", "[本地record:", "[本地video:", "[本地file:"))


def _message_contains_remote_media(message: str) -> bool:
    return bool(_CQ_MEDIA_RE.search(message or ""))


def _message_contains_media(message: str) -> bool:
    return _message_contains_local_media(message) or _message_contains_remote_media(message)


def _message_mentions_recent_image(message: str) -> bool:
    text = (message or "").lower()
    if not text:
        return False
    if any(s in text for s in ("新的图", "新图", "刚发的图", "刚才的图", "上面的图", "这张图", "那个图")):
        return True
    return "图" in text and any(s in text for s in ("新", "刚", "这", "那", "上面", "刚才"))


def _remember_observed_media(group_id: str, user_id: str, raw_message: str) -> None:
    if not _message_contains_media(raw_message):
        return
    _RECENT_OBSERVED_MEDIA[(group_id, user_id)] = (time.time(), raw_message)
    if len(_RECENT_OBSERVED_MEDIA) > 512:
        now = time.time()
        for key, (ts, _) in list(_RECENT_OBSERVED_MEDIA.items()):
            if now - ts > _RECENT_OBSERVED_MEDIA_TTL:
                _RECENT_OBSERVED_MEDIA.pop(key, None)


def _attach_recent_media_if_referenced(group_id: str, user_id: str, raw_message: str) -> str:
    if _message_contains_media(raw_message) or not _message_mentions_recent_image(raw_message):
        return raw_message
    item = _RECENT_OBSERVED_MEDIA.get((group_id, user_id))
    if not item:
        return raw_message
    ts, media_msg = item
    if time.time() - ts > _RECENT_OBSERVED_MEDIA_TTL:
        _RECENT_OBSERVED_MEDIA.pop((group_id, user_id), None)
        return raw_message
    return f"{media_msg}\n{raw_message}"


async def _observe_message(
    client: httpx.AsyncClient,
    archive_id: str,
    group_id: str,
    user_id: str,
    user_name: str,
    content: str,
    addressed_bot: bool,
) -> None:
    """Save message to group_messages for KB accumulation."""
    try:
        await client.post(
            f"{CHATBOT_URL}/v1/archives/{archive_id}/groups/{group_id}/observe",
            json={
                "archive_id": archive_id,
                "group_id": group_id,
                "user_id": user_id,
                "user_name": user_name,
                "content": content,
                "addressed_bot": addressed_bot,
            },
        )
    except Exception:
        log.debug("observe failed (non-critical)")


async def _sync_group_files_fire_and_forget(
    group_id: str,
    archive_id: str,
) -> None:
    """Fire-and-forget: sync new group files to KB in the background.

    Patch 13: 用共享 client(避免每次新建 connection),timeout 显式设 60s
    (这是一个普通 POST,不是 SSE 流,可以有 read 超时)。
    """
    try:
        client = get_http_client()
        r = await client.post(
            f"{CHATBOT_URL}/v1/archives/{archive_id}/groups/{group_id}/group-files/sync",
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
        )
        if r.status_code == 200:
            cnt = r.json().get("synced", 0)
            if cnt:
                log.info("group files synced: group=%s count=%d", group_id, cnt)
    except Exception:
        log.debug("group file sync failed (non-critical)")


async def _chat_and_reply(
    client: httpx.AsyncClient,
    archive_id: str,
    group_id: str,
    user_id: str,
    user_name: str,
    message: str,
    *,
    client_msg_id: str = "",
) -> tuple[str | None, list[dict], bool, bool, str]:
    """Run full chat pipeline. Returns (reply_text, files_list, voice_reply, suppress_text, voice_reply_file).

    Handles "thinking" SSE events: sends an immediate quick-reply
    (e.g. "让我想想...") for hard questions, then collects the real reply.
    Captures files from the "done" event for QQ upload.

    Patch 10: client_msg_id 传给后端做幂等去重(NapCat 重发同一条消息不会重跑流水线)
    Patch 13: 不传 timeout (用 client 默认 read=None,SSE 长任务不再 120s 断流)
    """
    reply_parts: list[str] = []
    thinking_sent: bool = False
    generated_files: list[dict] = []
    last_error: str = ""
    voice_reply = False
    suppress_text = False
    voice_reply_file: str = ""
    payload = {
        "archive_id": archive_id,
        "group_id": group_id,
        "user_id": user_id,
        "user_name": user_name,
        "message": message,
        **({"client_msg_id": client_msg_id} if client_msg_id else {}),
    }

    async def _inject_pending_interrupt_message() -> bool:
        try:
            resp = await client.post(
                f"{CHATBOT_URL}/v1/chat/interrupt_message",
                json=payload,
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                return bool(data.get("ok"))
        except Exception:
            log.debug("interrupt message injection failed (non-critical)")
        return False

    try:
        async with client.stream(
            "POST",
            f"{CHATBOT_URL}/v1/chat/stream",
            json=payload,
        ) as resp:
            # 检查 HTTP 状态码
            if resp.status_code == 409:
                last_error = "chatbot busy (409), message dropped"
                log.warning(last_error)
            elif resp.status_code >= 400:
                last_error = f"chatbot returned {resp.status_code}"
                log.error(last_error)

            current_event: str = ""
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    current_event = line[7:].strip()
                elif line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    if current_event == "thinking" and not thinking_sent:
                        thinking_text = data.get("text", "")
                        if thinking_text:
                            at_prefix = f"[CQ:at,qq={user_id}] "
                            try:
                                await client.post(
                                    f"{NAPCAT_URL}/send_group_msg",
                                    json={
                                        "group_id": int(group_id),
                                        "message": at_prefix + thinking_text,
                                    },
                                )
                                thinking_sent = True
                                log.info("thinking sent: group=%s user=%s", group_id, user_id)
                            except Exception:
                                log.debug("thinking reply failed (non-critical)")
                    elif current_event in ("progress", "intermediate_reply"):
                        llm_msg = data.get("message", "")
                        if llm_msg and data.get("persona_safe") is True:
                            try:
                                await client.post(
                                    f"{NAPCAT_URL}/send_group_msg",
                                    json={
                                        "group_id": int(group_id),
                                        "message": llm_msg,
                                    },
                                )
                                log.info(
                                    "%s sent: group=%s msg=%s",
                                    current_event, group_id, llm_msg[:60],
                                )
                            except Exception:
                                log.debug("%s msg failed (non-critical)", current_event)
                        elif llm_msg:
                            log.debug(
                                "%s suppressed: group=%s kind=%s msg=%s",
                                current_event, group_id, data.get("kind"), llm_msg[:60],
                            )
                    elif current_event == "token":
                        reply_parts.append(data.get("text", ""))
                    elif current_event == "abort_marker":
                        await _inject_pending_interrupt_message()
                    elif current_event == "done":
                        if "files" in data:
                            generated_files = data.get("files") or []
                        voice_reply = bool(data.get("voice_reply", False))
                        suppress_text = bool(data.get("_suppress_text", False))
                        voice_reply_file = str(data.get("voice_reply_file", ""))
                    elif current_event == "error":
                        last_error = data.get("message", "unknown error")
                        log.error("chatbot error: %s", last_error)
    except Exception as e:
        log.error("chat stream failed: %s", e)
        return None, [], False, False, ""

    if last_error and not reply_parts:
        return f"[{last_error}]", generated_files, voice_reply, suppress_text, voice_reply_file

    reply = "".join(reply_parts).strip()
    return (reply or None), generated_files, voice_reply, suppress_text, voice_reply_file


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_AUDIO_EXTS = {".mp3", ".wav", ".amr", ".silk", ".ogg"}
_VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv"}


# Patch 11: 可执行文件扩展名 -- 这些文件不自动 upload_group_file,
# 改成发警告链接,要求用户主动确认下载。
_EXEC_EXTS = {
    ".exe", ".dll", ".out", ".o", ".obj",
    ".bat", ".cmd", ".ps1", ".sh",
    ".msi", ".com", ".scr",
    ".jar", ".apk", ".dmg", ".deb", ".rpm",
}


def _detect_media_type(fname: str) -> str:
    decision = classify_file_for_delivery(fname)
    if not decision.allowed:
        return "blocked"
    if decision.delivery_kind == "image":
        return "image"
    if decision.delivery_kind == "voice":
        return "record"
    ext = os.path.splitext(fname)[1].lower()
    if ext in _VIDEO_EXTS:
        return "video"
    return "file"


async def _napcat_ok(resp: httpx.Response) -> bool:
    if resp.status_code != 200:
        return False
    try:
        data = resp.json()
    except Exception:
        return True
    if isinstance(data, dict):
        retcode = data.get("retcode")
        status_value = data.get("status")
        if retcode is not None and retcode != 0:
            return False
        if isinstance(status_value, str) and status_value.lower() not in ("ok", "async"):
            return False
    return True


async def _send_generated_files(
    client: httpx.AsyncClient,
    group_id: str,
    files: list[dict],
    voice_reply_file: str = "",
) -> bool:
    """Send generated files to QQ group.

    Images → inline CQ image (NapCat downloads from URL)
    Voice reply (voice_reply_file match) → WAV→AMR via ffmpeg, then [CQ:record]
    Voice files (non-match .wav/.mp3 etc.) → upload_group_file
    Executables → 警告 + 链接(不自动 upload_group_file,防群成员双击执行 RCE)
    Other files → upload_group_file with local path (falls back to download link)

    Returns: True if a voice message was sent successfully, False otherwise.
    """
    voice_sent = False
    for f in files:
        fname = f.get("name", "file")
        url = f.get("url", "")
        local_path = f.get("local_path", "")
        if not url:
            continue
        file_url = f"{CHATBOT_URL}{url}"
        media_type = _detect_media_type(fname)
        # Round2 tts 工具生成的文件 → 走文件上传; Round3 auto-voice → 走语音条
        if media_type == "record" and fname != voice_reply_file:
            media_type = "file"
        try:
            if media_type == "image":
                # 图片：内联 CQ 代码发送（NapCat 下载后显示）
                cq_code = f"[CQ:image,file={file_url}]"
                resp = await client.post(
                    f"{NAPCAT_URL}/send_group_msg",
                    json={"group_id": int(group_id), "message": cq_code},
                    timeout=30.0,
                )
                if await _napcat_ok(resp):
                    log.info("image sent inline: group=%s name=%s", group_id, fname)
                else:
                    log.warning("image send failed: %s status=%d body=%s", fname, resp.status_code, resp.text[:300])
            elif media_type == "record":
                # 语音/音频：先转 AMR，再用 [CQ:record] 发送
                amr_path = None
                if local_path and os.path.isfile(local_path):
                    amr_path = await _convert_to_amr(local_path)
                if amr_path:
                    abs_path = os.path.abspath(amr_path).replace("\\", "/")
                    cq_code = f"[CQ:record,file=file:///{abs_path}]"
                    resp = await client.post(
                        f"{NAPCAT_URL}/send_group_msg",
                        json={"group_id": int(group_id), "message": cq_code},
                        timeout=30.0,
                    )
                    if await _napcat_ok(resp):
                        log.info("voice sent (AMR): group=%s name=%s", group_id, fname)
                        voice_sent = True
                        # 清理临时 AMR 文件
                        if amr_path != local_path:
                            try:
                                os.remove(amr_path)
                            except OSError:
                                pass
                    else:
                        log.warning("voice send failed: %s status=%d body=%s", fname, resp.status_code, resp.text[:300])
                        # 清理失败的临时文件
                        if amr_path != local_path:
                            try:
                                os.remove(amr_path)
                            except OSError:
                                pass
                elif local_path and os.path.isfile(local_path):
                    # AMR 转换失败，用原始 WAV 尝试
                    abs_path = os.path.abspath(local_path).replace("\\", "/")
                    cq_code = f"[CQ:record,file=file:///{abs_path}]"
                    resp = await client.post(
                        f"{NAPCAT_URL}/send_group_msg",
                        json={"group_id": int(group_id), "message": cq_code},
                        timeout=30.0,
                    )
                    if await _napcat_ok(resp):
                        log.info("voice sent (WAV fallback): group=%s name=%s", group_id, fname)
                        voice_sent = True
                    else:
                        log.warning("voice send failed (WAV): %s status=%d body=%s", fname, resp.status_code, resp.text[:300])
                elif url:
                    # 无本地路径时用 URL 尝试
                    cq_code = f"[CQ:record,file={file_url}]"
                    resp = await client.post(
                        f"{NAPCAT_URL}/send_group_msg",
                        json={"group_id": int(group_id), "message": cq_code},
                        timeout=30.0,
                    )
                    if await _napcat_ok(resp):
                        log.info("voice sent via URL: group=%s name=%s", group_id, fname)
                        voice_sent = True
                    else:
                        log.warning("voice send URL failed: %s status=%d body=%s", fname, resp.status_code, resp.text[:300])
            elif media_type == "blocked":
                decision = classify_file_for_delivery(fname)
                log.info(
                    "blocked file skipped (not delivered): group=%s name=%s reason=%s",
                    group_id, fname, decision.reason,
                )
                try:
                    await client.post(
                        f"{NAPCAT_URL}/send_group_msg",
                        json={
                            "group_id": int(group_id),
                            "message": f"[文件未发送] {fname}\n原因：{decision.reason}",
                        },
                        timeout=10.0,
                    )
                except Exception:
                    log.exception("blocked-file notice failed: group=%s name=%s", group_id, fname)
            elif local_path and os.path.isfile(local_path):
                # 其他文件：用本地路径上传到群文件
                resp = await client.post(
                    f"{NAPCAT_URL}/upload_group_file",
                    json={"group_id": int(group_id), "file": local_path, "name": fname},
                    timeout=60.0,
                )
                if await _napcat_ok(resp):
                    log.info("file uploaded: group=%s name=%s path=%s", group_id, fname, local_path)
                else:
                    log.warning("file upload failed: %s status=%d body=%s -- retrying via download+reupload", fname, resp.status_code, resp.text[:300])
                    if not await _download_and_reupload(client, group_id, fname, file_url):
                        await _send_file_link_fallback(client, group_id, fname, file_url)
            else:
                # 无本地路径：先尝试下载后上传，再回退到链接
                if not await _download_and_reupload(client, group_id, fname, file_url):
                    await _send_file_link_fallback(client, group_id, fname, file_url)
        except Exception as e:
            log.error("file send failed: %s error=%s", fname, e)
            try:
                await _send_file_link_fallback(client, group_id, fname, file_url)
            except Exception:
                pass
    return voice_sent


async def _convert_to_amr(wav_path: str) -> str | None:
    """Convert WAV to AMR narrowband using ffmpeg. Returns path to AMR file, or None."""
    import subprocess
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ffmpeg = os.path.join(script_dir, "ominvioce", "python", "ffmpeg", "bin", "ffmpeg.exe")
    if not os.path.isfile(ffmpeg):
        ffmpeg = "ffmpeg"
    amr_path = wav_path.rsplit(".", 1)[0] + ".amr"
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-i", wav_path,
            "-ac", "1", "-ar", "8000", "-b:a", "12.2k",
            amr_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0 and os.path.isfile(amr_path) and os.path.getsize(amr_path) > 0:
            log.info("WAV→AMR converted: %s → %s (%d bytes)", wav_path, amr_path, os.path.getsize(amr_path))
            return amr_path
        else:
            stderr_text = stderr.decode("utf-8", errors="replace")[:200] if stderr else ""
            log.warning("WAV→AMR conversion failed: rc=%d stderr=%s", proc.returncode, stderr_text)
            if os.path.isfile(amr_path):
                try:
                    os.remove(amr_path)
                except OSError:
                    pass
            return None
    except Exception as e:
        log.warning("WAV→AMR conversion error: %s", e)
        return None


async def _download_and_reupload(
    client: httpx.AsyncClient,
    group_id: str,
    fname: str,
    file_url: str,
) -> bool:
    """Download the file from chatbot and re-upload to QQ group. Returns True on success."""
    import tempfile
    import os as _os
    _tmp_dir = _os.path.join(tempfile.gettempdir(), "napcat_bridge_uploads")
    _os.makedirs(_tmp_dir, exist_ok=True)
    _tmp_path = _os.path.join(_tmp_dir, fname)
    try:
        # Download from chatbot file endpoint
        full_url = f"{CHATBOT_URL}{file_url}" if file_url.startswith("/") else file_url
        log.info("download+reupload: fetching %s → %s", full_url[:120], _tmp_path)
        dl_resp = await client.get(full_url, timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0))
        if dl_resp.status_code != 200:
            log.warning("download+reupload: GET %s returned %d", full_url[:120], dl_resp.status_code)
            return False
        content = dl_resp.content
        if not content or len(content) == 0:
            log.warning("download+reupload: empty body from %s", full_url[:120])
            return False
        with open(_tmp_path, "wb") as f:
            f.write(content)
        log.info("download+reupload: saved %d bytes to %s", len(content), _tmp_path)
        # Re-upload via NapCat
        resp = await client.post(
            f"{NAPCAT_URL}/upload_group_file",
            json={"group_id": int(group_id), "file": _tmp_path, "name": fname},
            timeout=60.0,
        )
        ok = await _napcat_ok(resp)
        if ok:
            log.info("download+reupload: uploaded group=%s name=%s", group_id, fname)
        else:
            log.warning("download+reupload: upload_group_file failed status=%d body=%s", resp.status_code, resp.text[:300])
        return ok
    except Exception:
        log.exception("download+reupload failed: group=%s name=%s", group_id, fname)
        return False
    finally:
        try:
            if _os.path.isfile(_tmp_path):
                _os.remove(_tmp_path)
        except OSError:
            pass


async def _send_file_link_fallback(
    client: httpx.AsyncClient,
    group_id: str,
    fname: str,
    file_url: str,
) -> None:
    """Fallback: send a download link for the file."""
    download_msg = f"[文件] {fname}\n下载：{file_url}"
    await client.post(
        f"{NAPCAT_URL}/send_group_msg",
        json={"group_id": int(group_id), "message": download_msg},
        timeout=10.0,
    )


# ── 入站消息去重(防 NapCat 重试打断正在处理的消息) ────────────
# NapCat HTTP 重试同一条 message_id 时,bridge 若不加去重会:
#   1. 发现 lock.locked() → 发 abort 打断正在处理的原始请求
#   2. 把重试消息入队 → 原始请求被 abort 杀掉 → 用户收不到回复
#   3. 重试消息到达 chat API 后被 cmd_id 幂等挡下 → 队列空洞
# 修法: 用进程内 LRU 追踪最近见过的 message_id,重试直接 ack 不入队。

_SEEN_MSG_TTL = 120.0   # 2 分钟内同 message_id 视为重试
_SEEN_MSG_MAX = 2000
_seen_msg_ids: dict[str, float] = {}  # message_id → 首次出现时间


def _is_duplicate_message(message_id: str) -> bool:
    """检测是否为 NapCat 重试。重试直接 ack,不给 chatbot 发 abort。"""
    if not message_id:
        return False
    now = time.time()
    first_seen = _seen_msg_ids.get(message_id, 0.0)
    if first_seen and (now - first_seen) < _SEEN_MSG_TTL:
        return True
    _seen_msg_ids[message_id] = now
    # 惰性清理: 超量时淘汰一半
    if len(_seen_msg_ids) > _SEEN_MSG_MAX:
        stale = [k for k, v in _seen_msg_ids.items() if now - v > _SEEN_MSG_TTL]
        for k in stale:
            del _seen_msg_ids[k]
        if len(_seen_msg_ids) > _SEEN_MSG_MAX:
            # 仍超量,淘汰最旧的一半
            sorted_items = sorted(_seen_msg_ids.items(), key=lambda x: x[1])
            for k, _ in sorted_items[:len(_seen_msg_ids) // 2]:
                del _seen_msg_ids[k]
    return False


# ── 入站媒体自动下载 ─────────────────────────────────────────────
# 用户通过 QQ 发送的图片/语音/视频存在 QQ CDN 上,URL 带有时效性 rkey。
# 如果直接传给 LLM,等 LLM 执行工具去下载时 rkey 早已过期。
# 这里在消息入队前就把媒体下载到本地工作区,并替换 raw_message 中的远程 URL。

_MEDIA_DOWNLOAD_TIMEOUT = 30.0
_MEDIA_DOWNLOAD_ATTEMPTS = 3
_MEDIA_DOWNLOAD_RETRY_DELAYS = (0.5, 1.5)
_MAX_MEDIA_DOWNLOAD_SIZE = 50 * 1024 * 1024  # 50 MB


def _extract_cq_media_segments(raw_message: str) -> list[dict]:
    out: list[dict] = []
    for m in _CQ_MEDIA_RE.finditer(raw_message or ""):
        data = {km.group(1): html.unescape(km.group(2)) for km in _CQ_KV_RE.finditer(m.group(2))}
        out.append({"type": m.group(1), "data": data})
    return out


def _cache_pic_data(file_name: str, data: dict, message_time: object = 0) -> None:
    cached = dict(data)
    cached["message_time"] = message_time
    _PIC_DATA_BY_FILE[file_name] = cached
    _PIC_DATA_BY_FILE[file_name.lower()] = cached
    if len(_PIC_DATA_BY_FILE) > 512:
        _PIC_DATA_BY_FILE.clear()


def _remember_pic_metadata_from_body(body: dict) -> None:
    try:
        msg_array = body.get("message", [])
        if isinstance(msg_array, list):
            for seg in msg_array:
                if not isinstance(seg, dict) or seg.get("type") != "image":
                    continue
                data = seg.get("data", {}) if isinstance(seg.get("data"), dict) else {}
                file_name = str(data.get("file", "") or "").strip()
                if file_name:
                    _cache_pic_data(file_name, data, body.get("time", 0))

        for el in body.get("elements", []) or []:
            if not isinstance(el, dict):
                continue
            pic = el.get("picElement")
            if not isinstance(pic, dict):
                continue
            file_name = str(pic.get("fileName", "") or pic.get("file", "") or "").strip()
            if not file_name:
                continue
            data = {
                "file": file_name,
                "file_size": pic.get("fileSize", "") or pic.get("file_size", ""),
                "md5HexStr": pic.get("md5HexStr", "") or pic.get("md5", ""),
                "sourcePath": pic.get("sourcePath", ""),
                "fileUuid": pic.get("fileUuid", ""),
                "originImageUrl": pic.get("originImageUrl", ""),
                "width": pic.get("picWidth", 0),
                "height": pic.get("picHeight", 0),
            }
            _cache_pic_data(file_name, data, body.get("time", 0))
    except Exception:
        log.debug("remember pic metadata failed", exc_info=True)


async def _download_image_via_pic_source(seg_data: dict, dst_dir: str, default_ext: str, prefix: str, stable_stem: str) -> str | None:
    local_src = str(seg_data.get("sourcePath", "") or seg_data.get("source_path", "") or "").strip()
    if local_src:
        log.info("incoming image trying local sourcePath: %s", local_src)
        copied = _copy_existing_media_file(local_src, dst_dir, default_ext, prefix, stable_stem)
        if copied:
            return copied
    origin = str(seg_data.get("originImageUrl", "") or "").strip()
    file_uuid = str(seg_data.get("fileUuid", "") or seg_data.get("file_uuid", "") or "").strip()
    if origin and origin.startswith("/"):
        origin = f"https://multimedia.nt.qq.com.cn{origin}"
    elif not origin and file_uuid:
        origin = f"https://multimedia.nt.qq.com.cn/download?appid=1407&fileid={file_uuid}&spec=0"
    if origin:
        log.info("incoming image trying origin/fileUuid download: %s...", origin[:160])
        return await _download_single_media(origin, dst_dir, default_ext, prefix, stable_stem)
    return None


def _incoming_media_segments(body: dict, raw_message: str) -> list[dict]:
    out: list[dict] = []
    msg_array = body.get("message", [])
    if isinstance(msg_array, list):
        for seg in msg_array:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type", "")
            if seg_type not in _MEDIA_TYPE_LABEL:
                continue
            seg_data = seg.get("data", {}) if isinstance(seg.get("data"), dict) else {}
            file_name = str(seg_data.get("file", "") or "").strip()
            if seg_type == "image" and file_name:
                cached = _PIC_DATA_BY_FILE.get(file_name) or _PIC_DATA_BY_FILE.get(file_name.lower())
                if cached:
                    merged = dict(seg_data)
                    merged.update({k: v for k, v in cached.items() if v not in (None, "")})
                    seg_data = merged
            if seg_data.get("url") or seg_data.get("path") or seg_data.get("file"):
                out.append({"type": seg_type, "data": seg_data})
    seen = {(item.get("type"), item.get("data", {}).get("url"), item.get("data", {}).get("file")) for item in out}
    for seg in _extract_cq_media_segments(raw_message):
        data = seg.get("data", {})
        key = (seg.get("type"), data.get("url"), data.get("file"))
        if (key[1] or key[2]) and key not in seen:
            out.append(seg)
            seen.add(key)
    return out


def _media_failure_marker(seg_type: str, seg_data: dict, reason: str) -> str:
    label = _MEDIA_TYPE_LABEL.get(seg_type, "媒体")
    file_name = str(seg_data.get("file", "") or "unknown")
    return f"[本地{seg_type}: 下载失败({label}:{file_name}; reason={reason}; 当前这条媒体未下载成功)]"


def _replace_first_cq_in_text(text: str, cq_type: str, replacement: str) -> str:
    return re.sub(r'\[CQ:' + re.escape(cq_type) + r'[^\]]*\]', replacement, text, count=1)


def _replace_cq_with_marker(text: str, cq_type: str, url: str, replacement: str) -> str:
    if url:
        pattern = r'\[CQ:' + re.escape(cq_type) + r'[^\]]*' + re.escape(url) + r'[^\]]*\]'
        new_text = re.sub(pattern, replacement, text)
        if new_text != text:
            return new_text
    return _replace_first_cq_in_text(text, cq_type, replacement)


def _media_stable_stem(seg_type: str, seg_data: dict, url: str = "") -> str:
    import hashlib
    ids = [
        str(seg_data.get("file_id", "") or seg_data.get("fileid", "") or "").strip(),
        str(seg_data.get("file_unique", "") or seg_data.get("file_unique_id", "") or "").strip(),
        str(seg_data.get("fileUuid", "") or seg_data.get("file_uuid", "") or "").strip(),
    ]
    for value in ids:
        if value:
            return hashlib.sha256(f"{seg_type}:id:{value}".encode("utf-8", "ignore")).hexdigest()[:32]

    raw_file = str(seg_data.get("file", "") or seg_data.get("name", "") or "").strip()
    size = str(seg_data.get("file_size", "") or seg_data.get("size", "") or "").strip()
    ts = str(seg_data.get("time", "") or seg_data.get("upload_time", "") or seg_data.get("modify_time", "") or "").strip()
    md5 = str(seg_data.get("md5", "") or seg_data.get("md5HexStr", "") or "").strip().lower()
    if md5:
        return hashlib.sha256(f"{seg_type}:md5:{md5}".encode("utf-8", "ignore")).hexdigest()[:32]
    if raw_file and size and ts:
        return hashlib.sha256(f"{seg_type}:nzt:{raw_file}|{size}|{ts}".encode("utf-8", "ignore")).hexdigest()[:32]
    if raw_file and size and url:
        base = url.split("&rkey=", 1)[0].split("?rkey=", 1)[0]
        return hashlib.sha256(f"{seg_type}:nzu:{raw_file}|{size}|{base}".encode("utf-8", "ignore")).hexdigest()[:32]
    if raw_file and size:
        return hashlib.sha256(f"{seg_type}:nz:{raw_file}|{size}".encode("utf-8", "ignore")).hexdigest()[:32]
    if raw_file:
        return hashlib.sha256(f"{seg_type}:name:{raw_file}".encode("utf-8", "ignore")).hexdigest()[:32]
    if url:
        base = url.split("&rkey=", 1)[0].split("?rkey=", 1)[0]
        return hashlib.sha256(f"{seg_type}:url:{base}".encode("utf-8", "ignore")).hexdigest()[:32]
    return hashlib.sha256(repr(sorted(seg_data.items())).encode("utf-8", "ignore")).hexdigest()[:32]


def _media_preferred_ext(seg_data: dict, default_ext: str) -> str:
    raw_file = str(seg_data.get("file", "") or seg_data.get("name", "") or "").strip()
    ext = Path(raw_file).suffix.lower().lstrip(".")
    if ext:
        return ext
    return default_ext


def _existing_stable_media(dst_dir: str, prefix: str, stem: str) -> str | None:
    try:
        for path in Path(dst_dir).glob(f"{prefix}_{stem}.*"):
            if path.is_file() and path.stat().st_size > 0:
                return str(path)
    except Exception:
        return None
    return None


def _copy_existing_media_file(src: str, dst_dir: str, default_ext: str, prefix: str, stem: str = "") -> str | None:
    import shutil
    import uuid
    try:
        if not src:
            return None
        src_path = Path(src)
        if not src_path.is_file():
            return None
        if src_path.stat().st_size <= 0 or src_path.stat().st_size > _MAX_MEDIA_DOWNLOAD_SIZE:
            return None
        ext = src_path.suffix.lower().lstrip(".") or default_ext
        fname = f"{prefix}_{stem}.{ext}" if stem else f"{prefix}_{uuid.uuid4().hex[:8]}.{ext}"
        local_path = os.path.join(dst_dir, fname)
        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            return local_path
        shutil.copy2(src_path, local_path)
        return local_path
    except Exception:
        log.warning("media local copy failed: src=%s", src, exc_info=True)
        return None


async def _index_inline_image_for_dream(
    *,
    archive_id: str,
    group_id: str,
    user_id: str,
    user_name: str,
    local_path: str,
    seg_data: dict,
    reused: bool,
) -> None:
    try:
        p = Path(local_path)
        if not p.is_file():
            return
        rel = f"_downloaded_media/{p.name}"
        existing = await pool().fetchval(
            """
            SELECT id FROM cold_nodes
            WHERE archive_id = $1
              AND group_id = $2
              AND scope = 'kb'
              AND node_type = 'file'
              AND file_metadata->>'workspace_path' = $3
            LIMIT 1
            """,
            archive_id, group_id, rel,
        )
        if existing:
            if not reused:
                try:
                    from app.core.dream import event_bus
                    await event_bus.emit(
                        "file_uploaded",
                        archive_id=archive_id,
                        group_id=group_id,
                        file_id=existing,
                        file_name=p.name,
                        ext=p.suffix.lower(),
                    )
                except Exception:
                    pass
            return

        tz = timezone(timedelta(hours=8))
        ts_val = int(time.time())
        ts = datetime.fromtimestamp(ts_val, tz=tz).strftime("%Y-%m-%d %H:%M")
        file_size = p.stat().st_size
        original_name = str(seg_data.get("file", "") or p.name)
        cid = f"c_{ulid.ULID()}"
        headline = sanitize_headline(f"{user_name} 发送了图片 {p.name}")
        content = sanitize_summary(
            f"{user_name} 于 {ts} 发送图片「{p.name}」。后台正在 OCR 和索引图片内容。"
        )
        file_meta = json.dumps({
            "filename": p.name,
            "original_filename": original_name,
            "workspace_path": rel,
            "archive_id": archive_id,
            "group_id": group_id,
            "upload_time": ts_val,
            "uploader_name": user_name,
            "uploader_uin": user_id,
            "file_size": file_size,
            "download_status": "done",
            "source": "inline_image",
            "napcat_file": original_name,
        }, ensure_ascii=False)

        await pool().execute(
            """
            INSERT INTO cold_nodes
                (id, archive_id, group_id, user_id, scope,
                 node_type, headline, content,
                 salience, source_refs, file_metadata)
            VALUES ($1, $2, $3, $4, 'kb', 'file', $5, $6, $7, $8::jsonb, $9)
            """,
            cid, archive_id, group_id, user_id,
            headline, content, 0.5, json.dumps([]), file_meta,
        )
        try:
            from app.core.dream import event_bus
            await event_bus.emit(
                "file_uploaded",
                archive_id=archive_id,
                group_id=group_id,
                file_id=cid,
                file_name=p.name,
                ext=p.suffix.lower(),
            )
        except Exception:
            pass
        log.info("inline image indexed for dream OCR: node=%s path=%s", cid, rel)
    except Exception:
        log.warning("inline image dream index failed: path=%s", local_path, exc_info=True)


async def _download_incoming_media(
    body: dict,
    raw_message: str,
    archive_id: str,
    group_id: str,
    user_id: str = "",
    user_name: str = "",
) -> str:
    """Download media files (images/records/videos) from incoming message to local workspace.

    Returns modified raw_message with [CQ:...] codes replaced by local file references.
    """
    media_segments = _incoming_media_segments(body, raw_message)
    if not media_segments:
        if _message_contains_remote_media(raw_message):
            log.warning("incoming media present but no downloadable url: group=%s msg=%s", group_id, raw_message[:300])
        return raw_message

    try:
        from app.llm.tools.workspace import _get_workspace_root
        ws_root = _get_workspace_root()
    except Exception:
        log.exception("incoming media skipped: workspace root unavailable group=%s", group_id)
        return raw_message

    media_dir = ws_root / archive_id / group_id / "_downloaded_media"
    media_dir.mkdir(parents=True, exist_ok=True)

    modified = raw_message
    for seg in media_segments:
        seg_type = seg.get("type", "")
        seg_data = seg.get("data", {}) if isinstance(seg.get("data"), dict) else {}
        url = seg_data.get("url", "") or seg_data.get("file_url", "") or seg_data.get("download_url", "") if seg_data else ""
        local_src = seg_data.get("path", "") or seg_data.get("file_path", "") if seg_data else ""
        default_ext = _media_preferred_ext(seg_data, _MEDIA_DEFAULT_EXT.get(seg_type, "bin"))
        prefix = _MEDIA_PREFIX.get(seg_type, "media")
        stable_stem = _media_stable_stem(seg_type, seg_data, url)
        local_path = _existing_stable_media(str(media_dir), prefix, stable_stem)
        reused = bool(local_path)
        if not local_path and local_src:
            local_path = _copy_existing_media_file(local_src, str(media_dir), default_ext, prefix, stable_stem)
        if not local_path and seg_type == "image":
            local_path = await _download_image_via_pic_source(
                seg_data, str(media_dir), default_ext, prefix, stable_stem
            )
        if not local_path and url:
            local_path = await _download_single_media(
                url, str(media_dir), default_ext, prefix=prefix, stable_stem=stable_stem
            )
        if local_path:
            modified = _replace_cq_in_text(modified, seg_type, url, local_path)
            if seg_type == "image":
                await _index_inline_image_for_dream(
                    archive_id=archive_id,
                    group_id=group_id,
                    user_id=user_id,
                    user_name=user_name,
                    local_path=local_path,
                    seg_data=seg_data,
                    reused=reused,
                )
            if reused:
                log.info("incoming %s reused: %s", seg_type, local_path)
            else:
                log.info("incoming %s saved: %s", seg_type, local_path)
        else:
            reason = "download_failed" if url else "no_url_or_local_path"
            marker = _media_failure_marker(seg_type, seg_data, reason)
            modified = _replace_cq_with_marker(modified, seg_type, url, marker)
            log.warning(
                "incoming %s unavailable: group=%s file=%s reason=%s url=%s local=%s",
                seg_type, group_id, seg_data.get("file", ""), reason, url[:160], local_src,
            )

    return modified


async def _download_single_media(
    url: str, dst_dir: str, default_ext: str, prefix: str, stable_stem: str = ""
) -> str | None:
    """Download a single media file. Returns local path or None."""
    import uuid
    retryable_errors = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError)
    client = get_http_client()
    last_error: Exception | None = None
    for attempt in range(1, _MEDIA_DOWNLOAD_ATTEMPTS + 1):
        try:
            resp = await client.get(
                url,
                timeout=httpx.Timeout(connect=10.0, read=_MEDIA_DOWNLOAD_TIMEOUT, write=10.0, pool=10.0),
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Referer": "https://multimedia.nt.qq.com.cn",
                },
            )
            if resp.status_code != 200:
                log.warning(
                    "media download failed: status=%d attempt=%d/%d url=%s...",
                    resp.status_code, attempt, _MEDIA_DOWNLOAD_ATTEMPTS, url[:160],
                )
                if resp.status_code in (408, 425, 429, 500, 502, 503, 504) and attempt < _MEDIA_DOWNLOAD_ATTEMPTS:
                    await asyncio.sleep(_MEDIA_DOWNLOAD_RETRY_DELAYS[min(attempt - 1, len(_MEDIA_DOWNLOAD_RETRY_DELAYS) - 1)])
                    continue
                return None

            content = resp.content
            if len(content) > _MAX_MEDIA_DOWNLOAD_SIZE or len(content) == 0:
                log.warning("media download rejected: size=%d url=%s...", len(content), url[:160])
                return None

            # 从 Content-Type 推断扩展名
            ct = resp.headers.get("content-type", "")
            ext = default_ext
            if "jpeg" in ct or "jpg" in ct:
                ext = "jpg"
            elif "png" in ct:
                ext = "png"
            elif "gif" in ct:
                ext = "gif"
            elif "webp" in ct:
                ext = "webp"
            elif "amr" in ct:
                ext = "amr"
            elif "mp4" in ct:
                ext = "mp4"
            elif "mpeg" in ct:
                ext = "mp3"

            fname = f"{prefix}_{stable_stem}.{ext}" if stable_stem else f"{prefix}_{uuid.uuid4().hex[:8]}.{ext}"
            local_path = os.path.join(dst_dir, fname)
            if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                return local_path
            with open(local_path, "wb") as f:
                f.write(content)
            return local_path
        except retryable_errors as e:
            last_error = e
            if attempt < _MEDIA_DOWNLOAD_ATTEMPTS:
                log.warning(
                    "media download retryable error: attempt=%d/%d error=%s url=%s...",
                    attempt, _MEDIA_DOWNLOAD_ATTEMPTS, type(e).__name__, url[:160],
                )
                await asyncio.sleep(_MEDIA_DOWNLOAD_RETRY_DELAYS[min(attempt - 1, len(_MEDIA_DOWNLOAD_RETRY_DELAYS) - 1)])
                continue
            log.warning(
                "media download error after retries for url=%s... last_error=%s",
                url[:160], repr(last_error), exc_info=True,
            )
            return None
        except Exception:
            log.warning("media download error for url=%s...", url[:160], exc_info=True)
            return None
    return None


def _replace_cq_in_text(
    text: str, cq_type: str, url: str, local_path: str
) -> str:
    """Replace [CQ:type,...url=URL...] with a local file reference in text."""
    import re
    local_name = os.path.basename(local_path)
    local_dir = os.path.dirname(local_path)

    # 1. 精确替换含有该 url 的 [CQ:...] 段
    #    [CQ:image,file=...,url=THE_URL,...]
    pattern = r'\[CQ:' + re.escape(cq_type) + r'[^\]]*' + re.escape(url) + r'[^\]]*\]'
    replacement = f"[本地{cq_type}: {local_name}]"
    new_text = re.sub(pattern, replacement, text)

    # 2. 如果没匹配到(URL 可能被截断),也尝试替换不含该 URL 的对应 CQ 段(只替换第一个)
    if new_text == text:
        pattern2 = r'\[CQ:' + re.escape(cq_type) + r'[^\]]*\]'
        # 只替换第一个匹配
        new_text = re.sub(pattern2, replacement, text, count=1)

    return new_text


@app.post("/napcat/callback")
async def napcat_callback(request: Request):
    body = await request.json()
    _remember_pic_metadata_from_body(body)
    log.info("recv: %s", json.dumps(body, ensure_ascii=False)[:400])

    if body.get("post_type") != "message" or body.get("message_type") != "group":
        return JSONResponse({"status": "ignored"})

    group_id = str(body["group_id"])
    user_id = str(body["user_id"])
    user_name = body.get("sender", {}).get("nickname", "unknown")
    raw_message = body.get("raw_message", "").strip()
    raw_message = html.unescape(raw_message)  # Decode HTML entities from napcat

    if not raw_message:
        return JSONResponse({"status": "empty"})

    # Patch 10: 用 NapCat 的 message_id 做幂等键(NapCat 重发同条消息 message_id 不变)
    nc_msg_id = str(body.get("message_id", "")).strip()
    if not nc_msg_id:
        # fallback:用 self_id + time + user_id 拼一个
        nc_msg_id = f"{body.get('self_id', '')}_{body.get('time', '')}_{user_id}"

    # 入站去重: NapCat 重试同一条 message_id 时直接 ack,不入队不 abort
    if _is_duplicate_message(nc_msg_id):
        log.info("duplicate message ignored: msg_id=%s group=%s user=%s",
                 nc_msg_id, group_id, user_id)
        return JSONResponse({"status": "ok"})

    # Patch 13: 用模块级共享 client(connection pool 复用 + read=None for SSE 长任务)
    client = get_http_client()
    try:
        # Step 0: Check for admin botctl commands
        msg_lower = raw_message.lower().lstrip()
        if msg_lower.startswith("botctl"):
            admin_gid = await _get_admin_group(client)
            if admin_gid and group_id == admin_gid:
                cmd = raw_message.strip()[6:].strip()
                cmd_lower = cmd.lower()
                # 禁止从群内移除 admin 群----否则自己把自己权限删了
                if cmd_lower.startswith("admin") and any(
                    kw in cmd_lower for kw in ("off", "none", "remove", "clear")
                ):
                    await client.post(
                        f"{NAPCAT_URL}/send_group_msg",
                        json={
                            "group_id": int(group_id),
                            "message": "[botctl] 不能从群内移除 admin 群（会删掉自己权限），请直接用 CLI 操作：python botctl_helper.py admin off",
                        },
                    )
                    return JSONResponse({"status": "ok"})

                # ── Multi-turn session check ──────────────────────
                session = _botctl_sessions.get(user_id)
                if session:
                    ts, stage_cmd, stage_args = session
                    if time.time() - ts > _SESSION_TTL:
                        del _botctl_sessions[user_id]
                    else:
                        # New known command → cancel old session
                        cmd_first = cmd.strip().split()[0].lower() if cmd.strip() else ""
                        if cmd_first in _KNOWN_BOTCTL_CMDS:
                            del _botctl_sessions[user_id]
                        else:
                            # Continue session with user's choice
                            set_pending_input(cmd.strip())
                            full_cmd = f"{stage_cmd} {' '.join(stage_args)}"
                            log.info("botctl session continue: user=%s stage=%s choice=%s",
                                     user_id, stage_cmd, cmd.strip())
                            result = await asyncio.to_thread(run_command, full_cmd)
                            del _botctl_sessions[user_id]
                            # Guard against nested await (unlikely)
                            if "__BOTCTL_AWAIT__" in result:
                                new_parts = full_cmd.strip().split()
                                _botctl_sessions[user_id] = (time.time(), new_parts[0].lower(), new_parts[1:])
                                result = result.replace("__BOTCTL_AWAIT__", "").strip()
                            if result:
                                if len(result) > 4000:
                                    result = result[:4000] + "\n... (truncated)"
                                await client.post(
                                    f"{NAPCAT_URL}/send_group_msg",
                                    json={"group_id": int(group_id), "message": result},
                                )
                            return JSONResponse({"status": "ok"})

                log.info("admin cmd: group=%s user=%s cmd=%s", group_id, user_id, cmd)
                result = await asyncio.to_thread(run_command, cmd)

                # Check if command is awaiting multi-turn input
                if "__BOTCTL_AWAIT__" in result:
                    parts = cmd.strip().split()
                    _botctl_sessions[user_id] = (time.time(), parts[0].lower(), parts[1:])
                    result = result.replace("__BOTCTL_AWAIT__", "").strip()
                    if not result:
                        result = "[botctl] 请回复选项（例如: botctl 1）"

                if len(result) > 4000:
                    result = result[:4000] + "\n... (truncated)"
                await client.post(
                    f"{NAPCAT_URL}/send_group_msg",
                    json={"group_id": int(group_id), "message": result},
                )
                return JSONResponse({"status": "ok"})
            else:
                return JSONResponse({"status": "ignored"})

        # Step 1: Check participation
        participate, active_aid = await _check_participate(client, group_id)
        if not participate or not active_aid:
            return JSONResponse({"status": "ignored"})

        # Step 2: Check if addressed to bot (must @mention bot's QQ specifically)
        bot_qq = str(body.get("self_id", ""))
        at_bot = _message_addressed_to_bot(body, bot_qq)

        # Step 2.5: Download incoming media (images/records/videos) to local workspace
        # 必须在 observe + chat 之前下载,否则 LLM 拿到的是过期的 QQ CDN URL
        raw_message = await _download_incoming_media(
            body, raw_message, active_aid, group_id, user_id, user_name,
        )

        # Step 3: Always observe (save to KB) -- use active_aid, same archive chat reads from
        await _observe_message(client, active_aid, group_id, user_id, user_name, raw_message, at_bot)
        _remember_observed_media(group_id, user_id, raw_message)

        # Step 3.5: Sync group files (fire-and-forget) -- use active_aid for same reason
        from app.core.bg_tasks import schedule

        schedule(
            _sync_group_files_fire_and_forget(group_id, active_aid),
            name=f"bridge.sync_files:{group_id}",
        )

        # Step 4: Only process chat if bot was @mentioned
        if not at_bot:
            # ── 2026-05-04 Bug #13 修复 ──
            # 用户着急想停时常忘记 @,旧版 not at_bot 直接 return,把"停/别/算了"
            # 这种短 stop 命令静默吞掉 → 主线程继续跑 32 分钟。
            # 修法:若该 (group, user) 当前正忙,且消息看起来是短 stop 命令
            # (≤8 字符 + 含 stop 关键词),仍然发 abort 信号,只是不入对话队列。
            # 这是高置信度低成本的修复:短消息 + 自己正忙 = 几乎不可能误伤。
            try:
                _stripped = (raw_message or "").strip()
                _looks_stop = (
                    0 < len(_stripped) <= 8
                    and any(
                        kw in _stripped for kw in (
                            "停", "别", "算了", "等等", "不用", "中断",
                            "stop", "Stop", "STOP", "cancel", "Cancel",
                        )
                    )
                )
                pkey_check: _PendKey = (group_id, user_id)
                _busy = pkey_check in _processing_lock and _processing_lock[pkey_check].locked()
                if _looks_stop and _busy:
                    try:
                        await client.post(
                            f"{CHATBOT_URL}/v1/chat/abort",
                            json={
                                "archive_id": active_aid,
                                "group_id": group_id,
                                "user_id": user_id,
                            },
                            timeout=5.0,
                        )
                        log.info(
                            "stop-bypass abort sent (no @ but looks like stop, user busy): "
                            "group=%s user=%s msg=%r",
                            group_id, user_id, _stripped,
                        )
                    except Exception:
                        log.debug("stop-bypass abort failed (non-critical)")
            except Exception:
                # 任何异常不影响 observed 路径返回
                log.debug("stop-bypass check raised (non-critical)", exc_info=True)
            return JSONResponse({"status": "observed"})

        # Step 5: per-user 排队 + 处理。
        #
        # 旧版 per-group: 同群所有 user 共一把锁 + 一条队列,trace ea1a8826
        # 一个用户的 13 分钟训练任务把整群所有人堵死。
        # 新版 per-user: 键 (group_id, user_id),不同用户互不阻塞。abort 信号
        # 也只对同 user 发,不会误打断别人。
        pkey: _PendKey = (group_id, user_id)
        queued_message = _attach_recent_media_if_referenced(group_id, user_id, raw_message)
        _pending_queue[pkey].append((user_id, user_name, queued_message))
        _touch_pkey(pkey)
        lock = _processing_lock[pkey]

        if lock.locked():
            # 同 user 正在跑 -- 先查当前所处阶段再决定行为。
            # round3 是人设流式回复阶段,abort 会让用户收不到任何文字;
            # 此时只排队不打断,等 round3 自然结束后 while 循环自动消费队列。
            current_stage = ""
            try:
                stage_resp = await client.get(
                    f"{CHATBOT_URL}/v1/chat/stage",
                    params={
                        "archive_id": active_aid,
                        "group_id": group_id,
                        "user_id": user_id,
                    },
                    timeout=3.0,
                )
                if stage_resp.status_code == 200:
                    current_stage = (stage_resp.json() or {}).get("stage", "")
            except Exception:
                log.debug("stage check failed (non-critical), proceeding with abort")

            if current_stage == "round3":
                # Round 3 正在流式生成回复 -- 不打断,只排队
                log.info(
                    "queued (round3 streaming, no abort): group=%s user=%s",
                    group_id, user_id,
                )
                return JSONResponse({"status": "queued"})

            # 非 round3 阶段(Round 2 规划/工具执行) -- 发 abort + 尝试注入新消息
            injected = False
            try:
                payload = {
                    "archive_id": active_aid,
                    "group_id": group_id,
                    "user_id": user_id,
                    "message": queued_message,
                    **({"client_msg_id": nc_msg_id} if nc_msg_id else {}),
                }
                resp = await client.post(
                    f"{CHATBOT_URL}/v1/chat/interrupt_message",
                    json=payload,
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    injected = bool((resp.json() or {}).get("ok"))
            except Exception:
                log.debug("interrupt message injection failed before abort (non-critical)")
            if not injected:
                _abort_injected_messages[pkey].append(queued_message)

            try:
                await client.post(
                    f"{CHATBOT_URL}/v1/chat/abort",
                    json={
                        "archive_id": active_aid,
                        "group_id": group_id,
                        "user_id": user_id,
                    },
                    timeout=5.0,
                )
                log.info("abort signal sent: group=%s user=%s injected=%s", group_id, user_id, injected)
            except Exception:
                log.debug("abort signal failed (non-critical)")

            if injected:
                try:
                    _pending_queue[pkey].pop()
                except IndexError:
                    pass
            log.info("queued (same-user busy): group=%s user=%s injected=%s", group_id, user_id, injected)
            return JSONResponse({"status": "queued", "injected": injected})

        # Process all queued messages for THIS (group, user). 同 user 串行,
        # 进入处理过程中又来同 user 的新消息会再次入队,这里用 while True 兜住。
        async with lock:
            _currently_processing.add(pkey)
            try:
                while True:
                    # Collect all pending messages for this (group, user)
                    pending: list[tuple[str, str, str]] = []
                    while _pending_queue[pkey]:
                        try:
                            _item = _pending_queue[pkey].popleft()
                            _msg = _item[2]
                            _skip = False
                            if _abort_injected_messages[pkey]:
                                try:
                                    if _abort_injected_messages[pkey][0] == _msg:
                                        _abort_injected_messages[pkey].popleft()
                                        _skip = True
                                except IndexError:
                                    pass
                            if not _skip:
                                pending.append(_item)
                        except IndexError:
                            break

                    if not pending:
                        # 双检: 给"刚刚 append 完、还没来得及检查 lock"的并发 callback
                        # 一个让出机会;这处理"A 即将退出循环、释放锁,A2 已经 append
                        # 但还没去 check lock"的窄窗口竞态----若我们直接 break 就会
                        # 把 A2 的消息留在队列里没人处理。sleep(0) 让出后再读一次
                        # 队列;真空了再 break。
                        await asyncio.sleep(0)
                        if _pending_queue[pkey]:
                            continue
                        break

                    # 同 user 的多条消息合并(同一用户连发 N 条 → 一次回复)
                    uname = pending[-1][1]   # 用最近一次的 user_name
                    msgs = [m for _, _, m in pending]
                    combined = "\n".join(msgs) if len(msgs) > 1 else msgs[0]
                    log.info("processing: group=%s user=%s msgs=%d",
                             group_id, user_id, len(msgs))
                    combined = _attach_recent_media_if_referenced(group_id, user_id, combined)

                    await _ensure_persona(client, active_aid)
                    reply, files, voice_reply, suppress_text, voice_reply_file = await _chat_and_reply(
                        client, active_aid, group_id, user_id, uname, combined,
                        client_msg_id=nc_msg_id,
                    )

                    # ── 语音回复: 先发语音,失败则回退文字 ──
                    voice_sent = False
                    if files:
                        voice_sent = await _send_generated_files(client, group_id, files, voice_reply_file)

                    if voice_reply and suppress_text:
                        if voice_sent:
                            log.info("voice reply mode: voice sent, text suppressed")
                        elif reply:
                            # 语音发送失败 → 回退到文字消息
                            log.warning("voice send failed, falling back to text reply")
                            at_prefix = f"[CQ:at,qq={user_id}] "
                            if len(msgs) > 1:
                                reply = f"(回复以上{len(msgs)}条消息)\n{reply}"
                            resp = await client.post(
                                f"{NAPCAT_URL}/send_group_msg",
                                json={"group_id": int(group_id), "message": at_prefix + reply},
                            )
                            if resp.status_code == 200:
                                log.info("sent (voice fallback): group=%s user=%s len=%d",
                                         group_id, user_id, len(reply))
                            else:
                                log.error("send_group_msg failed: status=%d body=%s",
                                          resp.status_code, resp.text[:300])
                    elif reply:
                        at_prefix = f"[CQ:at,qq={user_id}] "
                        if len(msgs) > 1:
                            reply = f"(回复以上{len(msgs)}条消息)\n{reply}"
                        resp = await client.post(
                            f"{NAPCAT_URL}/send_group_msg",
                            json={"group_id": int(group_id), "message": at_prefix + reply},
                        )
                        if resp.status_code == 200:
                            log.info("sent: group=%s user=%s len=%d",
                                     group_id, user_id, len(reply))
                        else:
                            log.error("send_group_msg failed: status=%d body=%s",
                                      resp.status_code, resp.text[:300])
            finally:
                _currently_processing.discard(pkey)

        return JSONResponse({"status": "ok"})
    except Exception:
        log.exception("napcat_callback failed unexpectedly")
        return JSONResponse({"status": "error"}, status_code=500)


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
