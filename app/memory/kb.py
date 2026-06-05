"""
知识库（KB）。

数据模型：复用 cold_nodes scope='kb'。结构与用户/群组冷记忆完全一致——
KB 也是图（节点 + 边），可被 expand_kb 工具展开。

数据来源：group_messages 表（所有群消息，含机器人未参与的）。

压缩时机：
  - 后台维护任务检查 group_messages 中 kb_processed=false 的数量，
    >= settings.kb_compress_threshold 时触发一次压缩
  - 每次压缩 settings.kb_compress_batch 条最早的消息
  - 压缩成功后标记 kb_processed=true（保留原文档案），节点写入 cold_nodes
"""
from __future__ import annotations

from app.memory.prompt_catalog import (
    KB_COMPRESS_SYSTEM,
    KB_FILE_INDEX_SYSTEM,
    KB_GROUP_FILE_INDEX_SYSTEM,
)
_KB_COMPRESS_SYSTEM = KB_COMPRESS_SYSTEM
_FILE_INDEX_SYSTEM = KB_FILE_INDEX_SYSTEM
_GROUP_FILE_INDEX_SYSTEM = KB_GROUP_FILE_INDEX_SYSTEM


import asyncio
import json
import logging
import os

import ulid

from app.config import settings
from app.db.pool import pool
from app.llm import client as llm
from app.core.sanitize import sanitize_headline, sanitize_summary
from app.memory import group_messages as gm
from app.memory.cold import (
    _eff_sql, _eff_sql_aliased,
    expand_cold as _expand_cold_internal,
)


log = logging.getLogger(__name__)


# ── 索引读取（注入 system） ──────────────────────────────────

# 2026-05-10 Patch 54: stale placeholder 节点检测
# 病因(trace 52fd894afef34855):上传文件后的"占位"模板节点理论上 phase 2 后被
# lite 模型 summary 替换,但实际经常 stale,模型 expand_kb / search_files 命中
# 时 content/headline 是无意义模板 → 决策被误导,workspace_path 也常对不上。
# 修法:headline / content 含 placeholder 模板字样 → 视为 stale,过滤。
_PLACEHOLDER_HEADLINE_TEMPLATES = (
    " 上传了 ",  # "昵称 上传了 X" / "包涵 上传了 X"
    " uploaded ",
)
_PLACEHOLDER_CONTENT_KEYWORDS = (
    "正在后台下载和分析中",
    "下载可能还没完成",
    "摘要稍后就会出现在这里",
    "still being downloaded and indexed",
    "Treat this node as pending",
)


def _is_placeholder_content(headline: str | None, content: str | None) -> bool:
    """检测节点是否仍是 placeholder(未被 lite 摘要替换的初始模板)。

    判断顺序:
      - content 含 placeholder 模板关键词 → 立刻 True
      - content=None(只有 headline)→ 看 headline 是否模板格式
      - headline 含 " 上传了 " 模式且 content 含 placeholder 词 → True
      - 都不含 → False(真实 summary)
    """
    if content:
        c = content
        for kw in _PLACEHOLDER_CONTENT_KEYWORDS:
            if kw in c:
                return True
    if headline:
        h = headline
        for tpl in _PLACEHOLDER_HEADLINE_TEMPLATES:
            if tpl in h:
                # headline 是 "X 上传了 Y" 模板 — content 没传或不含 placeholder kw
                # 时仍然返回 True(headline 模板就是 placeholder 信号)
                if content is None:
                    return True
    return False


def _metadata_from_row(row) -> dict:
    raw = row.get("file_metadata") if hasattr(row, "get") else row["file_metadata"]
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _file_identity_keys(meta: dict) -> set[tuple[str, str]]:
    filename = str(meta.get("filename", "") or "").strip()
    if not filename:
        return set()
    uploader = str(meta.get("uploader_name", "") or "").strip()
    file_size = meta.get("file_size")
    keys = {("filename", filename)} if not uploader and file_size in (None, "") else set()
    if uploader:
        keys.add(("filename_uploader", f"{filename}\0{uploader}"))
    if file_size not in (None, ""):
        keys.add(("filename_size", f"{filename}\0{file_size}"))
    return keys


def _is_real_file_summary(row, meta: dict) -> bool:
    if _is_placeholder_content(row.get("headline"), row.get("content")):
        return False
    status = str(meta.get("download_status", "") or "")
    if status in {"pending", "failed"}:
        return False
    return status == "done" or bool(meta.get("workspace_path"))


def _is_stale_file_placeholder(row, meta: dict) -> bool:
    status = str(meta.get("download_status", "") or "")
    if status in {"pending", "failed"}:
        return True
    return _is_placeholder_content(row.get("headline"), row.get("content"))


