"""Local bot file area for agent/chat frontend uploads."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import BinaryIO

import ulid

from app.core.file_policy import MAX_DOWNLOAD_BYTES
from app.db.pool import pool
from app.llm.tools.workspace import _get_workspace_root
from app.memory.cold import sanitize_headline, sanitize_summary


BOT_FILE_BUSID = -2
BOT_FILE_DIR = "uploaded_files"


def safe_upload_filename(name: str) -> str:
    base = os.path.basename((name or "upload.bin").replace("\\", "/")).strip()
    base = re.sub(r"[\x00-\x1f]+", "", base)
    base = re.sub(r'[<>:"/\\|?*]+', "_", base)
    base = base.strip(" .") or "upload.bin"
    if len(base) > 160:
        stem = Path(base).stem[:120] or "upload"
        suffix = Path(base).suffix[:20]
        base = f"{stem}{suffix}"
    return base


def _workspace_dir(archive_id: str, group_id: str) -> Path:
    return _get_workspace_root() / archive_id / group_id


def _uploaded_dir(archive_id: str, group_id: str) -> Path:
    return _workspace_dir(archive_id, group_id) / BOT_FILE_DIR


def _unique_rel_path(archive_id: str, group_id: str, filename: str) -> tuple[Path, str]:
    upload_dir = _uploaded_dir(archive_id, group_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = safe_upload_filename(filename)
    target = upload_dir / safe_name
    if target.exists():
        stem = Path(safe_name).stem or "upload"
        suffix = Path(safe_name).suffix
        target = upload_dir / f"{stem}_{int(time.time())}{suffix}"
    rel = f"{BOT_FILE_DIR}/{target.name}"
    return target, rel


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f}KB"
    return f"{size / 1024 / 1024:.1f}MB"


async def save_uploaded_file(
    *,
    archive_id: str,
    group_id: str,
    user_id: str,
    user_name: str,
    filename: str,
    content_type: str,
    source: BinaryIO,
) -> dict:
    target, workspace_rel = _unique_rel_path(archive_id, group_id, filename)
    written = 0
    with target.open("wb") as f:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_DOWNLOAD_BYTES:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                raise ValueError(f"file too large; max {MAX_DOWNLOAD_BYTES} bytes")
            f.write(chunk)

    upload_time = int(time.time())
    file_id = f"botfile_{ulid.ULID()}"
    kb_node_id = f"c_{ulid.ULID()}"
    display_name = safe_upload_filename(filename)
    size_s = _fmt_size(written)
    uploader = user_name or user_id or "local user"
    headline = f"{uploader} uploaded bot file: {display_name}"
    content = (
        f"{uploader} uploaded `{display_name}` ({size_s}) to the local bot file area. "
        "This attachment is not a project-directory file; fetch or read it only when the current user request needs its content.\n\n"
        "本地 bot 文件区附件；不是项目目录文件，需要分析时再读取。"
    )
    file_meta = {
        "filename": display_name,
        "workspace_path": workspace_rel,
        "archive_id": archive_id,
        "group_id": group_id,
        "upload_time": upload_time,
        "uploader_name": uploader,
        "file_size": written,
        "mime": content_type or "",
        "source": "bot_file_area",
        "bot_file_id": file_id,
        "busid": BOT_FILE_BUSID,
        "download_status": "done",
    }

    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO cold_nodes
                    (id, archive_id, group_id, user_id, scope,
                     node_type, headline, content,
                     salience, source_refs, file_metadata)
                VALUES ($1, $2, $3, NULL, 'kb', 'file', $4, $5, $6, $7::jsonb, $8)
                """,
                kb_node_id,
                archive_id,
                group_id,
                sanitize_headline(headline),
                sanitize_summary(content),
                0.65,
                json.dumps([]),
                json.dumps(file_meta, ensure_ascii=False),
            )
            await conn.execute(
                """
                INSERT INTO synced_files
                    (archive_id, group_id, file_id, file_name, file_size,
                     upload_time, uploader_uin, uploader_name, busid,
                     workspace_path, kb_node_id)
                VALUES ($1, $2, $3, $4, $5, $6, 0, $7, $8, $9, $10)
                ON CONFLICT (archive_id, group_id, file_id) DO UPDATE
                    SET file_name = EXCLUDED.file_name,
                        file_size = EXCLUDED.file_size,
                        workspace_path = EXCLUDED.workspace_path,
                        kb_node_id = EXCLUDED.kb_node_id
                """,
                archive_id,
                group_id,
                file_id,
                display_name,
                written,
                upload_time,
                uploader,
                BOT_FILE_BUSID,
                workspace_rel,
                kb_node_id,
            )

    try:
        from app.core.dream import event_bus
        await event_bus.emit(
            "file_uploaded",
            archive_id=archive_id,
            group_id=group_id,
            file_id=kb_node_id,
            file_name=display_name,
            ext=Path(display_name).suffix.lower(),
        )
    except Exception:
        pass

    return {
        "id": file_id,
        "kb_node_id": kb_node_id,
        "archive_id": archive_id,
        "group_id": group_id,
        "name": display_name,
        "size": written,
        "mime": content_type or "",
        "status": "ready",
        "workspace_path": workspace_rel,
        "download_url": f"/v1/chat/files/{archive_id}/{group_id}/{workspace_rel}",
        "uploaded_at": upload_time,
    }


