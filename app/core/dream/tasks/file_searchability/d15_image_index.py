"""
D15: 图片 OCR + vision 索引 (文件可寻性 v6 核心)

问题: 当前图片 cold_nodes.content 为空, 主线程只能按文件名 + 时间戳找图。
目标: dream 后台对图片做 OCR + vision 描述, UPDATE cold_nodes:
       - content: OCR 文字 + visual description + 关键词
       - file_metadata: dream_processed=True, topics, keywords, ocr_word_count
       → 主线程 search_files("含 calculate 的截图") 直接命中

工作流 (D 类长任务, 但每个图片是独立单元):
1. 找未 dream_processed 的图片节点 (file_uploaded 事件触发查)
2. 对每个图片:
   Step 1: inspect (尺寸/格式)
   Step 2: OCR (ocr_bridge.ocr_file, 同步用 to_thread)
   Step 3: 如有文字 → LLM 提关键词 + 描述
   Step 4: UPDATE cold_nodes
3. 完成后 mark file_metadata.dream_processed = True

阈值: 1 (每张图都做)
LLM: main+max (后台用质量, lite_first=False)
打断: 单图片粒度, 完成一张算一张
"""
from __future__ import annotations

from app.core.dream.prompt_catalog import (
    D15_IMAGE_INDEX_SYSTEM,
)
_LLM_PROMPT_SYSTEM = D15_IMAGE_INDEX_SYSTEM


import asyncio
import json
import os
import time
from typing import Any

from app.config import settings
from app.core.dream.dream_log import dream_log
from app.core.dream.event_bus import event_bus
from app.core.dream.registry import register_dream_task
from app.core.dream.task_base import InfoDrivenTask
from app.core.dream.tasks.file_searchability.file_meta import (
    emit_file_indexed,
    is_active_file_metadata,
)


# 触发: 1 个新图片就处理 (即时可寻)
D15_THRESHOLD = 1
# 单次最多处理几张 (防一次跑太久)
D15_MAX_IMAGES_PER_RUN = 5
_D15_RAW_SCAN_LIMIT = 5000
_D15_MAX_CANDIDATES_PER_RUN = 200
# 图片扩展名
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"}




