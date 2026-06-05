"""
群文件同步 API。
"""
from fastapi import APIRouter

from app.memory.group_files import sync_group_files

router = APIRouter(prefix="/v1/archives", tags=["group-files"])


@router.post("/{archive_id}/groups/{group_id}/group-files/sync")
async def sync_group_files_endpoint(archive_id: str, group_id: str):
    """触发群文件同步。返回本次新同步的文件数。"""
    count = await sync_group_files(archive_id, group_id)
    return {"ok": True, "synced": count}
