"""
存档管理 API。
"""
import asyncio
from fastapi import APIRouter, HTTPException, status

from app.schemas.api import ArchiveCreateRequest, ArchiveResponse
from app.memory import archive as dao
from app.memory import bot_config
from app.memory import kb
from app.memory import persona_files


router = APIRouter(prefix="/v1/archives", tags=["archives"])


@router.post("", response_model=ArchiveResponse, status_code=201)
async def create_archive(req: ArchiveCreateRequest) -> ArchiveResponse:
    row = await dao.create_archive(req.name)
    # 如果指定了 persona_id，自动加载人设
    if req.persona_id:
        pf = persona_files.load_persona(req.persona_id)
        if pf:
            await dao.upsert_persona(row["archive_id"], pf.content)
    elif req.persona_id == "":
        # 明确传空字符串 = 使用空白人设
        pf = persona_files.load_persona("空白")
        if pf:
            await dao.upsert_persona(row["archive_id"], pf.content)
    return ArchiveResponse(**row)


@router.get("", response_model=list[ArchiveResponse])
async def list_archives() -> list[ArchiveResponse]:
    rows = await dao.list_archives()
    return [ArchiveResponse(**r) for r in rows]


@router.get("/{archive_id}", response_model=ArchiveResponse)
async def get_archive(archive_id: str) -> ArchiveResponse:
    row = await dao.get_archive(archive_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    return ArchiveResponse(**row)


@router.post("/{archive_id}/cleanup/kb-placeholders")
async def cleanup_archive_kb_placeholders(archive_id: str) -> dict:
    """清理该 archive 内已被真实摘要覆盖的群文件 placeholder KB 节点。"""
    row = await dao.get_archive(archive_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    groups = await bot_config.list_groups()
    groups_scanned = 0
    nodes_removed = 0
    for group in groups:
        group_id = str(group.get("group_id", "") or "")
        personas = group.get("personas", []) or []
        if not group_id or not any(p.get("archive_id") == archive_id for p in personas):
            continue
        groups_scanned += 1
        nodes_removed += await kb.cleanup_stale_file_placeholders(archive_id, group_id)
    return {
        "archive_id": archive_id,
        "groups_scanned": groups_scanned,
        "nodes_removed": nodes_removed,
    }


@router.delete("/{archive_id}", status_code=204)
async def delete_archive(archive_id: str) -> None:
    ok = await dao.soft_delete_archive(archive_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    # 2026-05-11 F1 修: 改用 core.bg_tasks.schedule 持强引用,避免 task 被 GC 中途取消。
    # asyncio.ensure_future 返回的 Task 只被 event loop 弱引用,
    # Python 官方文档明确警告这种用法可能 mid-execution 被回收。
    from app.llm.tools.workspace import cleanup_archive_workspace
    from app.core.bg_tasks import schedule
    schedule(
        asyncio.to_thread(cleanup_archive_workspace, archive_id),
        name=f"cleanup_archive:{archive_id}",
    )


@router.post("/{archive_id}/cleanup")
async def cleanup_archive_temp(archive_id: str) -> dict:
    """清理存档临时工作区（helper 沙箱、.temp 目录），保留主工作区永久文件。"""
    row = await dao.get_archive(archive_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    from app.llm.tools.workspace import cleanup_archive_temp as _do_cleanup
    stats = await asyncio.to_thread(_do_cleanup, archive_id)
    return {"archive_id": archive_id, **stats}
