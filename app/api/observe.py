"""
群消息旁观端点。

用途：
  把群里所有消息（无论是否与机器人交互）推给本服务，
  作为知识库（cold_nodes scope='kb'）的源数据。

注意：
  - 此端点不触发对话流程；机器人不会回复
  - 与机器人交互的消息（addressed_bot=true）调用方应同时调用 /v1/chat/stream，
    本端点仅用于把它们也归档进 group_messages
  - addressed_bot 标志只是审计字段，不影响处理路径
"""
from fastapi import APIRouter, HTTPException, status

from app.core.bg_tasks import schedule
from app.schemas.api import ObserveRequest, ObserveResponse
from app.memory import archive as archive_dao
from app.memory import group_messages as gm
from app.memory import kb as kb_mem


router = APIRouter(
    prefix="/v1/archives/{archive_id}/groups/{group_id}",
    tags=["observe"],
)


@router.post("/observe", response_model=ObserveResponse, status_code=202)
async def observe(
    archive_id: str,
    group_id: str,
    req: ObserveRequest,
) -> ObserveResponse:
    if archive_id != req.archive_id or group_id != req.group_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "archive_id/group_id mismatch between path and body",
        )
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")

    msg_id = await gm.append_message(
        archive_id=archive_id,
        group_id=group_id,
        user_id=req.user_id,
        user_name=req.user_name,
        content=req.content,
        addressed_bot=req.addressed_bot,
    )
    schedule(
        kb_mem.maybe_compress_kb(archive_id, group_id),
        name=f"observe.kb_compress:{archive_id}:{group_id}",
    )
    return ObserveResponse(message_id=msg_id)
