import logging
from fastapi import APIRouter, Depends, Body, Query, status
from app.core.database import DbDep
from app.core.security import validate_admin_key
from app.schemas.taxonomy import (
    CategoryAdminPublic,
    CategoryCreateRequest,
    CategoryUpdateRequest,
    ReplaceCategoryProductsRequest,
    ReplaceCategoryProductsResponse,
    CategoryProductsResponse,
)
from app.schemas.products import ProductBasic
from app.services import taxonomy_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/categories", tags=["Admin - Taxonomy"], dependencies=[Depends(validate_admin_key)])


@router.post("", response_model=CategoryAdminPublic, status_code=status.HTTP_201_CREATED, summary="Crear un nodo de categoria")
async def admin_create_category(db: DbDep, data: CategoryCreateRequest = Body()):
    category = await taxonomy_service.create_category(db, data.name, data.parent_uuid)
    return CategoryAdminPublic.model_validate(category)


@router.patch("/{category_uuid}", response_model=CategoryAdminPublic, summary="Renombrar y/o mover (reasignar padre) un nodo de categoria")
async def admin_update_category(category_uuid: str, db: DbDep, data: CategoryUpdateRequest = Body()):
    category = await taxonomy_service.update_category(db, category_uuid, data.model_dump(exclude_unset=True))
    return CategoryAdminPublic.model_validate(category)


@router.delete("/{category_uuid}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar un nodo de categoria (bloqueado si tiene subcategorias o productos asignados)")
async def admin_delete_category(category_uuid: str, db: DbDep):
    await taxonomy_service.delete_category(db, category_uuid)


@router.put("/{category_uuid}/products", response_model=ReplaceCategoryProductsResponse, summary="Reemplazar el conjunto completo de productos asignados a una categoria")
async def admin_replace_category_products(category_uuid: str, db: DbDep, data: ReplaceCategoryProductsRequest = Body()):
    product_uuids = await taxonomy_service.replace_category_products(db, category_uuid, data.product_uuids)
    return ReplaceCategoryProductsResponse(category_uuid=category_uuid, product_uuids=product_uuids)


@router.get("/{category_uuid}/products", response_model=CategoryProductsResponse, summary="Listar productos asignados directamente a una categoria (sin incluir descendientes)")
async def admin_list_category_products(
    category_uuid: str,
    db: DbDep,
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    total, products = await taxonomy_service.list_category_products(db, category_uuid, limit, offset)
    return CategoryProductsResponse(total=total, docs=[ProductBasic.model_validate(p) for p in products])
