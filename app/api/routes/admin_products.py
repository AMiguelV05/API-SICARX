import logging
from fastapi import APIRouter, Depends, Body, status
from app.core.database import DbDep
from app.core.security import get_current_admin
from app.schemas.attribute import (
    ProductAttributesResponse,
    AttributeValuePublic,
    ReplaceProductAttributesRequest,
    SetProductVariantGroupRequest,
    SetProductVariantGroupResponse,
)
from app.schemas.products import ProductInfoUpdateRequest, ProductInfoPublic
from app.services import attribute_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/products", tags=["Admin - Products"], dependencies=[Depends(get_current_admin)])

# La mayoria de los campos propios del producto (name/price/stock/...) siguen siendo
# propiedad de Sicar X, sincronizados por el worker - no editables aqui. Este router cubre
# lo que este PIM administra localmente: atributos EAV, agrupacion de variantes, y (abajo)
# marca/bullets/especificaciones/contenido.


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


@router.patch("/{product_uuid}/info", response_model=ProductInfoPublic, summary="Actualizar marca/bullets/especificaciones tecnicas/contenido de un producto")
async def admin_update_product_info(product_uuid: str, db: DbDep, data: ProductInfoUpdateRequest = Body()):
    """Actualizacion parcial (exclude_unset=True) - un campo omitido no se toca, enviado
    explicitamente como null se borra. `404` si el producto no existe/esta eliminado. No hay
    GET equivalente aqui: `GET /products/{uuid}` (publico) ya expone estos mismos campos."""
    product = await attribute_service.update_product_info(db, product_uuid, data.model_dump(exclude_unset=True))
    return ProductInfoPublic(
        product_uuid=product_uuid,
        brand=product.brand,
        bullet_points=product.bullet_points,
        technical_specs=product.technical_specs,
        contents=product.contents,
    )
