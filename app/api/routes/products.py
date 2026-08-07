import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Body, Query, status
from sqlalchemy.future import select
from datetime import datetime, timedelta, timezone
from app.core.database import DbDep
from app.core.security import validate_api_key

from app.models.product import Product
from app.services.product_service import fetch_full_details_from_sicar
from app.schemas.products import LocalCatalogFilter, LocalCatalogResponse, ProductDetail, AttributeValuePublic, VariantGroupDetail, BestSellersResponse
from app.services.catalog_service import get_local_catalog, get_best_selling_products
from app.services import attribute_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/products", tags=["Products Catalog and Details"], dependencies=[Depends(validate_api_key)])

# Declarado antes de GET /{uuid} - de lo contrario "best-sellers" se interpretaria como un uuid.
@router.get("/best-sellers", response_model=BestSellersResponse, summary="Productos mas vendidos")
async def get_best_sellers(
    db: DbDep,
    limit: int = Query(default=10, ge=1, le=50, description="Cantidad de productos a devolver (1-50)"),
    department_uuid: Optional[str] = Query(default=None, alias="departmentUuid"),
    category_uuid: Optional[str] = Query(default=None, alias="categoryUuid"),
    taxonomy_uuid: Optional[str] = Query(default=None, alias="taxonomyUuid"),
    vehicle_uuid: Optional[str] = Query(default=None, alias="vehicleUuid"),
    in_stock: bool = Query(default=False, alias="inStock"),
):
    """
    Productos mas vendidos (Product.sales_count > 0), pensado para una seccion de la
    pagina principal. Sin paginacion - es un feed acotado de top-N, no un browse.
    """
    docs = await get_best_selling_products(
        db, limit,
        department_uuid=department_uuid,
        category_uuid=category_uuid,
        taxonomy_uuid=taxonomy_uuid,
        vehicle_uuid=vehicle_uuid,
        in_stock=in_stock,
    )
    return BestSellersResponse(docs=docs)

@router.get("/{uuid}", response_model=ProductDetail, summary="Obtener detalle de producto")
async def get_product_details(uuid: str, db: DbDep):
    """
    Busca un producto localmente. Si no tiene detalles o pasaron 24 horas,
    hace scraping al servidor central de Sicar para actualizar la base de datos.
    """
    result = await db.execute(
        select(Product).filter(
            Product.sicar_uuid == uuid,
            Product.is_deleted == False,
            Product.is_active == True,
        )
    )
    product = result.scalars().first()

    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado")

    needs_update = (
        product.details_updated_at is None or
        datetime.now(timezone.utc) - product.details_updated_at > timedelta(days=1)
    )
    logger.debug(f"Producto {uuid}: details_updated_at={product.details_updated_at}, needs_update={needs_update}")
    if needs_update:
        logger.info(f"Datos obsoletos para {uuid}. Descargando de GraphQL...")

        full_data = await fetch_full_details_from_sicar(product.sicar_uuid)

        if full_data:
            product.additional_skus = full_data.get("skus")
            product.description_details = full_data.get("details")
            product.tags = full_data.get("tags")
            product.sales_unit_uuid = full_data.get("sales_unit_uuid")
            product.unit_short_name = full_data.get("unit_short_name")
            product.additional_images = full_data.get("additional_images")
            product.details_updated_at = datetime.now(timezone.utc)

            await db.commit()
            await db.refresh(product)
            logger.info(f"Producto {uuid} actualizado con exito en la base de datos local.")

    # PIM propio (no sincronizado desde Sicar X) - lectura pura de Postgres, sin interaccion
    # con el bloque de lazy-refresh de arriba. attributes: [] / variantGroup: null si el
    # producto no tiene nada guardado - nunca un error.
    attribute_docs = await attribute_service.get_attributes_for_product(db, product)
    variant_group = await attribute_service.get_variant_group_detail(db, product)

    # No se usa ProductDetail.model_validate(product) directo: Product.attributes (JSONB
    # crudo, {"slug": valor}) y ProductDetail.attributes (List[AttributeValuePublic])
    # comparten nombre pero tipos incompatibles - Pydantic fallaria validando el dict crudo
    # antes de poder sobreescribirlo. Se listan los campos propios de Product explicitamente
    # y los dos nuevos (attributes/variantGroup) se calculan aparte.
    detail = ProductDetail(
        id=product.id,
        sicar_uuid=product.sicar_uuid,
        sku=product.sku,
        additional_skus=product.additional_skus,
        name=product.name,
        description_details=product.description_details,
        image_url=product.image_url,
        tags=product.tags,
        additional_images=product.additional_images,
        sales_unit_uuid=product.sales_unit_uuid,
        unit_short_name=product.unit_short_name,
        department_uuid=product.department_uuid,
        category_uuid=product.category_uuid,
        price=product.price,
        stock=product.available_stock,
        is_bulk=product.is_bulk,
        is_active=product.is_active,
        is_deleted=product.is_deleted,
        last_sync_id=product.last_sync_id,
        details_updated_at=product.details_updated_at,
        deleted_at=product.deleted_at,
        attributes=[AttributeValuePublic.model_validate(d) for d in attribute_docs],
        variant_group=VariantGroupDetail.model_validate(variant_group) if variant_group else None,
    )
    return detail

@router.post("", response_model=LocalCatalogResponse, summary="Obtener catálogo local")
async def get_catalog(db: DbDep, filter_data: LocalCatalogFilter = Body()):
    """
    Obtiene la lista de productos directamente desde la base de datos local.
    Retorna solo la información básica necesaria para las tarjetas de producto.
    """
    try:
        result = await get_local_catalog(db, filter_data.model_dump())
        return result
    except Exception as e:
        logger.error(f"Error al obtener el catalogo local: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Ocurrió un error interno al obtener el catálogo local. Intenta más tarde.")