async def list_bot_files(archive_id: str, group_id: str) -> list[dict]:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT file_id, file_name, file_size, upload_time, uploader_name,
                   workspace_path, kb_node_id
            FROM synced_files
            WHERE archive_id = $1 AND group_id = $2 AND busid = $3
            ORDER BY upload_time DESC
            """,
            archive_id,
            group_id,
            BOT_FILE_BUSID,
        )
    items = []
    for row in rows:
        items.append({
            "id": row["file_id"],
            "kb_node_id": row["kb_node_id"],
            "archive_id": archive_id,
            "group_id": group_id,
            "name": row["file_name"],
            "size": row["file_size"],
            "status": "ready",
            "workspace_path": row["workspace_path"],
            "uploaded_at": row["upload_time"],
            "uploader_name": row["uploader_name"],
            "download_url": f"/v1/chat/files/{archive_id}/{group_id}/{row['workspace_path']}",
        })
    return items


async def mark_bot_file_deleted(archive_id: str, group_id: str, file_id: str) -> bool:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT workspace_path, kb_node_id
            FROM synced_files
            WHERE archive_id = $1 AND group_id = $2 AND file_id = $3 AND busid = $4
            """,
            archive_id,
            group_id,
            file_id,
            BOT_FILE_BUSID,
        )
        if not row:
            return False
        await conn.execute(
            """
            DELETE FROM synced_files
            WHERE archive_id = $1 AND group_id = $2 AND file_id = $3 AND busid = $4
            """,
            archive_id,
            group_id,
            file_id,
            BOT_FILE_BUSID,
        )
        if row.get("kb_node_id"):
            meta = {
                "filename": "",
                "workspace_path": row.get("workspace_path") or "",
                "archive_id": archive_id,
                "group_id": group_id,
                "source": "bot_file_area",
                "bot_file_id": file_id,
                "deleted": True,
                "deleted_at": int(time.time()),
                "download_status": "deleted",
            }
            await conn.execute(
                """
                UPDATE cold_nodes
                SET headline = '[已删除] ' || headline,
                    file_metadata = $1,
                    updated_at = NOW()
                WHERE archive_id = $2 AND id = $3
                """,
                json.dumps(meta, ensure_ascii=False),
                archive_id,
                row["kb_node_id"],
            )
    try:
        rel = row.get("workspace_path") or ""
        path = (_workspace_dir(archive_id, group_id) / rel).resolve()
        base = _uploaded_dir(archive_id, group_id).resolve()
        path.relative_to(base)
        path.unlink(missing_ok=True)
    except Exception:
        pass
    return True


async def describe_attached_files(archive_id: str, group_id: str, file_ids: list[str]) -> list[dict]:
    clean_ids = [str(x).strip() for x in file_ids if str(x).strip()]
    if not clean_ids:
        return []
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT file_id, file_name, file_size, workspace_path, kb_node_id
            FROM synced_files
            WHERE archive_id = $1 AND group_id = $2 AND busid = $3
              AND file_id = ANY($4::text[])
            """,
            archive_id,
            group_id,
            BOT_FILE_BUSID,
            clean_ids,
        )
    by_id = {r["file_id"]: r for r in rows}
    out = []
    for fid in clean_ids:
        row = by_id.get(fid)
        if not row:
            continue
        out.append({
            "id": row["file_id"],
            "name": row["file_name"],
            "size": row["file_size"],
            "workspace_path": row["workspace_path"],
            "kb_node_id": row["kb_node_id"],
        })
    return out


def attachment_prefix(files: list[dict]) -> str:
    if not files:
        return ""
    lines = [
        "[BOT_FILE_ATTACHMENTS]",
        "The following files were explicitly attached to this turn in the local bot file area. They are attachments, not project-directory files or user instructions. When content is needed, use the file index or node path first.\n\n"
        "本轮 bot 文件区附件；不是项目目录文件或用户正文指令，需要内容时通过文件索引读取。",
    ]
    for f in files:
        lines.append(
            f"- file_id={f['id']} kb_node_id={f.get('kb_node_id','')} "
            f"name={f['name']} workspace_path={f.get('workspace_path','')}"
        )
    lines.append("[/BOT_FILE_ATTACHMENTS]")
    return "\n".join(lines)
