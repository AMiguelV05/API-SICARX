import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Body, Depends, Query, status

from app.core.database import DbDep
from app.core.security import CurrentAdminDep, SuperAdminDep, get_current_admin
from app.schemas.search_synonym import (
    SearchSynonymCreate,
    SearchSynonymListResponse,
    SearchSynonymPublic,
    SearchSynonymUpdate,
    SearchSynonymWriteResponse,
)
from app.services import audit_service, synonym_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/search/synonyms", tags=["Admin - Search"], dependencies=[Depends(get_current_admin)])


def _write_response(row, synced: bool) -> SearchSynonymWriteResponse:
    return SearchSynonymWriteResponse(**SearchSynonymPublic.model_validate(row).model_dump(), synced_to_search=synced)


@router.get("", response_model=SearchSynonymListResponse, summary="Listar sinonimos del buscador")
async def admin_list_synonyms(
    db: DbDep,
    q: Annotated[Optional[str], Query(max_length=100, description="Coincidencia parcial contra la raiz o cualquier palabra")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 60,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Mas recientes primero."""
    total, rows = await synonym_service.list_synonym_entries(db, q, limit, offset)
    return SearchSynonymListResponse(total=total, docs=[SearchSynonymPublic.model_validate(r) for r in rows])


@router.post("", response_model=SearchSynonymWriteResponse, status_code=status.HTTP_201_CREATED, summary="Crear un sinonimo")
async def admin_create_synonym(db: DbDep, current: CurrentAdminDep, data: SearchSynonymCreate = Body()):
    """Crea una entrada multi-direccional (`synonyms` solo, 2+ palabras) o de una direccion
    (`root` + 1+ palabras). Las palabras se normalizan igual que las busquedas. `422` si no
    cumple esas reglas; `409` si ya existe una identica. Surte efecto en la busqueda de
    inmediato, sin reindexar (`syncedToSearch`)."""
    row = await synonym_service.create_synonym_entry(db, current, data.root, data.synonyms)
    await audit_service.log_action(db, current, "search_synonym.create", "search_synonym", row.uuid,
                                   {"root": row.root, "synonyms": row.synonyms})
    await db.commit()
    synced = await synonym_service.push_to_search(row)
    return _write_response(row, synced)


@router.patch("/{synonym_uuid}", response_model=SearchSynonymWriteResponse, summary="Actualizar un sinonimo")
async def admin_update_synonym(synonym_uuid: str, db: DbDep, current: CurrentAdminDep, data: SearchSynonymUpdate = Body()):
    """Parcial (`exclude_unset`). `root: null` explicito convierte una entrada de una
    direccion en multi-direccional. `404` si no existe."""
    fields = data.model_dump(exclude_unset=True)
    row, before = await synonym_service.update_synonym_entry(db, current, synonym_uuid, fields)
    await audit_service.log_action(db, current, "search_synonym.update", "search_synonym", row.uuid,
                                   {"before": before, "after": {"root": row.root, "synonyms": row.synonyms}})
    await db.commit()
    synced = await synonym_service.push_to_search(row)
    return _write_response(row, synced)


@router.delete("/{synonym_uuid}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar un sinonimo (solo super_admin)")
async def admin_delete_synonym(synonym_uuid: str, db: DbDep, current: SuperAdminDep):
    """Borrado real, solo `super_admin` (mismo reparto de permisos que cupones). `404` si no
    existe. Se quita del motor de busqueda de inmediato si esta disponible; si no, en la
    siguiente reconciliacion (<= 5 minutos)."""
    snapshot = await synonym_service.delete_synonym_entry(db, synonym_uuid)
    await audit_service.log_action(db, current, "search_synonym.delete", "search_synonym", synonym_uuid, snapshot)
    await db.commit()
    await synonym_service.remove_from_search(synonym_uuid)
