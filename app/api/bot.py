"""
Bot management API.
Control which QQ groups the bot participates in and switch personas.
Default: bot does NOT participate in any group until explicitly joined.
"""
from fastapi import APIRouter, HTTPException, status
import asyncio

from app.schemas.api import (
    BotJoinRequest,
    BotPersonaAddRequest,
    BotGroupResponse,
    BotGroupListResponse,
    CurrentArchiveRequest,
    AdminGroupRequest,
)
from app.memory import bot_config as dao
from app.memory import archive as archive_dao

router = APIRouter(prefix="/v1/bot", tags=["bot"])


async def _require_archive(archive_id: str) -> None:
    if not await archive_dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"archive not found: {archive_id}")


# ── Group management ──────────────────────────────────────────

@router.post("/groups/{group_id}/join", response_model=BotGroupResponse)
async def join_group(group_id: str, req: BotJoinRequest):
    await _require_archive(req.archive_id)
    cfg = await dao.join_group(group_id, req.archive_id, req.group_name, req.persona_label)
    cfg["personas"] = await dao.list_personas(group_id)
    return BotGroupResponse(**cfg)


@router.post("/groups/{group_id}/leave")
async def leave_group(group_id: str):
    await dao.leave_group(group_id)
    return {"status": "ok", "group_id": group_id}


@router.delete("/groups/{group_id}")
async def delete_group(group_id: str):
    """彻底删除群: 删除群下所有人设关联 + 群配置 + 软删关联存档 + 清理工作区。"""
    from app.llm.tools.workspace import cleanup_archive_workspace
    from app.core.bg_tasks import schedule

    # 1. 先取所有人设(在删群前取,删群后关联就没了)
    personas = await dao.list_personas(group_id)
    archive_ids = [p["archive_id"] for p in personas]

    # 2. 删群(群配置 + 群内人设关联)
    await dao.delete_group(group_id)

    # 3. 软删所有关联存档 + 清理工作区
    # 2026-05-11 F1 修: schedule 持强引用,避免 cleanup task 被 GC 取消导致
    # workspace 残留垃圾(磁盘累积、未来同 archive_id 复用会冲突)。
    deleted_archives = []
    for aid in archive_ids:
        ok = await archive_dao.soft_delete_archive(aid)
        if ok:
            deleted_archives.append(aid)
            schedule(
                asyncio.to_thread(cleanup_archive_workspace, aid),
                name=f"cleanup_archive:{aid}",
            )

    return {
        "status": "ok",
        "group_id": group_id,
        "deleted_archives": deleted_archives,
        "note": f"已删除群 {group_id} 及 {len(deleted_archives)} 个关联存档",
    }


@router.get("/groups", response_model=BotGroupListResponse)
async def list_groups():
    items = await dao.list_groups()
    return BotGroupListResponse(items=[BotGroupResponse(**g) for g in items])


@router.get("/groups/{group_id}", response_model=BotGroupResponse)
async def get_group(group_id: str):
    cfg = await dao.get_group_config(group_id)
    if not cfg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "group not configured")
    cfg["participate"] = bool(cfg["participate"])
    cfg["personas"] = await dao.list_personas(group_id)
    return BotGroupResponse(**cfg)


# ── Persona management per group ──────────────────────────────

@router.put("/groups/{group_id}/personas")
async def add_persona(group_id: str, req: BotPersonaAddRequest):
    await _require_archive(req.archive_id)
    await dao.add_persona(group_id, req.archive_id, req.label)
    return {"status": "ok", "group_id": group_id, "archive_id": req.archive_id}


@router.post("/groups/{group_id}/personas/{archive_id}/activate")
async def activate_persona(group_id: str, archive_id: str):
    await _require_archive(archive_id)
    # Check that persona is registered for this group
    personas = await dao.list_personas(group_id)
    ids = {p["archive_id"] for p in personas}
    if archive_id not in ids:
        # Auto-register
        await dao.add_persona(group_id, archive_id, "")
    await dao.activate_persona(group_id, archive_id)
    await dao.set_participate(group_id, True)
    return {"status": "ok", "group_id": group_id, "active_archive_id": archive_id}


@router.get("/groups/{group_id}/personas")
async def list_personas(group_id: str):
    return {"group_id": group_id, "personas": await dao.list_personas(group_id)}


@router.delete("/groups/{group_id}/personas/{archive_id}")
async def remove_persona(group_id: str, archive_id: str):
    await dao.remove_persona(group_id, archive_id)
    return {"status": "ok"}


# ── Current archive (global) ──────────────────────────────────

@router.get("/current-archive")
async def get_current_archive():
    aid = await dao.get_setting("current_archive_id")
    return {"archive_id": aid}


@router.put("/current-archive")
async def set_current_archive(req: CurrentArchiveRequest):
    await dao.set_setting("current_archive_id", req.archive_id)
    return {"archive_id": req.archive_id, "status": "ok"}


# ── Admin group ──────────────────────────────────────────────

@router.get("/admin-group")
async def get_admin_group():
    gid = await dao.get_setting("admin_group_id")
    return {"admin_group_id": gid}


@router.put("/admin-group")
async def set_admin_group(req: AdminGroupRequest):
    await dao.set_setting("admin_group_id", req.group_id)
    return {"admin_group_id": req.group_id, "status": "ok"}


@router.delete("/admin-group")
async def delete_admin_group():
    await dao.set_setting("admin_group_id", "")
    return {"admin_group_id": None, "status": "removed"}
