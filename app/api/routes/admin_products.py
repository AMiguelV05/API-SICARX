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
from app.schemas.brand import BulkSetBrandRequest, BulkSetBrandResponse
from app.schemas.sale import BulkSaleClearRequest, BulkSaleResponse, BulkSaleSetRequest, ProductSalePublic, ProductSaleSetRequest
from app.core.security import CurrentAdminDep
from app.services import attribute_service, brand_service, audit_service, sale_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/products", tags=["Admin - Products"], dependencies=[Depends(get_current_admin)])

# La mayoria de los campos propios del producto (name/price/stock/...) siguen siendo
# propiedad de Sicar X, sincronizados por el worker - no editables aqui. Este router cubre
# lo que este PIM administra localmente: atributos EAV, agrupacion de variantes, y (abajo)
# descripcion/marca/bullets/especificaciones/contenido.


# Declarado antes de las rutas /{product_uuid}/... por claridad - no chocan de todos modos
# (esas llevan dos segmentos).
@router.patch("/brand", response_model=BulkSetBrandResponse, summary="Asignar o quitar la marca de varios productos a la vez")
async def admin_bulk_set_brand(db: DbDep, current: CurrentAdminDep, data: BulkSetBrandRequest = Body()):
    """Asigna `brand` (recortada; null o "" la borra) a 1-500 productos. Exito parcial:
    uuids que no resuelven a un producto no eliminado vuelven en `notFound` en vez de
    rechazar toda la solicitud."""
    updated, not_found = await brand_service.set_brand_for_products(db, data.product_uuids, data.brand)
    brand = brand_service.normalize_brand(data.brand)
    await audit_service.log_action(
        db, current, "product.brand_bulk_set", "brand", brand or "(sin marca)",
        {"brand": brand, "productUuids": data.product_uuids, "updated": updated, "notFound": not_found},
    )
    await db.commit()
    return BulkSetBrandResponse(updated=updated, not_found=not_found)


# Precio de oferta - ver sale_service y "Precio de oferta" en CLAUDE.md. Las rutas masivas
# (/sale, un segmento) no chocan con las de un producto (/{product_uuid}/sale, dos).
@router.put("/sale", response_model=BulkSaleResponse, summary="Capturar el precio anterior (oferta) de varios productos")
async def admin_bulk_set_sale(db: DbDep, current: CurrentAdminDep, data: BulkSaleSetRequest = Body()):
    """Objetivo: `productUuids` (1-500) o `categoryUuid` (con descendientes). Precio: un
    `compareAtPrice` fijo, o `compareAtPercent` para calcularlo por producto a partir de su
    price actual. Exito parcial: ver `notFound`/`skipped`."""
    updated, not_found, skipped = await sale_service.bulk_set_sale(
        db, product_uuids=data.product_uuids, category_uuid=data.category_uuid,
        compare_at_price=data.compare_at_price, compare_at_percent=data.compare_at_percent,
        sale_ends_at=data.sale_ends_at,
    )
    await audit_service.log_action(
        db, current, "product.sale_bulk_set", "product", data.category_uuid or f"{len(data.product_uuids)} productos",
        {
            "productUuids": data.product_uuids, "categoryUuid": data.category_uuid,
            "compareAtPrice": data.compare_at_price, "compareAtPercent": data.compare_at_percent,
            "saleEndsAt": data.sale_ends_at.isoformat() if data.sale_ends_at else None,
            "updated": updated, "notFound": not_found, "skipped": skipped,
        },
    )
    await db.commit()
    return BulkSaleResponse(updated=updated, not_found=not_found, skipped=skipped)


@router.delete("/sale", response_model=BulkSaleResponse, summary="Quitar la oferta de varios productos")
async def admin_bulk_clear_sale(db: DbDep, current: CurrentAdminDep, data: BulkSaleClearRequest = Body()):
    updated, not_found = await sale_service.bulk_clear_sale(db, product_uuids=data.product_uuids, category_uuid=data.category_uuid)
    await audit_service.log_action(
        db, current, "product.sale_bulk_clear", "product", data.category_uuid or f"{len(data.product_uuids)} productos",
        {"productUuids": data.product_uuids, "categoryUuid": data.category_uuid, "updated": updated, "notFound": not_found},
    )
    await db.commit()
    return BulkSaleResponse(updated=updated, not_found=not_found)


def _sale_public(product) -> ProductSalePublic:
    return ProductSalePublic(
        product_uuid=product.sicar_uuid,
        price=float(product.price),
        compare_at_price=float(product.compare_at_price) if product.compare_at_price is not None else None,
        sale_ends_at=product.sale_ends_at,
        is_on_sale=product.is_on_sale,
    )


@router.put("/{product_uuid}/sale", response_model=ProductSalePublic, summary="Capturar el precio anterior (oferta) de un producto")
async def admin_set_product_sale(product_uuid: str, db: DbDep, current: CurrentAdminDep, data: ProductSaleSetRequest = Body()):
    """`compareAtPrice` es el precio ANTERIOR con IVA (lo que se muestra tachado); el precio
    con descuento sigue siendo el `price` de Sicar X. `422` si no es mayor que el price
    actual o si `saleEndsAt` ya paso; `404` si el producto no existe."""
    product = await sale_service.set_sale(db, product_uuid, data.compare_at_price, data.sale_ends_at)
    await audit_service.log_action(
        db, current, "product.sale_set", "product", product_uuid,
        {"compareAtPrice": data.compare_at_price, "saleEndsAt": data.sale_ends_at.isoformat() if data.sale_ends_at else None},
    )
    await db.commit()
    return _sale_public(product)


@router.delete("/{product_uuid}/sale", status_code=status.HTTP_204_NO_CONTENT, summary="Quitar la oferta de un producto")
async def admin_clear_product_sale(product_uuid: str, db: DbDep, current: CurrentAdminDep):
    """Idempotente: quitarla de un producto sin oferta tambien es `204`."""
    await sale_service.clear_sale(db, product_uuid)
    await audit_service.log_action(db, current, "product.sale_clear", "product", product_uuid)
    await db.commit()


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


@router.patch("/{product_uuid}/info", response_model=ProductInfoPublic, summary="Actualizar descripcion/marca/bullets/especificaciones tecnicas/contenido de un producto")
async def admin_update_product_info(product_uuid: str, db: DbDep, data: ProductInfoUpdateRequest = Body()):
    """Actualizacion parcial (exclude_unset=True) - un campo omitido no se toca, enviado
    explicitamente como null se borra. `404` si el producto no existe/esta eliminado. No hay
    GET equivalente aqui: `GET /products/{uuid}` (publico) ya expone estos mismos campos."""
    product = await attribute_service.update_product_info(db, product_uuid, data.model_dump(exclude_unset=True))
    return ProductInfoPublic(
        product_uuid=product_uuid,
        description=product.description,
        brand=product.brand,
        bullet_points=product.bullet_points,
        technical_specs=product.technical_specs,
        contents=product.contents,
    )
