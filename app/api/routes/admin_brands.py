import logging
from typing import Annotated
from fastapi import APIRouter, Depends, Body, Query
from app.core.database import DbDep
from app.core.security import get_current_admin, CurrentAdminDep
from app.schemas.brand import AdminBrandListResponse, AdminBrandPublic, RenameBrandRequest, BrandUpdateCountResponse
from app.services import brand_service, audit_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/brands", tags=["Admin - Brands"], dependencies=[Depends(get_current_admin)])

# Marcas como grupo (Product.brand, case-insensitive) - para asignar la marca de varios
# productos a la vez ver PATCH /admin/products/brand (admin_products.py).


@router.get("", response_model=AdminBrandListResponse, summary="Listar marcas con conteo y variantes de escritura")
async def admin_list_brands(db: DbDep):
    """Una entrada por marca (agrupada case-insensitive), ordenada por nombre, sobre productos
    no eliminados (activos o no). `variants` con mas de un valor = duplicado por casing a
    fusionar via `POST /admin/brands/rename`. `unbrandedCount` = productos sin marca."""
    docs, unbranded_count = await brand_service.list_brands(db)
    return AdminBrandListResponse(docs=[AdminBrandPublic(**d) for d in docs], unbranded_count=unbranded_count)


@router.post("/rename", response_model=BrandUpdateCountResponse, summary="Renombrar o fusionar una marca")
async def admin_rename_brand(db: DbDep, current: CurrentAdminDep, data: RenameBrandRequest = Body()):
    """Todo producto cuya marca coincida con `from` (case-insensitive) pasa a `to`; si `to` ya
    existe como marca, las dos quedan fusionadas. `404` si ningun producto tiene `from`;
    `422` si `from`/`to` vienen vacios."""
    updated = await brand_service.rename_brand(db, data.from_, data.to)
    await audit_service.log_action(db, current, "brand.rename", "brand", data.from_, {"from": data.from_, "to": data.to, "updated": updated})
    await db.commit()
    return BrandUpdateCountResponse(updated=updated)


@router.delete("", response_model=BrandUpdateCountResponse, summary="Quitar una marca de todos sus productos")
async def admin_delete_brand(db: DbDep, current: CurrentAdminDep, name: Annotated[str, Query(min_length=1)]):
    """Pone la marca en null en todo producto con `name` (case-insensitive). Idempotente:
    `{updated: 0}` si ningun producto la tiene."""
    name = name.strip()
    updated = await brand_service.clear_brand(db, name)
    await audit_service.log_action(db, current, "brand.delete", "brand", name, {"updated": updated})
    await db.commit()
    return BrandUpdateCountResponse(updated=updated)
