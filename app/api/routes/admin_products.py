import logging
from fastapi import APIRouter, Depends, Body, status
from app.core.database import DbDep
from app.core.security import validate_admin_key
from app.schemas.attribute import (
    ProductAttributesResponse,
    AttributeValuePublic,
    ReplaceProductAttributesRequest,
    SetProductVariantGroupRequest,
    SetProductVariantGroupResponse,
)
from app.services import attribute_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/products", tags=["Admin - Products"], dependencies=[Depends(validate_admin_key)])

# No hay superficie admin para editar los campos propios del producto (name/price/stock/...)
# - esos siguen siendo propiedad de Sicar X, sincronizados por el worker. Este router solo
# cubre lo que este PIM administra localmente: atributos EAV y agrupacion de variantes.


@router.get("/{product_uuid}/attributes", response_model=ProductAttributesResponse, summary="Ver los atributos guardados de un producto")
async def admin_get_product_attributes(product_uuid: str, db: DbDep):
    """`404` si el producto no existe/esta eliminado. `docs: []` si existe pero no tiene
    ningun atributo guardado todavia (no es un error)."""
    docs = await attribute_service.get_product_attributes(db, product_uuid)
    return ProductAttributesResponse(product_uuid=product_uuid, docs=[AttributeValuePublic.model_validate(d) for d in docs])


@router.put("/{product_uuid}/attributes", response_model=ProductAttributesResponse, summary="Reemplazar el conjunto completo de atributos guardados de un producto")
async def admin_replace_product_attributes(product_uuid: str, db: DbDep, data: ReplaceProductAttributesRequest = Body()):
    """Reemplaza el conjunto COMPLETO de `Product.attributes` (no incremental). `404` si
    algun `attributeUuid` no resuelve contra el catalogo; `422` si algun `value` no cuadra
    con el `dataType`/`allowedValues` del atributo referenciado - nombra los que fallen,
    no escribe nada hasta que todos pasen."""
    docs = await attribute_service.replace_product_attributes(db, product_uuid, [v.model_dump() for v in data.values])
    return ProductAttributesResponse(product_uuid=product_uuid, docs=[AttributeValuePublic.model_validate(d) for d in docs])


@router.patch("/{product_uuid}/variant-group", response_model=SetProductVariantGroupResponse, summary="Asignar o quitar el grupo de variantes de un producto")
async def admin_set_product_variant_group(product_uuid: str, db: DbDep, data: SetProductVariantGroupRequest = Body()):
    """Convenience de un solo producto - para asignar/reasignar varios a la vez usar
    `PUT /admin/variant-groups/{uuid}/products`. `variantGroupUuid: null` quita al
    producto de cualquier grupo."""
    product = await attribute_service.set_product_variant_group(db, product_uuid, data.variant_group_uuid)
    return SetProductVariantGroupResponse(product_uuid=product_uuid, variant_group_uuid=product.variant_group_uuid)