async def cleanup_stale_file_placeholders(archive_id: str, group_id: str) -> int:
    """删除已被真实文件摘要覆盖的 stale/pending/failed placeholder 节点。"""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, headline, content, file_metadata
            FROM cold_nodes
            WHERE archive_id = $1 AND group_id = $2
              AND scope = 'kb' AND node_type = 'file'
            """,
            archive_id, group_id,
        )

        row_infos: list[tuple[str, set[tuple[str, str]], bool]] = []
        covered_keys: set[tuple[str, str]] = set()
        for r in rows:
            meta = _metadata_from_row(r)
            keys = _file_identity_keys(meta)
            if not keys:
                continue
            is_stale_candidate = _is_stale_file_placeholder(r, meta)
            if _is_real_file_summary(r, meta):
                covered_keys.update(keys)
            row_infos.append((r["id"], keys, is_stale_candidate))

        delete_ids = [
            node_id
            for node_id, keys, is_stale_candidate in row_infos
            if is_stale_candidate and keys & covered_keys
        ]
        if not delete_ids:
            return 0
        await conn.execute(
            """
            DELETE FROM cold_nodes
            WHERE archive_id = $1 AND group_id = $2 AND id = ANY($3::text[])
            """,
            archive_id, group_id, delete_ids,
        )
        return len(delete_ids)


async def load_kb_index(
    archive_id: str, group_id: str,
    *,
    viewer_user_id: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """
    KB 索引。viewer_user_id 用于解析"该用户视角下"的 avoid_mention 遮罩。

    2026-05-09 Patch 38:同 load_file_index,过滤 KB 主索引中污染的 file 节点
    (0B / 临时路径 / helper 内部产物),避免它们出现在 system prompt 中。
    非 file 节点(event/fact/preference 等)不受影响。
    """
    n = limit or settings.kb_index_topn
    # 多取一些以补偿过滤造成的损失
    _query_limit = int(n * 1.5) + 10
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT cn.id, cn.node_type, cn.headline, cn.salience,
                   cn.file_metadata,
                   (nua.node_id IS NOT NULL) AS avoid_mention,
                   nua.reason AS avoid_reason,
                   {_eff_sql_aliased('cn')} AS eff_salience
            FROM cold_nodes cn
            LEFT JOIN node_user_avoid nua
              ON nua.archive_id = cn.archive_id
             AND nua.node_id = cn.id
             AND nua.user_id = $4
            WHERE cn.archive_id = $1 AND cn.group_id = $2 AND cn.scope = 'kb'
            ORDER BY eff_salience DESC, cn.last_access DESC NULLS LAST
            LIMIT $3
            """,
            archive_id, group_id, _query_limit, viewer_user_id or "",
        )
    # 2026-05-09 Patch 38:file 节点过滤(同 load_file_index)
    _SKIP_PATH_PATTERNS = (
        ".temp/", ".prev/", "_helpers_shared/", "_delegate_",
        "/.temp/", "/.prev/", "/_helpers_shared/",
    )
    items: list[dict] = []
    _filtered = 0
    _filtered_placeholder = 0  # P54: stale placeholder 节点
    for r in rows:
        # 仅对 file 节点应用过滤;其他节点(event/fact/preference)直接保留
        if r["node_type"] == "file" and r["file_metadata"]:
            try:
                meta = json.loads(r["file_metadata"])
            except (json.JSONDecodeError, TypeError):
                meta = {}
            filename = meta.get("filename", "") or ""
            workspace_path = (meta.get("workspace_path", "") or "").replace("\\", "/")
            file_size = meta.get("file_size", 0) or 0
            if file_size <= 0:
                _filtered += 1
                continue
            if any(pat in workspace_path for pat in _SKIP_PATH_PATTERNS):
                _filtered += 1
                continue
            _bn = filename.lstrip()
            if _bn.startswith((".helper_", "_helper_", "._helper_")):
                _filtered += 1
                continue
            # 2026-05-10 Patch 54: 过滤 stale placeholder 节点
            # 病因(trace 52fd894afef34855):同文件可能存在 2 个 cold_node:
            #   - placeholder(headline="昵称 上传了 X",content="正在后台下载和分析中...")
            #   - fresh(headline=语义 summary,content=lite 模型生成的真实摘要)
            # placeholder 应在 phase 2 下载+lite 摘要后被替换,但时常 stale(数据不一致)
            # 模型 expand_kb 命中 placeholder 时 content 是无意义模板 → 决策被误导
            # search_files 也会命中 placeholder workspace_path → helper read_file 失败
            # 修法:任何阶段 content 含 placeholder 模板字样的节点全部过滤
            if _is_placeholder_content(r.get("headline"), None):
                # headline 是 "X 上传了 Y" 模板格式 → 还没生成 summary,大概率 placeholder
                _filtered_placeholder += 1
                continue
        items.append({
            "id": r["id"], "type": r["node_type"], "headline": r["headline"],
            "avoid_mention": bool(r["avoid_mention"]),
            "avoid_reason": r["avoid_reason"] or "",
        })
        if len(items) >= n:
            break  # 已凑够 n 条,可能还有 row 没遍历但不要了
    if _filtered or _filtered_placeholder:
        try:
            from app.core import debug as _dbg
            _dbg.log(
                "memory.kb.index.filtered",
                f"KB 主索引过滤了 {_filtered} 个污染 file 节点 + "
                f"{_filtered_placeholder} 个 stale placeholder",
            )
        except Exception:
            pass
    return items


