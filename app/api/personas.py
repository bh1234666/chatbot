"""
人设管理 API。
"""
from fastapi import APIRouter, HTTPException, status

from app.schemas.api import PersonaUpdateRequest, PersonaResponse, PersonaMetaResponse
from app.memory import archive as dao
from app.memory import persona_files


router = APIRouter(prefix="/v1", tags=["personas"])


# ── 人设文件列表 ──────────────────────────────────────────────

@router.get("/personas", response_model=list[PersonaMetaResponse])
async def list_personas() -> list[PersonaMetaResponse]:
    return [PersonaMetaResponse(id=m.id, name=m.name, description=m.description)
            for m in persona_files.list_personas()]


# ── 存档人设 CRUD ──────────────────────────────────────────────

@router.put("/archives/{archive_id}/persona", response_model=PersonaResponse)
async def update_persona(archive_id: str, req: PersonaUpdateRequest) -> PersonaResponse:
    if not await dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    row = await dao.upsert_persona(archive_id, req.content)
    return PersonaResponse(**row)


@router.get("/archives/{archive_id}/persona", response_model=PersonaResponse)
async def get_persona(archive_id: str) -> PersonaResponse:
    if not await dao.get_archive(archive_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    row = await dao.get_persona_full(archive_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "persona not set")
    return PersonaResponse(**row)
