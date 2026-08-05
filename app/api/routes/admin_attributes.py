import logging
from typing import Optional
from fastapi import APIRouter, Depends, Body, Query, status
from app.core.database import DbDep
from app.core.security import validate_admin_key
from app.schemas.attribute import (
    DataType,
    AttributePublic,
    AttributeCreateRequest,
    AttributeUpdateRequest,
    AttributeListResponse,
    AttributeProductsResponse,
)
from app.schemas.products import ProductBasic
from app.services import attribute_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/attributes", tags=["Admin - Attributes"], dependencies=[Depends(validate_admin_key)])


@router.post("", response_model=AttributePublic, status_code=status.HTTP_201_CREATED, summary="Crear una definicion de atributo")
async def admin_create_attribute(db: DbDep, data: AttributeCreateRequest = Body()):
    """Crea una definicion en el catalogo de atributos. `slug` siempre se deriva de `name`.
    `allowedValues` es requerido (minimo 2) cuando `dataType` es ENUM. Los VALORES reales
    por producto viven en `Product.attributes` (JSONB), no aqui."""
    attribute = await attribute_service.create_attribute(db, data.name, data.data_type, data.allowed_values, data.unit)
    return AttributePublic.model_validate(attribute)


@router.get("", response_model=AttributeListResponse, summary="Buscar/listar atributos (filtrable por texto y dataType)")
async def admin_list_attributes(
    db: DbDep,
    search: Optional[str] = Query(default=None, description="Coincidencia parcial contra name, sin distinguir mayusculas"),
    data_type: Optional[DataType] = Query(default=None, alias="dataType"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    total, attributes = await attribute_service.list_attributes(db, search=search, data_type=data_type, limit=limit, offset=offset)
    return AttributeListResponse(total=total, docs=[AttributePublic.model_validate(a) for a in attributes])


@router.patch("/{attribute_uuid}", response_model=AttributePublic, summary="Actualizacion parcial de un atributo")
async def admin_update_attribute(attribute_uuid: str, db: DbDep, data: AttributeUpdateRequest = Body()):
    """Actualizacion parcial (`exclude_unset`). Renombrar (cambia el slug) o cambiar
    `dataType` mientras el atributo ya tiene valores guardados en productos se bloquea
    (`409`) - ambos romperian esos valores."""
    attribute = await attribute_service.update_attribute(db, attribute_uuid, data.model_dump(exclude_unset=True))
    return AttributePublic.model_validate(attribute)


@router.delete("/{attribute_uuid}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar un atributo (bloqueado si tiene valores guardados o esta en un preset)")
async def admin_delete_attribute(attribute_uuid: str, db: DbDep):
    """Borrado real. `409` si algun producto tiene esta clave en `attributes` o si esta
    asignado a algun preset - quitarlo de ambos primero."""
    await attribute_service.delete_attribute(db, attribute_uuid)


@router.get("/{attribute_uuid}/products", response_model=AttributeProductsResponse, summary="Listar productos que tienen este atributo guardado")
async def admin_list_attribute_products(
    attribute_uuid: str,
    db: DbDep,
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Direccion inversa de `GET /admin/products/{uuid}/attributes`: dado un atributo, que
    productos tienen esta clave guardada en `attributes` (JSONB) - mismo proposito que
    `GET /admin/categories/{uuid}/products`/`GET /admin/vehicles/{uuid}/products`, via
    containment JSONB en vez de una tabla pivote. `404` si el atributo no existe."""
    total, products = await attribute_service.list_products_with_attribute(db, attribute_uuid, limit, offset)
    return AttributeProductsResponse(total=total, docs=[ProductBasic.model_validate(p) for p in products])