async def load_file_index(
    archive_id: str, group_id: str,
    *,
    viewer_user_id: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """加载群组文件索引——所有 file 类型的 KB 节点，独立于主 KB 索引。

    文件节点 salience=0.5，在大量 KB 节点中可能被挤出 top-N。
    此函数单独加载全部文件节点，保证用户在问文件时一定能被模型看到。

    去重：同一个 (filename, uploader_name) 多次上传/同步可能产生多个节点
    （历史 sync 重试、用户重传等都会触发）。SQL 已按 created_at DESC 排序，
    Python 层按 (filename, uploader_name) 保留最新的一个，其余跳过——
    避免模型看到 `hello.c` × 3 个节点而并行 fetch 浪费工具调用。
    不同上传者上传的同名文件分别保留（可能内容不同）。
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT cn.id, cn.node_type, cn.headline, cn.content,
                   cn.salience, cn.file_metadata,
                   (nua.node_id IS NOT NULL) AS avoid_mention,
                   {_eff_sql_aliased('cn')} AS eff_salience
            FROM cold_nodes cn
            LEFT JOIN node_user_avoid nua
              ON nua.archive_id = cn.archive_id
             AND nua.node_id = cn.id
             AND nua.user_id = $4
            WHERE cn.archive_id = $1 AND cn.group_id = $2
              AND cn.scope = 'kb' AND cn.node_type = 'file'
            ORDER BY cn.created_at DESC
            LIMIT $3
            """,
            archive_id, group_id, limit, viewer_user_id or "",
        )
    items: list[dict] = []
    seen: set[tuple[str, str]] = set()
    # 2026-05-09 Patch 38 + Patch 43: KB 文件索引污染过滤
    # 病因(trace 779bbcf0):42 个文件中 16 个是 0B + 4 个含 .temp/.prev/_helpers_shared
    # 路径 + 12 个 NapCat file_id 失效(download_status=failed 永久错误)。这些
    # 被混进 system prompt → 模型每次启动都看到一堆"假可读"文件,helper 反复
    # fetch 失败浪费工具调用。
    # 修法:查询层过滤(立刻见效;DB 端污染数据保留,等运维清理):
    #   (a) file_size <= 0 → 跳过(0B 文件没价值)
    #   (b) workspace_path 含 .temp/.prev/_helpers_shared/_delegate_ → 临时副本,跳过
    #   (c) headline 形如 ".helper_*" / "_helper_*" → 内部产物,跳过
    #   (d) 2026-05-09 P43:download_status="failed" 且错误信息含"丢失 NapCat file_id"
    #       → 永久不可恢复,跳过(普通 failed 仍保留,可能是临时网络问题等下次重试)
    # 同时把过滤数量记 log 便于运维监控
    _SKIP_PATH_PATTERNS = (
        ".temp/", ".prev/", "_helpers_shared/", "_delegate_",
        "/.temp/", "/.prev/", "/_helpers_shared/",  # 反斜线 normalize 后的 / 形式
    )
    # P43 永久不可恢复的下载错误关键词
    # 2026-05-10 Patch 49(trace 859e363903b74432 验证):新加"无法获取下载链接"。
    # 病因:P43 原版只覆盖 sync_group_files 的 file_id 丢失场景,但 _bg_download_and_index
    # 在 NapCat 返回不到下载 URL 时调 _mark_download_failed(reason="无法获取下载链接"),
    # 这种失败下次重试也拿不到(NapCat 重启后 file_id 全死),实际是永久失败。trace
    # 859e363903b74432 显示 23 个文件中 12 个是这种状态污染 system prompt。
    _PERMANENT_FAIL_KEYWORDS = (
        "丢失 NapCat file_id", "无法重新下载",
        "无法获取下载链接",  # P49 新加
    )
    _filtered_zero_size = 0
    _filtered_temp_path = 0
    _filtered_helper_artifact = 0
    _filtered_perm_failed = 0
    _filtered_placeholder = 0  # P54: stale placeholder 节点
    for r in rows:
        meta = {}
        if r["file_metadata"]:
            try:
                meta = json.loads(r["file_metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        filename = meta.get("filename", "") or ""
        uploader = meta.get("uploader_name", "") or ""
        file_size = meta.get("file_size", 0) or 0
        workspace_path = meta.get("workspace_path", "") or ""
        download_status = meta.get("download_status", "done")
        download_error = meta.get("download_error", "") or ""
        # 过滤 (d, P43): 永久失败(file_id 丢失)
        if download_status == "failed" and any(
            kw in download_error for kw in _PERMANENT_FAIL_KEYWORDS
        ):
            _filtered_perm_failed += 1
            continue
        # 过滤 (a): 0B 文件
        if file_size <= 0:
            _filtered_zero_size += 1
            continue
        # 过滤 (b): 临时区/helper sandbox 路径
        _wp_norm = workspace_path.replace("\\", "/")
        if any(pat in _wp_norm for pat in _SKIP_PATH_PATTERNS):
            _filtered_temp_path += 1
            continue
        # 过滤 (c): 内部 helper 产物(以 . / _helper_ 开头的 basename)
        _bn = filename.lstrip()
        if _bn.startswith(".helper_") or _bn.startswith("_helper_") or _bn.startswith("._helper_"):
            _filtered_helper_artifact += 1
            continue
        # 2026-05-10 Patch 54: 过滤 stale placeholder 节点
        # 病因(trace 52fd894afef34855):同文件可能有 placeholder 节点(headline=
        # "X 上传了 Y",content=" 正在后台下载和分析中...")长期不被 phase 2 替换。
        # 这些会泄漏到 system prompt 的 file_index → 模型看到 ⏳ 状态但实际下载早就死了。
        if _is_placeholder_content(r.get("headline"), r.get("content")):
            _filtered_placeholder += 1
            continue
        # 去重 key:同 filename + 同 uploader 视为重复(保留最新——SQL DESC 第一个)
        # 空 filename 不去重(可能是异常节点,让模型看到)
        if filename:
            dedup_key = (filename, uploader)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
        items.append({
            "id": r["id"],
            "type": r["node_type"],
            "headline": r["headline"],
            "content": r["content"],
            "avoid_mention": bool(r["avoid_mention"]),
            "filename": filename,
            "uploader_name": uploader,
            "file_size": file_size,
            "workspace_path": workspace_path,
            "download_status": download_status,
            "download_error": download_error,
            "eff_salience": float(r["eff_salience"] or 0),
            # 2026-05-10 Patch 80 v2: 加 upload_time 字段供"本会话强时效"识别
            "upload_time": int(meta.get("upload_time", 0)) if meta else 0,
        })

    if (_filtered_zero_size or _filtered_temp_path or _filtered_helper_artifact
            or _filtered_perm_failed or _filtered_placeholder):
        try:
            from app.core import debug as _dbg
            _dbg.log(
                "memory.kb.file_index.filtered",
                f"过滤了 KB 文件索引污染:0B={_filtered_zero_size}, "
                f"临时路径={_filtered_temp_path}, helper 产物={_filtered_helper_artifact}, "
                f"永久失败={_filtered_perm_failed}, "
                f"placeholder={_filtered_placeholder}",  # P54
            )
        except Exception:
            pass  # debug 不可用时静默

    return items


# ── 展开 ────────────────────────────────────────────────────
async def expand_kb(
    archive_id: str, ids: list[str], depth: int = 1,
    *, viewer_user_id: str | None = None,
) -> list[dict]:
    """KB 展开复用冷记忆展开（共表）。仅返回 scope=kb 的节点。"""
    items = await _expand_cold_internal(
        archive_id, ids, depth, viewer_user_id=viewer_user_id,
    )
    return [it for it in items if it.get("scope") == "kb"]


# ── 压缩：群消息 → KB ───────────────────────────────────────


# #6 修:KB 压缩失败兜底
# 历史 bug:LLM 压缩失败 → return 0 → 同一 batch 下次再加载 → 又失败 → 死循环。
# 后果:group_messages 表里 kb_processed=FALSE 的会无限累积,记忆系统瘫痪。
# 分级回退:前 2 次保持重试(LLM 有时能修复);第 3 次失败 → 直接 dump 原文为
# cold 节点(低 salience 0.12),信息不丢,只是没 LLM 结构化,仍可通过 expand_kb 检索。
_kb_compress_failures: dict[tuple[str, str, frozenset], int] = {}
_kb_compress_small_batch_until: dict[tuple[str, str], float] = {}
_KB_COMPRESS_MAX_FAILURES = 3
_KB_COMPRESS_CONTEXT_SOFT_CHARS = 200_000
_KB_COMPRESS_MIN_BATCH = 8
_KB_COMPRESS_SMALL_BATCH_TTL_SEC = 600.0

# 2026-05-01: per-(archive, group) 互斥锁,防止 per-user 并行下双触发压缩。
# 旧版同群多个用户的 maintenance 任务可能同时调用 maybe_compress_kb,各自看到
# n_pending >= threshold 就开始 LLM 压缩,浪费 token 且后续 mark_processed 也
# 互相竞争。锁是 in-process 单例,单进程部署够用;水平扩展需要 Redis 分布式锁。
_kb_compress_locks: dict[tuple[str, str], asyncio.Lock] = {}
_kb_compress_locks_guard = asyncio.Lock()


async def _get_kb_compress_lock(archive_id: str, group_id: str) -> asyncio.Lock:
    key = (archive_id, group_id)
    async with _kb_compress_locks_guard:
        lock = _kb_compress_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _kb_compress_locks[key] = lock
        return lock


def _batch_signature(archive_id: str, group_id: str, batch: list[dict]) -> tuple:
    """batch 唯一标识:同一组消息 ID 集合"""
    return (archive_id, group_id, frozenset(int(m["id"]) for m in batch))


async def maybe_compress_kb(archive_id: str, group_id: str) -> int:
    """
    检查未处理消息数；超过阈值则压缩一批。返回新建节点数。
    被后台维护任务调用，可频繁调用（内部判断）。

    并发保护(2026-05-01): 同 (archive, group) 同时只允许一个 compress 任务
    在跑。第二个调用拿不到锁就直接返回 0,等下一轮维护再试。
    """
    # 拿锁——拿不到说明同 (archive, group) 已有压缩在跑,直接返回让它专心干
    lock = await _get_kb_compress_lock(archive_id, group_id)
    if lock.locked():
        log.debug("kb compress: another worker holds lock for %s/%s, skip",
                  archive_id, group_id)
        return 0

    async with lock:
        return await _maybe_compress_kb_inner(archive_id, group_id)


async def _maybe_compress_kb_inner(archive_id: str, group_id: str) -> int:
    async with pool().acquire() as conn:
        n_pending = await conn.fetchval(
            """
            SELECT COUNT(*) FROM group_messages
            WHERE archive_id = $1 AND group_id = $2
              AND kb_processed = FALSE AND COALESCE(kb_processing, 0) = 0
            """,
            archive_id, group_id,
        )
    key = (archive_id, group_id)
    small_batch_active = _kb_compress_small_batch_until.get(key, 0.0) > asyncio.get_running_loop().time()
    if n_pending < settings.kb_compress_threshold and not (
        small_batch_active and n_pending >= _KB_COMPRESS_MIN_BATCH
    ):
        return 0

    batch_limit = max(1, int(settings.kb_compress_batch))
    while True:
        batch = await gm.claim_unprocessed(
            archive_id, group_id, limit=batch_limit,
        )
        if not batch:
            return 0
        existing = await load_kb_index(archive_id, group_id, limit=200)
        text = _format_messages_for_kb(batch)
        existing_text = _format_existing(existing)
        if (
            len(text) + len(existing_text) <= _KB_COMPRESS_CONTEXT_SOFT_CHARS
            or batch_limit <= _KB_COMPRESS_MIN_BATCH
            or len(batch) <= _KB_COMPRESS_MIN_BATCH
        ):
            break
        await gm.release_processing(archive_id, group_id, [int(m["id"]) for m in batch])
        batch_limit = max(_KB_COMPRESS_MIN_BATCH, batch_limit // 2)
        _kb_compress_small_batch_until[(archive_id, group_id)] = (
            asyncio.get_running_loop().time() + _KB_COMPRESS_SMALL_BATCH_TTL_SEC
        )
        log.warning(
            "kb compress: batch context %d chars exceeds soft limit %d; retrying smaller batch=%d",
            len(text) + len(existing_text),
            _KB_COMPRESS_CONTEXT_SOFT_CHARS,
            batch_limit,
        )
    if not batch:
        return 0

    user_text = (
        f"## Messages To Consolidate ({len(batch)} entries)\n{text}\n\n"
        f"## Existing KB Nodes\n{existing_text}\n\n"
        "以上是待沉淀消息和已有知识库节点。"
    )

    msgs = [
        {"role": "system", "content": _KB_COMPRESS_SYSTEM},
        {"role": "user", "content": user_text},
    ]

    input_ids = {int(m["id"]) for m in batch}

    def _validate(raw):
        if not isinstance(raw, dict):
            return False
        consumed = raw.get("consumed_message_ids") or []
        try:
            consumed_set = {int(x) for x in consumed}
        except (TypeError, ValueError):
            return False
        # 完整性：consumed 必须严格等于输入
        if consumed_set != input_ids:
            return False
        # source_message_ids ⊂ input
        for items_key in ("nodes", "references_existing"):
            for item in (raw.get(items_key) or []):
                srcs = item.get("source_message_ids") or []
                try:
                    if not all(int(x) in input_ids for x in srcs):
                        return False
                except (TypeError, ValueError):
                    return False
        return True

    raw = await llm.chat_json_with_upgrade(msgs, validate=_validate, label="kb")
    if raw is None:
        # 分级回退策略(避免 KB pipeline 死循环且不丢信息):
        #   第 3 次失败 → 直接 dump 为 KB cold 节点(不用 LLM,保留原文)
        sig = _batch_signature(archive_id, group_id, batch)
        _kb_compress_failures[sig] = _kb_compress_failures.get(sig, 0) + 1
        fail_count = _kb_compress_failures[sig]
        if fail_count >= _KB_COMPRESS_MAX_FAILURES:
            # 回退:直接 dump batch 原文为 cold 节点,信息不丢
            log.warning(
                "kb compress: batch failed %d times for archive=%s group=%s; "
                "fallback raw dump %d messages to KB (low salience)",
                fail_count, archive_id, group_id, len(batch),
            )
            count = 0
            try:
                # 用格式化文本作为 content(与 LLM 看到的输入一致)
                dump_text = _format_messages_for_kb(batch)
                first_msg = batch[0] if batch else {}
                headline = (first_msg.get("content", "") or "")[:200] if first_msg else "(empty batch)"
                src_ids = [int(m["id"]) for m in batch]
                async with pool().acquire() as _conn:
                    async with _conn.transaction():
                        cid = f"c_{ulid.ULID()}"
                        await _conn.execute(
                            """
                            INSERT INTO cold_nodes
                                (id, archive_id, group_id, user_id, scope,
                                 node_type, headline, content,
                                 salience, source_refs)
                            VALUES ($1, $2, $3, NULL, 'kb', $4, $5, $6, $7, $8::jsonb)
                            """,
                            cid, archive_id, group_id,
                            "fact",
                            sanitize_headline(headline),
                            sanitize_summary(dump_text),
                            0.12,
                            json.dumps(src_ids),
                        )
                        count = 1
                # 2026-05-15 deadlock fix: gm.mark_processed 必须在事务释放写锁之后调用。
                # 它内部走 `pool().acquire()` 拿新连接,这条新连接的 auto-commit UPDATE
                # 会因为外层事务还持着 RESERVED 锁而被 SQLite 拒(busy_timeout 5s 后报
                # "database is locked")—— 实际是死锁:同一协程一边握着写锁、一边等
                # 另一条连接的写。挪到 `async with conn.transaction():` 外后,事务先
                # COMMIT 释放锁,再让 mark_processed 拿干净连接写。
                # 原子性其实之前就没有(两个连接两个事务,SQLite 没分布式事务),
                # 失败语义保持一致:外层失败 → 走 except 释放 processing 标记。
                await gm.mark_processed(archive_id, group_id, list(input_ids))
                log.info(
                    "kb compress: fallback dump created node %s for %d messages",
                    cid, len(batch),
                )
            except Exception:
                log.exception("kb fallback dump failed, messages preserved in queue")
                await gm.release_processing(archive_id, group_id, list(input_ids))
                return 0
            _kb_compress_failures.pop(sig, None)
            return count
        log.warning(
            "kb compress: lite+main both failed (attempt %d/%d); messages kept",
            fail_count, _KB_COMPRESS_MAX_FAILURES,
        )
        await gm.release_processing(archive_id, group_id, list(input_ids))
        return 0

    # 成功就清掉这个 batch 的失败计数(不会有了,但保险)
    sig = _batch_signature(archive_id, group_id, batch)
    _kb_compress_failures.pop(sig, None)

    new_nodes = raw.get("nodes") or []
    refs_existing = raw.get("references_existing") or []
    edges = raw.get("edges") or []

    valid_types = {"fact", "preference", "event", "relationship", "topic"}
    tmp_to_real: dict[str, str] = {}

    async with pool().acquire() as conn:
        async with conn.transaction():
            count = 0
            for n in new_nodes:
                tmp = str(n.get("tmp_id", "")).strip()
                if not tmp or tmp in tmp_to_real:
                    continue
                ntype = str(n.get("type", "fact"))
                if ntype not in valid_types:
                    ntype = "fact"
                cid = f"c_{ulid.ULID()}"
                tmp_to_real[tmp] = cid
                sal = float(n.get("salience_init", 0.5))
                sal = max(0.1, min(1.0, sal))
                src_ids = [int(x) for x in (n.get("source_message_ids") or [])]
                await conn.execute(
                    """
                    INSERT INTO cold_nodes
                        (id, archive_id, group_id, user_id, scope,
                         node_type, headline, content,
                         salience, source_refs)
                    VALUES ($1, $2, $3, NULL, 'kb', $4, $5, $6, $7, $8::jsonb)
                    """,
                    cid, archive_id, group_id,
                    ntype,
                    sanitize_headline(str(n.get("headline", ""))),
                    sanitize_summary(str(n.get("content", ""))),
                    sal,
                    json.dumps(src_ids),
                )
                count += 1

            for ref in refs_existing:
                node_ref = str(ref.get("node_ref", "")).strip()
                if not node_ref.startswith("c_"):
                    continue
                exists = await conn.fetchval(
                    """
                    SELECT 1 FROM cold_nodes
                    WHERE archive_id = $1 AND id = $2 AND scope = 'kb'
                      AND group_id = $3
                    """,
                    archive_id, node_ref, group_id,
                )
                if not exists:
                    continue
                src_ids = [int(x) for x in (ref.get("source_message_ids") or [])]
                # Merge new source_message_ids into source_refs (SQLite: manual merge)
                cur = await conn.fetchval(
                    "SELECT source_refs FROM cold_nodes WHERE archive_id = $1 AND id = $2",
                    archive_id, node_ref,
                )
                existing = json.loads(cur) if cur and isinstance(cur, str) else (cur if isinstance(cur, list) else [])
                merged = existing + [x for x in src_ids if x not in existing]
                await conn.execute(
                    """
                    UPDATE cold_nodes
                    SET salience = LEAST(1.0, salience + $3),
                        access_count = access_count + 1,
                        last_access = NOW(),
                        source_refs = $4,
                        updated_at = NOW()
                    WHERE archive_id = $1 AND id = $2
                    """,
                    archive_id, node_ref, settings.salience_access_boost,
                    json.dumps(merged),
                )

            for e in edges:
                src = _resolve_id(e.get("src"), tmp_to_real)
                dst = _resolve_id(e.get("dst"), tmp_to_real)
                if not src or not dst or src == dst:
                    continue
                w = float(e.get("weight", 1.0) or 1.0)
                w = max(0.0, min(1.0, w))
                ok = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM cold_nodes
                    WHERE archive_id = $1 AND id IN ($2, $3)
                    """,
                    archive_id, src, dst,
                )
                if ok != 2:
                    continue
                await conn.execute(
                    """
                    INSERT INTO cold_edges (archive_id, src_id, dst_id, weight)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (archive_id, src_id, dst_id)
                    DO UPDATE SET weight = COALESCE(GREATEST(cold_edges.weight, EXCLUDED.weight), cold_edges.weight, EXCLUDED.weight)
                    """,
                    archive_id, src, dst, w,
                )

    # 2026-05-15 deadlock fix: gm.mark_processed 走的是另一条 pool 连接,
    # 必须在外层 `conn.transaction()` 退出、写锁释放之后再调用。
    # 如果挪进事务体里,外层握着 RESERVED 锁 → mark_processed 的新连接
    # busy_timeout 5s 后报 "database is locked",实际是同协程死锁。
    # 原子性之前就是假象(两个连接两个事务),挪到外面不改变失败语义:
    # 外层事务失败 → 异常上抛 → 不会执行 mark_processed → 下个 cycle 重处理。
    # 标记消息已处理(但不删;保留原始档案)
    await gm.mark_processed(archive_id, group_id, list(input_ids))
    # 2026-05-16 Dream: emit 事件 (新增节点 → 触发 D21/D23 等 KB DAG 整理)
    if count > 0:
        try:
            from app.core.dream import event_bus
            await event_bus.emit("kb_nodes_added",
                                  archive_id=archive_id, group_id=group_id,
                                  count=count)
        except Exception:
            pass
    return count


# ── 文件知识库：生成文件索引与搜索 ─────────────────────────────



async def index_generated_files(
    archive_id: str,
    group_id: str,
    workspace_dir: str,
    generated_files: list[tuple[str, str]],
    user_message: str,
    bot_response: str,
) -> int:
    """Create KB entries for AI-generated files using LLM to generate Chinese descriptions."""
    if not generated_files:
        return 0

    file_list = "\n".join(f"- {fname}" for fname, _url in generated_files)
    user_text = (
        f"用户消息：{user_message[:500]}\n"
        f"机器人回复：{bot_response[:500]}\n\n"
        f"生成的文件列表：\n{file_list}\n\n"
        "为每个文件生成中文简短描述。"
    )

    try:
        raw = await llm.chat_json(
            [
                {"role": "system", "content": _FILE_INDEX_SYSTEM},
                {"role": "user", "content": user_text},
            ],
            reasoning="disabled",
            lite=True,
            metrics_tag="json.kb_file_index",
        )
    except Exception:
        log.exception("file indexing LLM failed")
        return 0

    file_descs = raw.get("files") or []
    if not isinstance(file_descs, list):
        return 0

    valid_fnames = {fname for fname, _url in generated_files}
    count = 0

    async with pool().acquire() as conn:
        async with conn.transaction():
            for fd in file_descs:
                fname = str(fd.get("filename", ""))
                if fname not in valid_fnames:
                    continue

                cid = f"c_{ulid.ULID()}"
                headline = sanitize_headline(str(fd.get("headline", fname)))
                content = sanitize_summary(str(fd.get("content", "")))
                file_meta = json.dumps({
                    "filename": fname,
                    "workspace_path": os.path.join(workspace_dir, fname),
                    "archive_id": archive_id,
                    "group_id": group_id,
                }, ensure_ascii=False)

                await conn.execute(
                    """
                    INSERT INTO cold_nodes
                        (id, archive_id, group_id, user_id, scope,
                         node_type, headline, content,
                         salience, source_refs, file_metadata)
                    VALUES ($1, $2, $3, NULL, 'kb', 'file', $4, $5, $6, $7::jsonb, $8)
                    """,
                    cid, archive_id, group_id,
                    headline, content, 0.5,
                    json.dumps([]),
                    file_meta,
                )
                count += 1

    log.info("file KB indexed: %d entries for archive=%s group=%s", count, archive_id, group_id)
    return count


async def search_files(
    archive_id: str, group_id: str, query: str, *, limit: int = 10,
) -> list[dict]:
    """Search file KB entries with case-insensitive multi-keyword matching.

    设计:query 按空白拆成 tokens,每个 token 对 headline/content 做 ILIKE。
    所有 token 必须各自匹配上(AND between tokens, OR within each token's targets)。
    解决了之前 LIKE 单串完全匹配 + 大小写敏感的检索失效问题。

    跨语言匹配靠 _FILE_INDEX_SYSTEM 强制要求双语标注 headline/content,
    所以模型搜 "RL environment" 能命中 content="强化学习环境(RL environment)..." 的文件。
    """
    limit = min(max(limit, 1), 50)
    # 拆 query 为关键词:中英文混合时按空白和标点拆开
    raw_tokens = [t.strip() for t in query.replace(",", " ").replace("、", " ").split()]
    tokens = [t for t in raw_tokens if len(t) >= 2]  # 单字过滤,避免噪声匹配
    if not tokens:
        # query 太短/全单字 fallback 到原始整串
        tokens = [query.strip()] if query.strip() else []
    if not tokens:
        return []

    # 构造动态 SQL:每个 token 一对 headline/content ILIKE,token 之间 AND
    # 例: WHERE archive_id=$1 AND group_id=$2 AND scope='kb' AND node_type='file'
    #     AND (headline ILIKE $3 OR content ILIKE $3)
    #     AND (headline ILIKE $4 OR content ILIKE $4)
    where_clauses = []
    sql_args: list = [archive_id, group_id]
    for i, tok in enumerate(tokens):
        idx = len(sql_args) + 1
        where_clauses.append(f"(headline ILIKE ${idx} OR content ILIKE ${idx})")
        sql_args.append(f"%{tok}%")
    sql_args.append(limit)
    limit_idx = len(sql_args)

    sql = f"""
        SELECT id, headline, content, file_metadata, salience, created_at
        FROM cold_nodes
        WHERE archive_id = $1 AND group_id = $2
          AND scope = 'kb' AND node_type = 'file'
          AND {' AND '.join(where_clauses)}
        ORDER BY salience DESC, created_at DESC
        LIMIT ${limit_idx}
    """

    async with pool().acquire() as conn:
        rows = await conn.fetch(sql, *sql_args)

    items = []
    _filtered_placeholder = 0  # P54: stale placeholder 节点
    for r in rows:
        meta = {}
        if r["file_metadata"]:
            try:
                meta = json.loads(r["file_metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        # 2026-05-10 Patch 54: 过滤 stale placeholder 节点
        # 防止 helper search_files 命中 placeholder workspace_path → read_file 失败
        # (trace fc4ed68e91ef4a89 gen_charts 的 file not found 即由此触发)
        if _is_placeholder_content(r.get("headline"), r.get("content")):
            _filtered_placeholder += 1
            continue
        items.append({
            "id": r["id"],
            "headline": r["headline"],
            "content": r["content"],
            "filename": meta.get("filename", ""),
            "workspace_path": meta.get("workspace_path", ""),
            "archive_id": meta.get("archive_id", ""),
            "group_id": meta.get("group_id", ""),
        })
    if _filtered_placeholder:
        try:
            from app.core import debug as _dbg
            _dbg.log(
                "memory.kb.search_files.filtered_placeholder",
                f"search_files 过滤了 {_filtered_placeholder} 个 stale placeholder 节点",
            )
        except Exception:
            pass
    return items


# ── 群文件索引 ─────────────────────────────────────────────────



async def index_group_file(
    archive_id: str,
    group_id: str,
    file_name: str,
    file_size: int,
    upload_time: int,
    uploader_name: str,
    workspace_path: str,
    content_snippet: str,
) -> str | None:
    """为群文件创建 KB 节点。返回 kb_node_id，失败返回 None。"""
    import time
    from datetime import datetime, timezone, timedelta

    tz = timezone(timedelta(hours=8))
    ts_val = upload_time if upload_time > 0 else int(time.time())
    ts = datetime.fromtimestamp(ts_val, tz=tz).strftime("%Y-%m-%d %H:%M")
    size_str = f"{file_size:,} 字节" if file_size < 1024 else (
        f"{file_size / 1024:.1f} KB" if file_size < 1024 * 1024 else
        f"{file_size / 1024 / 1024:.1f} MB"
    )

    snippet_block = ""
    if content_snippet:
        snippet_block = f"## Extracted Content Snippet\n```\n{content_snippet[:8000]}\n```\n\n内容片段。"
    else:
        snippet_block = "## Extracted Content Snippet\nNo file content was extracted because the file was too large, non-text, or unavailable.\n\n未提取到文件正文。"

    user_text = (
        f"## Shared File Metadata\n"
        f"filename: {file_name}\n"
        f"size: {size_str}\n"
        f"uploaded_at: {ts}\n"
        f"uploader: {uploader_name}\n\n"
        f"{snippet_block}"
    )

    # 2026-05-15 P67: 本地短路 — 无内容文件不调 LLM, 生成确定性模板。
    # 病因(实测 trace): 图片/视频/无文本 PDF 每个都送 LLM 生成相似的"未提取到文字内容..." 摘要,
    # 检索价值为 0 但消耗 lite LLM 配额。修法见 group_files.py 同一编号注释。
    _local_short_circuit_raw = None
    if not content_snippet:
        import os as _os_kb
        from app.llm.tools.workspace import _FILE_TYPE_TABLE as _ftt_kb
        _ext = _os_kb.path.splitext(file_name)[1].lower()
        _cat = None
        if _ext in _ftt_kb:
            _cat, _, _, _ = _ftt_kb[_ext]
        _NO_TEXT_CATS = {"image", "media", "archive", "binary"}
        if _cat in _NO_TEXT_CATS:
            _cat_label_map = {
                "image": "image", "media": "media file",
                "archive": "archive", "binary": "binary file",
            }
            _cat_label = _cat_label_map.get(_cat, _cat)
            _local_short_circuit_raw = {
                "headline": f"{_cat_label} {file_name}",
                "content": (
                    f"{uploader_name} uploaded `{file_name}` at {ts} ({size_str}). "
                    f"The detected file category is {_cat_label}. No plain text was extracted. "
                    "Use the matching image, OCR, media, or archive tool when content inspection is needed.\n\n"
                    "非纯文本文件；需要内容时使用匹配的图片、OCR、媒体或归档工具。"
                ),
            }
        elif _cat in {"document", "presentation", "spreadsheet"}:
            _local_short_circuit_raw = {
                "headline": f"no extracted text for {file_name}",
                "content": (
                    f"{uploader_name} uploaded `{file_name}` at {ts} ({size_str}). "
                    "The local extractor did not obtain body text. The file may be scanned, image-heavy, encrypted, or structurally complex. "
                    "Use OCR or a dedicated Office/PDF inspection path when its content matters.\n\n"
                    "本地未提取到正文；需要内容时使用 OCR 或专用 Office/PDF 检查。"
                ),
            }

    if _local_short_circuit_raw is not None:
        raw = _local_short_circuit_raw
    else:
        try:
            raw = await llm.chat_json(
                [
                    {"role": "system", "content": _GROUP_FILE_INDEX_SYSTEM},
                    {"role": "user", "content": user_text},
                ],
                reasoning="disabled",
                lite=True,
                metrics_tag="json.kb_group_file_index",
            )
        except Exception:
            log.exception("index_group_file LLM failed for %s", file_name)
            # fallback：模板总结
            raw = {
                "headline": f"{uploader_name} uploaded {file_name}",
                "content": (
                    f"{uploader_name} uploaded `{file_name}` at {ts} ({size_str}). "
                    "No generated summary was available; use file inspection if content is needed.\n\n"
                    "文件索引摘要兜底；需要内容时检查文件。"
                ),
            }

    if not isinstance(raw, dict):
        raw = {
            "headline": f"{uploader_name} uploaded {file_name}",
            "content": (
                f"{uploader_name} uploaded `{file_name}` at {ts} ({size_str}). "
                "The summary response was malformed; use file inspection if content is needed.\n\n"
                "文件索引响应格式异常；需要内容时检查文件。"
            ),
        }

    cid = f"c_{ulid.ULID()}"
    headline = sanitize_headline(str(raw.get("headline", file_name)))
    content = sanitize_summary(str(raw.get("content", "")))
    file_meta = json.dumps({
        "filename": file_name,
        "workspace_path": workspace_path,
        "archive_id": archive_id,
        "group_id": group_id,
        "upload_time": upload_time,
        "uploader_name": uploader_name,
        "file_size": file_size,
    }, ensure_ascii=False)

    async with pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO cold_nodes
                (id, archive_id, group_id, user_id, scope,
                 node_type, headline, content,
                 salience, source_refs, file_metadata)
            VALUES ($1, $2, $3, NULL, 'kb', 'file', $4, $5, $6, $7::jsonb, $8)
            """,
            cid, archive_id, group_id,
            headline, content, 0.5,
            json.dumps([]),
            file_meta,
        )

    log.info("group file indexed: node=%s file=%s", cid, file_name)
    return cid


def _format_messages_for_kb(batch: list[dict]) -> str:
    lines = []
    for m in batch:
        ts = _fmt_ts(m["created_at"])
        bot_marker = "[→bot]" if m.get("addressed_bot") else ""
        content = (m["content"] or "")[:600]
        lines.append(f"[id={m['id']} {ts} {m['user_name']}]{bot_marker} {content}")
    return "\n".join(lines)


def _fmt_ts(val):
    """Format created_at as mm-dd HH:MM, accepting datetime or ISO string (SQLite)."""
    from datetime import datetime as dt
    if isinstance(val, str):
        val = dt.fromisoformat(val.replace("Z", "+00:00"))
    return val.strftime("%m-%d %H:%M")


def _format_existing(existing: list[dict]) -> str:
    if not existing:
        return "（无）"
    lines = []
    for n in existing[:200]:
        lines.append(f"- [{n['id']}] ({n.get('type','')}) {n['headline']}")
    return "\n".join(lines)


def _resolve_id(x, tmp_to_real: dict[str, str]):
    if not isinstance(x, str):
        return None
    x = x.strip()
    if x in tmp_to_real:
        return tmp_to_real[x]
    if x.startswith("c_"):
        return x
    return None


# ── 兼容 stub 的旧接口名 ──
async def topk_kb(
    archive_id: str, group_id: str,
    query_embedding=None, k: int = 100,
    *, viewer_user_id: str | None = None,
) -> list[dict]:
    return await load_kb_index(
        archive_id, group_id,
        viewer_user_id=viewer_user_id, limit=k,
    )
