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
    AttributeProductPublic,
    AttributeProductsResponse,
    ReplaceAttributeProductsRequest,
    ReplaceAttributeProductsResponse,
    PatchAttributeProductsRequest,
    PatchAttributeProductsResponse,
)
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
    containment JSONB en vez de una tabla pivote. A diferencia de esas dos rutas, cada
    producto trae su `value` guardado para este atributo (no solo membresia) - pensado
    para precargar la UI de edicion antes de un `PUT` de abajo. `404` si el atributo no
    existe."""
    total, attribute, products = await attribute_service.list_products_with_attribute(db, attribute_uuid, limit, offset)
    docs = [
        AttributeProductPublic(
            sicar_uuid=p.sicar_uuid,
            sku=p.sku,
            name=p.name,
            description_details=p.description_details,
            image_url=p.image_url,
            price=p.price,
            stock=p.stock,
            value=(p.attributes or {}).get(attribute.slug),
        )
        for p in products
    ]
    return AttributeProductsResponse(total=total, docs=docs)


@router.put("/{attribute_uuid}/products", response_model=ReplaceAttributeProductsResponse, summary="Reemplazar el conjunto completo de productos que tienen este atributo asignado")
async def admin_replace_attribute_products(attribute_uuid: str, db: DbDep, data: ReplaceAttributeProductsRequest = Body()):
    """Direccion attribute-primero: asigna/actualiza este atributo en un lote de productos
    de una sola llamada, en vez de un `PUT /admin/products/{uuid}/attributes` por producto.
    **Reemplazo completo, no incremental** - un producto que ya tenia este atributo y no
    aparece en `values` pierde la clave; los que si aparecen quedan con el `value` dado.
    Solo TOCA la clave de este atributo en cada producto - cualquier otro atributo que ya
    tuviera guardado no se toca (a diferencia de `PUT /admin/products/{uuid}/attributes`,
    que reemplaza el diccionario completo de un producto).

    `404` si algun `productUuid` no resuelve. `422` si algun `value` no cuadra con el
    `dataType`/`allowedValues` de este atributo (nombra cuales) - no escribe nada hasta que
    todos pasen. Para atributos con mas productos asignados que el limite de
    `GET .../products` (200), preferir el `PATCH` de abajo para no arriesgar sobreescribir
    asignaciones que el picker del dashboard nunca cargo."""
    docs = await attribute_service.replace_attribute_products(db, attribute_uuid, [v.model_dump() for v in data.values])
    return ReplaceAttributeProductsResponse(attribute_uuid=attribute_uuid, docs=docs)


@router.patch("/{attribute_uuid}/products", response_model=PatchAttributeProductsResponse, summary="Agregar/actualizar o quitar el valor de este atributo en un lote de productos, de forma incremental")
async def admin_patch_attribute_products(attribute_uuid: str, db: DbDep, data: PatchAttributeProductsRequest = Body()):
    """Incremental (a diferencia del `PUT` de arriba): asigna/actualiza el `value` de este
    atributo en `upsert` o le quita la clave a `remove`, sin necesitar conocer el conjunto
    completo - seguro de usar aunque el atributo tenga mas productos que el limite de
    `GET .../products`. `remove` de productos que no tengan la clave (o no existan) se
    ignora (no-op tolerante). `422` si un mismo `productUuid` aparece en `upsert` y
    `remove` a la vez, o si algun `value` no cuadra con el `dataType`/`allowedValues` de
    este atributo (nombra cuales) - no escribe nada hasta que todos pasen. `404` si algun
    `productUuid` de `upsert` no resuelve."""
    upserted, removed, upserted_count, removed_count = await attribute_service.patch_attribute_products(
        db, attribute_uuid, [v.model_dump() for v in data.upsert], data.remove
    )
    return PatchAttributeProductsResponse(
        attribute_uuid=attribute_uuid,
        upserted=upserted,
        removed=removed,
        upserted_count=upserted_count,
        removed_count=removed_count,
    )
