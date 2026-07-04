from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime, timedelta
from app.core.database import get_db
from app.models.product import Product
from app.services.product_service import fetch_full_details_from_sicar

router = APIRouter()

@router.get("/products/{uuid}")
async def get_product_details(uuid: str, db: AsyncSession = Depends(get_db)):
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
        datetime.now() - product.details_updated_at > timedelta(days=1)
    )

    if needs_update:
        print(f"Datos obsoletos para {uuid}. Descargando de GraphQL...")
        
        # Llamada al servicio que maneja el auto-login
        full_data = await fetch_full_details_from_sicar(product.sicar_uuid)
        
        # Actualizamos el modelo en PostgreSQL
        if full_data:
            product.additional_skus = full_data.get("skus")
            product.description_details = full_data.get("details")
            product.tags = full_data.get("tags")
            product.sales_unit_uuid = full_data.get("sales_unit_uuid")
            product.additional_images = full_data.get("additional_images")
            product.details_updated_at = datetime.now()
            
            await db.commit()
            await db.refresh(product)
            print(f"Producto {uuid} actualizado con éxito en la base de datos local.")

    return product