def _validate_d15_output(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    desc = raw.get("description", "")
    if not isinstance(desc, str):
        return False
    if len(desc) > 300:
        return False
    if raw.get("type", "") not in (
        "screenshot", "photo", "chart", "document", "code",
        "ui", "diagram", "other", "image", "",
    ):
        return False
    kws = raw.get("keywords", [])
    if not isinstance(kws, list) or len(kws) > 20:
        return False
    if not all(isinstance(k, str) and len(k) < 50 for k in kws):
        return False
    topics = raw.get("topics", [])
    if not isinstance(topics, list) or len(topics) > 5:
        return False
    return True


async def _get_unprocessed_images(limit: int = 10) -> list[dict]:
    """找未做过 dream OCR 的图片节点."""
    from app.db.pool import pool
    
    async with pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, archive_id, group_id, headline, content, file_metadata
            FROM cold_nodes
            WHERE scope = 'kb'
              AND node_type = 'file'
              AND (file_metadata->>'dream_processed' IS NULL
                   OR file_metadata->>'dream_processed' = 'false')
              AND file_metadata->>'workspace_path' IS NOT NULL
              AND file_metadata->>'workspace_path' != ''
              AND (file_metadata->>'deleted' IS NULL
                   OR file_metadata->>'deleted' != 'true')
              AND (file_metadata->>'download_status' IS NULL
                   OR file_metadata->>'download_status' = ''
                   OR file_metadata->>'download_status' = 'done')
            ORDER BY created_at DESC
            LIMIT $1
        """, _D15_RAW_SCAN_LIMIT)
    
    image_nodes = []
    for r in rows:
        meta = r["file_metadata"]
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                continue
        if not isinstance(meta, dict):
            continue
        if not is_active_file_metadata(meta):
            continue
        
        filename = meta.get("filename", "") or meta.get("file_name", "")
        ext = os.path.splitext(filename)[1].lower()
        if ext not in _IMAGE_EXTS:
            continue
        
        image_nodes.append({
            "id": r["id"],
            "archive_id": r["archive_id"],
            "group_id": r["group_id"],
            "headline": r["headline"],
            "content": r["content"] or "",
            "file_metadata": meta,
            "filename": filename,
            "ext": ext,
        })
        
        if len(image_nodes) >= limit:
            break
    
    return image_nodes


def _get_image_full_path(file_meta: dict) -> str | None:
    """从 file_metadata 拼绝对路径."""
    ws_path = file_meta.get("workspace_path") or ""
    if not ws_path:
        return None
    archive_id = file_meta.get("archive_id", "")
    group_id = file_meta.get("group_id", "")
    
    # 用 dream cache 的统一 getter (main.py 注入的指向真实 workspace_root)
    from app.core.dream.cache import _get_workspace_root
    ws_root = _get_workspace_root()
    
    if not ws_root:
        return None
    
    # workspace_path 是相对路径, 拼绝对
    full = os.path.join(ws_root, archive_id, group_id, ws_path)
    return full if os.path.isfile(full) else None


async def _run_ocr_safe(path: str) -> tuple[str, float]:
    """跑统一调度 OCR，走共享 GPU 锁 + 共享缓存 + 分层升级。"""
    try:
        from app.llm.tools.ocr_bridge import ocr_file_scheduled
    except Exception:
        return ("", 0.0)

    try:
        result = await ocr_file_scheduled(
            path,
            tier="fast",
            allow_upgrade=True,
            max_tier="accurate",
            timeout=60,
        )
        if result.ok:
            return (result.text or "", result.score or 0.0)
        return ("", 0.0)
    except Exception as e:
        dream_log.warn("dream.task.d15_image_index.ocr_failed", f"path={path} err={e!r}"[:200])
        return ("", 0.0)


async def _llm_extract_image_info(ocr_text: str, filename: str) -> dict | None:
    """用 LLM 从 OCR 文字提取检索元数据."""
    from app.llm import client as llm
    
    # OCR 文字截到 4000 字 (够提关键词)
    ocr_excerpt = (ocr_text or "")[:4000]
    
    user_text = (
        f"## Image Metadata\nfilename: {filename}\n\n"
        f"## OCR Text ({len(ocr_text)} chars)\n"
        f"```\n{ocr_excerpt}\n```\n\n"
        "Return the indexing JSON.\n\n输出索引 JSON。"
    )
    
    messages = [
        {"role": "system", "content": _LLM_PROMPT_SYSTEM},
        {"role": "user", "content": user_text},
    ]
    
    raw = await llm.chat_json_with_upgrade(
        messages,
        validate=_validate_d15_output,
        label="dream_d15_index",
        lite_first=False,  # 后台用 main+max
    )
    return raw


async def _update_image_node(node: dict, ocr_text: str, ocr_score: float, info: dict) -> bool:
    """UPDATE cold_nodes - 写入增强后的 content + file_metadata."""
    from app.db.pool import pool
    from app.memory.kb import sanitize_summary
    
    # 拼装 content (OCR + visual)
    content_parts = []
    if info.get("description"):
        content_parts.append(f"图片描述: {info['description']}")
    if info.get("type") and info["type"] != "image":
        content_parts.append(f"类型: {info['type']}")
    if ocr_text and len(ocr_text.strip()) > 0:
        # OCR 文字 (截到 1000 字 进 content)
        content_parts.append(f"OCR 文字: {ocr_text[:1000]}")
    if info.get("keywords"):
        content_parts.append(f"关键词: {', '.join(info['keywords'])}")
    
    if not content_parts:
        # 啥也没提到 → 至少标记已处理 (避免反复尝试)
        new_content = node.get("content") or "(图片无可提取文字)"
    else:
        new_content = sanitize_summary("\n".join(content_parts))
    
    # 更新 file_metadata
    new_meta = dict(node["file_metadata"])
    new_meta.update({
        "dream_processed": True,
        "dream_processed_at": time.time(),
        "dream_task": "d15_image_index",
        "ocr_word_count": len(ocr_text.split()) if ocr_text else 0,
        "ocr_score": ocr_score,
        "has_text": bool(ocr_text and ocr_text.strip()),
        "image_type_detected": info.get("type", "image"),
        "topics": info.get("topics", []),
        "keywords": info.get("keywords", []),
    })
    
    try:
        async with pool().acquire() as conn:
            await conn.execute("""
                UPDATE cold_nodes
                SET content = $1, file_metadata = $2, updated_at = NOW()
                WHERE id = $3 AND archive_id = $4
            """, new_content, json.dumps(new_meta, ensure_ascii=False),
                 node["id"], node["archive_id"])
        return True
    except Exception as e:
        dream_log.error(
            "dream.task.d15_image_index.update_failed",
            f"id={node['id']} err={e!r}"[:200],
        )
        return False


@register_dream_task
class D15ImageIndex(InfoDrivenTask):
    """D15: 图片 OCR + vision 索引 (文件可寻性核心)."""
    
    name = "d15_image_index"
    threshold = D15_THRESHOLD
    uses_llm = True
    startup_sweep = True
    
    async def info_fn(self) -> float:
        """信息量 = 文件上传事件累计数 (含非图片, 我们筛选)."""
        try:
            return float(len(await _get_unprocessed_images(limit=1000)))
        except Exception:
            return float(event_bus.total_count("file_uploaded"))

    async def should_run(self) -> bool:
        import time as _t
        if self.suspended_until > _t.time():
            return False
        try:
            return await self.info_fn() >= self.threshold
        except Exception:
            return False
    
    async def _do_work(self) -> None:
        # 找候选
        candidates = await _get_unprocessed_images(limit=_D15_MAX_CANDIDATES_PER_RUN)
        if not candidates:
            return
        
        dream_log.log(
            "dream.task.d15_image_index.found",
            f"unprocessed_images={len(candidates)}",
        )
        
        success = 0
        skipped = 0
        for node in candidates:
            # 单图片处理 - 完整链 (粒度小, 不需要 checkpoint)
            file_path = _get_image_full_path(node["file_metadata"])
            if not file_path:
                # 文件路径无效 → 标记 skipped
                skipped += 1
                await self._mark_skipped(node, reason="path_invalid")
                continue
            if success >= D15_MAX_IMAGES_PER_RUN:
                break
            
            # Step 1: OCR
            try:
                ocr_text, ocr_score = await _run_ocr_safe(file_path)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                dream_log.warn(
                    "dream.task.d15_image_index.ocr_exception",
                    f"path={file_path} err={e!r}"[:200],
                )
                skipped += 1
                continue
            
            # Step 2: LLM 提取元数据 (即使 OCR 空也跑, LLM 会给基础分类)
            try:
                info = await _llm_extract_image_info(ocr_text, node["filename"])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                dream_log.warn(
                    "dream.task.d15_image_index.llm_exception",
                    f"id={node['id']} err={e!r}"[:200],
                )
                info = None
            
            if info is None:
                # LLM 失败 → 用 fallback (仅 OCR 文字, 无元数据)
                info = {
                    "description": "",
                    "type": "image",
                    "keywords": [],
                    "topics": [],
                }
            
            # Step 3: UPDATE
            ok = await _update_image_node(node, ocr_text, ocr_score, info)
            if ok:
                success += 1
                await emit_file_indexed(node, self.name)
                dream_log.log(
                    "dream.task.d15_image_index.indexed",
                    f"id={node['id']} ocr_chars={len(ocr_text)} "
                    f"keywords={len(info.get('keywords', []))}",
                )
        
        if success or skipped:
            dream_log.log(
                "dream.task.d15_image_index.cycle_done",
                f"indexed={success} skipped={skipped}",
            )
    
    async def _mark_skipped(self, node: dict, reason: str) -> None:
        """标记节点 skipped (但 dream_processed=True 避免反复重试)."""
        from app.db.pool import pool
        new_meta = dict(node["file_metadata"])
        new_meta.update({
            "dream_processed": True,
            "dream_processed_at": time.time(),
            "dream_task": "d15_image_index",
            "dream_skipped": True,
            "dream_skip_reason": reason,
        })
        try:
            async with pool().acquire() as conn:
                await conn.execute("""
                    UPDATE cold_nodes
                    SET file_metadata = $1, updated_at = NOW()
                    WHERE id = $2 AND archive_id = $3
                """, json.dumps(new_meta, ensure_ascii=False),
                     node["id"], node["archive_id"])
        except Exception as e:
            dream_log.warn(
                "dream.task.d15_image_index.mark_skipped_failed",
                repr(e)[:200],
            )
