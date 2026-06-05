"""
记忆运营 API：查询、展开、删除。
M1 仅 hot；M2 增加 warm 索引读取与 expand。
"""
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.memory import archive as archive_dao
from app.memory import warm as warm_mem
from app.memory import hot as hot_mem
from app.memory import cold as cold_mem
from app.memory import kb as kb_mem


router = APIRouter(
    prefix="/v1/archives/{archive_id}/groups/{group_id}",
    tags=["memory"],
)


class WarmExpandRequest(BaseModel):
    ids: list[str]


class ColdExpandRequest(BaseModel):
    ids: list[str]
    depth: int = 1


@router.get("/users/{user_id}/memory/warm")
async def get_user_warm(archive_id: str, group_id: str, user_id: str):
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    return {
        "items": await warm_mem.load_user_warm_index(archive_id, group_id, user_id),
    }


@router.get("/memory/warm")
async def get_group_warm(archive_id: str, group_id: str):
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    return {
        "items": await warm_mem.load_group_warm_index(archive_id, group_id),
    }


@router.post("/memory/warm/expand")
async def expand_warm(archive_id: str, group_id: str, req: WarmExpandRequest):
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    return {
        "items": await warm_mem.expand_warm(archive_id, req.ids),
    }


@router.delete("/memory/warm/{warm_id}", status_code=204)
async def delete_warm(archive_id: str, group_id: str, warm_id: str):
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    n = await warm_mem.delete_warm(archive_id, group_id, [warm_id])
    if n == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "warm memory not found")


@router.get("/users/{user_id}/memory/hot")
async def get_user_hot(archive_id: str, group_id: str, user_id: str):
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    items = await hot_mem.load_user_hot(archive_id, group_id, user_id)
    return {
        "items": [
            {
                "role": m.role, "content": m.content,
                "turn_id": m.turn_id, "created_at": m.created_at.isoformat(),
            }
            for m in items
        ]
    }


@router.get("/memory/hot")
async def get_group_hot(archive_id: str, group_id: str):
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    items = await hot_mem.load_group_hot(archive_id, group_id)
    return {
        "items": [
            {
                "actor_user_id": e.actor_user_id, "actor_name": e.actor_name,
                "narration": e.narration, "created_at": e.created_at.isoformat(),
            }
            for e in items
        ]
    }


# ── M3: 冷记忆与知识库 ─────────────────────────────────────
@router.get("/users/{user_id}/memory/cold")
async def get_user_cold(archive_id: str, group_id: str, user_id: str):
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    return {
        "items": await cold_mem.load_cold_user_index(archive_id, group_id, user_id),
    }


@router.get("/memory/cold")
async def get_group_cold(archive_id: str, group_id: str):
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    return {
        "items": await cold_mem.load_cold_group_index(archive_id, group_id),
    }


@router.post("/memory/cold/expand")
async def expand_cold(archive_id: str, group_id: str, req: ColdExpandRequest):
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    return {
        "items": await cold_mem.expand_cold(archive_id, req.ids, req.depth),
    }


@router.delete("/memory/cold/{cold_id}", status_code=204)
async def delete_cold(archive_id: str, group_id: str, cold_id: str):
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    n = await cold_mem.delete_cold(archive_id, group_id, [cold_id])
    if n == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cold node not found")


@router.get("/memory/kb")
async def get_kb(archive_id: str, group_id: str):
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    return {
        "items": await kb_mem.load_kb_index(archive_id, group_id),
    }


@router.post("/memory/kb/expand")
async def expand_kb(archive_id: str, group_id: str, req: ColdExpandRequest):
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    return {
        "items": await kb_mem.expand_kb(archive_id, req.ids, req.depth),
    }


# ── 软遗忘（不删除，仅标记为不主动提及） ──────────────────────
class AvoidMentionRequest(BaseModel):
    topics: list[str]
    reason: str = ""


@router.post("/users/{user_id}/avoid-mention")
async def request_avoid_mention(
    archive_id: str, group_id: str, user_id: str,
    req: AvoidMentionRequest,
):
    """
    根据自然语言话题列表标记相关冷/KB 节点为'不主动提及'（按当前 user 视角）。
    不删除任何节点；记忆永远保留，仅影响机器人主动行为。
    """
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    if not req.topics:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "topics required")
    result = await cold_mem.apply_avoid_mention(
        archive_id=archive_id, group_id=group_id, user_id=user_id,
        topics=req.topics, reason=req.reason,
    )
    return result


@router.get("/users/{user_id}/avoid-mention")
async def list_avoid_mention(archive_id: str, group_id: str, user_id: str):
    """列出该用户视角下被标记的所有节点（含 user/group/kb 三类）。"""
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    items = await cold_mem.list_avoided_for_user(archive_id, group_id, user_id)
    return {
        "items": [
            {
                "id": r["id"], "scope": r["scope"], "type": r["node_type"],
                "headline": r["headline"], "reason": r["reason"] or "",
                "created_at": r["created_at"].isoformat(),
            }
            for r in items
        ]
    }


@router.delete("/users/{user_id}/avoid-mention/{node_id}", status_code=204)
async def cancel_avoid_mention(
    archive_id: str, group_id: str, user_id: str, node_id: str,
):
    """取消单个节点的 avoid 标记（运营恢复用）。"""
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    ok = await cold_mem.unmark_avoid_for_user(
        archive_id, group_id, user_id, node_id,
    )
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "avoid mark not found")
