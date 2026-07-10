import logging
from fastapi import APIRouter, Depends, HTTPException, Body, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime, timedelta, timezone
from app.core.database import get_db
from app.core.security import validate_api_key

from app.models.product import Product
from app.services.product_service import fetch_full_details_from_sicar
from app.schemas.products import LocalCatalogFilter, LocalCatalogResponse
from app.services.catalog_service import get_local_catalog

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/products/{uuid}")
async def get_product_details(
    uuid: str, 
    db: AsyncSession = Depends(get_db),
    _ : str = Depends(validate_api_key)):
    
    """
    Busca un producto localmente. Si no tiene detalles o pasaron 24 horas,
    hace scraping al servidor central de Sicar para actualizar la base de datos.
    """
    result = await db.execute(select(Product).filter(Product.sicar_uuid == uuid))
    product = result.scalars().first()

    if not product:
        raise HTTPException(status_code=404, detail="Producto no encontrado")

    # Lógica de expiración
    needs_update = (
        product.details_updated_at is None or
        datetime.now(timezone.utc) - product.details_updated_at > timedelta(days=1)
    )
    logger.debug(f"Producto {uuid}: details_updated_at={product.details_updated_at}, needs_update={needs_update}")
    if needs_update:
        logger.info(f"Datos obsoletos para {uuid}. Descargando de GraphQL...")
        
        # Llamada al servicio que maneja el auto-login
        full_data = await fetch_full_details_from_sicar(product.sicar_uuid)
        
        # Actualizamos el modelo en PostgreSQL
        if full_data:
            product.additional_skus = full_data.get("skus")
            product.description_details = full_data.get("details")
            product.tags = full_data.get("tags")
            product.sales_unit_uuid = full_data.get("sales_unit_uuid")
            product.additional_images = full_data.get("additional_images")
            product.details_updated_at = datetime.now(timezone.utc)
            
            await db.commit()
            await db.refresh(product)
            logger.info(f"Producto {uuid} actualizado con exito en la base de datos local.")

    return product

@router.post("/catalog", response_model=LocalCatalogResponse, summary="Obtener catálogo local")
async def get_catalog(
    filter_data: LocalCatalogFilter = Body(...),
    db: AsyncSession = Depends(get_db),
    _ : str = Depends(validate_api_key)
):
    """
    Obtiene la lista de productos directamente desde la base de datos local.
    Retorna solo la información básica necesaria para las tarjetas de producto.
    """
    try:
        # Convertimos el modelo de Pydantic a diccionario
        result = await get_local_catalog(db, filter_data.model_dump())
        return result
    except Exception as e:
        logger.error(f"Error al obtener el catalogo local: {str(e)}")
        raise HTTPException(status_code=400, detail="No se pudo obtener el catálogo local. Intenta nuevamente.")