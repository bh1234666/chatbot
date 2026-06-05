"""
D18: 音视频元数据 + 转写 (简化版)

注: 当前代码库没有现成的 transcribe 工具 (whisper 等). 这里实现基础版本:
- 提取元数据 (时长, 编解码, 容器格式) 用 ffprobe (如可用)
- 文件名 + 元数据生成基础 content
- 标记 dream_processed=true (避免 D15-D17 反复尝试)

未来扩展: 集成 whisper 后启用转写 (按段 checkpoint, 仿 D16 模式)
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from typing import Any

from app.core.dream.dream_log import dream_log
from app.core.dream.event_bus import event_bus
from app.core.dream.registry import register_dream_task
from app.core.dream.task_base import InfoDrivenTask
from app.core.dream.tasks.file_searchability.file_meta import (
    emit_file_indexed,
    is_active_file_metadata,
)


D18_THRESHOLD = 1
D18_MAX_PER_RUN = 5
_D18_RAW_SCAN_LIMIT = 5000
_D18_MAX_CANDIDATES_PER_RUN = 200
_AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus"}
_VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".wmv"}
_MEDIA_EXTS = _AUDIO_EXTS | _VIDEO_EXTS


async def _get_unprocessed_media(limit: int) -> list[dict]:
    from app.db.pool import pool
    
    async with pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, archive_id, group_id, headline, content, file_metadata
            FROM cold_nodes
            WHERE scope = 'kb' AND node_type = 'file'
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
        """, _D18_RAW_SCAN_LIMIT)
    
    out = []
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
        if ext not in _MEDIA_EXTS:
            continue
        out.append({
            "id": r["id"], "archive_id": r["archive_id"], "group_id": r["group_id"],
            "headline": r["headline"], "content": r["content"] or "",
            "file_metadata": meta, "filename": filename, "ext": ext,
            "is_video": ext in _VIDEO_EXTS,
        })
        if len(out) >= limit:
            break
    return out


def _get_media_path(file_meta: dict) -> str | None:
    ws_path = file_meta.get("workspace_path") or ""
    if not ws_path:
        return None
    from app.core.dream.cache import _get_workspace_root
    ws_root = _get_workspace_root()
    archive_id = file_meta.get("archive_id", "")
    group_id = file_meta.get("group_id", "")
    if not ws_root:
        return None
    full = os.path.join(ws_root, archive_id, group_id, ws_path)
    return full if os.path.isfile(full) else None


def _ffprobe_metadata(path: str) -> dict | None:
    """用 ffprobe 拿媒体元数据 (如可用)."""
    if not shutil.which("ffprobe"):
        return None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except Exception:
        return None


def _format_metadata(probe: dict, filename: str, is_video: bool) -> str:
    """把 ffprobe 输出格式化为可读 content."""
    parts = []
    
    fmt = probe.get("format", {})
    duration = fmt.get("duration")
    if duration:
        try:
            duration_sec = float(duration)
            mins = int(duration_sec // 60)
            secs = int(duration_sec % 60)
            parts.append(f"时长: {mins}:{secs:02d}")
        except (ValueError, TypeError):
            pass
    
    bit_rate = fmt.get("bit_rate")
    if bit_rate:
        try:
            br_kbps = int(bit_rate) // 1000
            parts.append(f"码率: {br_kbps} kbps")
        except (ValueError, TypeError):
            pass
    
    size = fmt.get("size")
    if size:
        try:
            size_mb = int(size) / (1024 * 1024)
            parts.append(f"大小: {size_mb:.1f} MB")
        except (ValueError, TypeError):
            pass
    
    streams = probe.get("streams", [])
    for s in streams:
        codec_type = s.get("codec_type", "")
        codec_name = s.get("codec_name", "")
        if codec_type == "audio":
            sample_rate = s.get("sample_rate", "")
            channels = s.get("channels", "")
            parts.append(f"音频: {codec_name} {sample_rate}Hz {channels}ch")
        elif codec_type == "video":
            width = s.get("width", 0)
            height = s.get("height", 0)
            fps_str = s.get("r_frame_rate", "")
            parts.append(f"视频: {codec_name} {width}x{height} @ {fps_str}fps")
    
    if not parts:
        return f"音视频文件: {filename}"
    
    type_label = "视频文件" if is_video else "音频文件"
    return f"{type_label}: {filename}\n" + "\n".join(parts)


@register_dream_task
class D18MediaMetadata(InfoDrivenTask):
    """D18: 音视频元数据提取 (简化, 转写待 whisper 集成)."""
    
    name = "d18_media_metadata"
    threshold = D18_THRESHOLD
    uses_llm = False
    startup_sweep = True
    
    async def info_fn(self) -> float:
        try:
            return float(len(await _get_unprocessed_media(limit=1000)))
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
        from app.db.pool import pool
        from app.memory.kb import sanitize_summary
        
        candidates = await _get_unprocessed_media(_D18_MAX_CANDIDATES_PER_RUN)
        if not candidates:
            return
        
        success = 0
        skipped = 0
        attempted = 0
        for node in candidates:
            path = _get_media_path(node["file_metadata"])
            if not path:
                skipped += 1
                await self._mark_skipped(node, "path_invalid")
                continue
            if attempted >= D18_MAX_PER_RUN:
                break
            attempted += 1
            
            # 跑 ffprobe (如不可用, 仅记录基础信息)
            probe = await asyncio.to_thread(_ffprobe_metadata, path)
            
            if probe:
                content = _format_metadata(probe, node["filename"], node["is_video"])
                duration_sec = None
                try:
                    duration_sec = float(probe.get("format", {}).get("duration", 0))
                except Exception:
                    duration_sec = None
            else:
                content = f"{'视频' if node['is_video'] else '音频'}文件: {node['filename']}"
                duration_sec = None
            
            meta = dict(node["file_metadata"])
            meta.update({
                "dream_processed": True,
                "dream_processed_at": time.time(),
                "dream_task": "d18_media_metadata",
                "media_type": "video" if node["is_video"] else "audio",
                "duration_sec": duration_sec,
                "has_transcribe": False,  # 标记待未来集成
                "transcribe_pending": True,  # 让未来 whisper 集成知道该补
            })
            
            try:
                async with pool().acquire() as conn:
                    await conn.execute("""
                        UPDATE cold_nodes
                        SET content = $1, file_metadata = $2, updated_at = NOW()
                        WHERE id = $3
                    """, sanitize_summary(content),
                         json.dumps(meta, ensure_ascii=False),
                         node["id"])
                await emit_file_indexed(node, self.name)
                success += 1
            except Exception as e:
                dream_log.error(
                    "dream.task.d18_media_metadata.update_failed",
                    f"id={node['id']} err={e!r}"[:200],
                )
        
        if success or skipped:
            dream_log.log(
                "dream.task.d18_media_metadata.cycle_done",
                f"indexed={success} skipped={skipped}",
            )

    async def _mark_skipped(self, node: dict, reason: str) -> None:
        from app.db.pool import pool
        meta = dict(node["file_metadata"])
        meta.update({
            "dream_processed": True,
            "dream_processed_at": time.time(),
            "dream_skipped": True,
            "dream_skip_reason": reason,
        })
        try:
            async with pool().acquire() as conn:
                await conn.execute(
                    "UPDATE cold_nodes SET file_metadata = $1, updated_at = NOW() WHERE id = $2",
                    json.dumps(meta, ensure_ascii=False), node["id"],
                )
        except Exception as e:
            dream_log.warn(
                "dream.task.d18_media_metadata.mark_skipped_failed",
                repr(e)[:200],
            )
