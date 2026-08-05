import logging
from typing import Optional
from fastapi import APIRouter, Depends, Body, Query, status
from app.core.database import DbDep
from app.core.security import validate_admin_key
from app.schemas.attribute import (
    VariantGroupPublic,
    VariantGroupCreateRequest,
    VariantGroupUpdateRequest,
    VariantGroupListResponse,
    ReplaceVariantGroupProductsRequest,
    ReplaceVariantGroupProductsResponse,
    PatchVariantGroupProductsRequest,
    PatchVariantGroupProductsResponse,
    VariantGroupProductsResponse,
)
from app.schemas.products import ProductBasic
from app.services import attribute_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/variant-groups", tags=["Admin - Variant Groups"], dependencies=[Depends(validate_admin_key)])


@router.post("", response_model=VariantGroupPublic, status_code=status.HTTP_201_CREATED, summary="Crear un grupo de variantes")
async def admin_create_variant_group(db: DbDep, data: VariantGroupCreateRequest = Body()):
    """Vincula explicitamente SKUs de Sicar X que son la misma pieza en variantes (p. ej.
    color/talla) - cada SKU sigue siendo su propia fila de Product con su propio
    sicar_uuid/precio/stock. `variantAttributeSlug` es texto libre, solo indica al
    frontend que selector renderizar."""
    group = await attribute_service.create_variant_group(db, data.name, data.variant_attribute_slug)
    return VariantGroupPublic.model_validate(group)


@router.get("", response_model=VariantGroupListResponse, summary="Buscar/listar grupos de variantes")
async def admin_list_variant_groups(
    db: DbDep,
    search: Optional[str] = Query(default=None, description="Coincidencia parcial contra name, sin distinguir mayusculas"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    total, groups = await attribute_service.list_variant_groups(db, search=search, limit=limit, offset=offset)
    return VariantGroupListResponse(total=total, docs=[VariantGroupPublic.model_validate(g) for g in groups])


@router.patch("/{variant_group_uuid}", response_model=VariantGroupPublic, summary="Actualizacion parcial de un grupo de variantes")
async def admin_update_variant_group(variant_group_uuid: str, db: DbDep, data: VariantGroupUpdateRequest = Body()):
    group = await attribute_service.update_variant_group(db, variant_group_uuid, data.model_dump(exclude_unset=True))
    return VariantGroupPublic.model_validate(group)


@router.delete("/{variant_group_uuid}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar un grupo de variantes (bloqueado si tiene productos asignados)")
async def admin_delete_variant_group(variant_group_uuid: str, db: DbDep):
    """Borrado real. `409` si algun producto todavia apunta a este grupo - quitalos primero."""
    await attribute_service.delete_variant_group(db, variant_group_uuid)


@router.put("/{variant_group_uuid}/products", response_model=ReplaceVariantGroupProductsResponse, summary="Reemplazar el conjunto completo de productos de un grupo de variantes")
async def admin_replace_variant_group_products(variant_group_uuid: str, db: DbDep, data: ReplaceVariantGroupProductsRequest = Body()):
    """Reemplaza el conjunto COMPLETO de miembros: limpia el grupo de quien ya no este en
    la lista y lo asigna a quien si. `404` si algun uuid no resuelve. Para grupos con mas
    productos asignados que el limite de `GET .../products` (200), preferir el `PATCH` de
    abajo para no arriesgar sobreescribir asignaciones que el picker del dashboard nunca
    cargo."""
    product_uuids = await attribute_service.replace_variant_group_products(db, variant_group_uuid, data.product_uuids)
    return ReplaceVariantGroupProductsResponse(variant_group_uuid=variant_group_uuid, product_uuids=product_uuids)


@router.patch("/{variant_group_uuid}/products", response_model=PatchVariantGroupProductsResponse, summary="Agregar/quitar productos de un grupo de variantes de forma incremental")
async def admin_patch_variant_group_products(variant_group_uuid: str, db: DbDep, data: PatchVariantGroupProductsRequest = Body()):
    """Incremental (a diferencia del `PUT` de arriba): agrega/quita sin necesitar conocer
    el conjunto completo - seguro de usar aunque el grupo tenga mas productos que el
    limite de `GET .../products`. `add` reasigna incondicionalmente (si un producto ya
    estaba en otro grupo, esta llamada gana). `remove` solo quita la pertenencia si el
    producto sigue en ESTE grupo - no le toca `variantGroupUuid` si mientras tanto ya fue
    movido a otro grupo por una llamada distinta. `422` si un mismo `productUuid` aparece
    en `add` y `remove` a la vez. `404` si algun uuid de `add` no resuelve."""
    added, removed, added_count, removed_count = await attribute_service.patch_variant_group_products(
        db, variant_group_uuid, data.add, data.remove
    )
    return PatchVariantGroupProductsResponse(
        variant_group_uuid=variant_group_uuid, added=added, removed=removed, added_count=added_count, removed_count=removed_count
    )


@router.get("/{variant_group_uuid}/products", response_model=VariantGroupProductsResponse, summary="Listar productos asignados a un grupo de variantes")
async def admin_list_variant_group_products(
    variant_group_uuid: str,
    db: DbDep,
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    total, products = await attribute_service.list_variant_group_products(db, variant_group_uuid, limit, offset)
    return VariantGroupProductsResponse(total=total, docs=[ProductBasic.model_validate(p) for p in products])
