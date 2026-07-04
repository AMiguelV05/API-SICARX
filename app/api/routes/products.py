from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime, timedelta
from app.core.database import get_db
from app.models.product import Product
from app.core.config import settings
import httpx

router = APIRouter()

async def fetch_full_details_from_sicar(uuid: str):
    graphql_url = "https://api.sicarx.com/graph/v1/"
    headers = {
        "Authorization": f"Bearer {settings.SICAR_TOKEN}",
        "Content-Type": "application/graphql",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0"
    }
    graphql_query = f"""{{
        product(uuid: "{uuid}") {{
            skus
            details
            tags
            salesUnitUuid
        }}
        listImages (uuid: "{uuid}") {{
            url
        }}
    }}"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(graphql_url, content=graphql_query, headers=headers)
            
            if response.status_code != 200:
                print(f"Error fetching details for UUID {uuid}. Status code: {response.status_code}")
                return {}
            
            response.raise_for_status()

            data = response.json()
            if "errors" in data:
                print(f"GraphQL errors for UUID {uuid}: {data['errors']}")
                return {}
            
            product_data = data.get("data", {}).get("product", {})
            images_data = data.get("data", {}).get("listImages", [])

            return {
                "skus": product_data.get("skus"),
                "details": product_data.get("details"),
                "tags": product_data.get("tags"),
                "sales_unit_uuid": product_data.get("salesUnitUuid"),
                "additional_images": [img.get("url") for img in images_data if img.get("url")]
            }
        except httpx.RequestError as e:
            print(f"Request error while fetching details for UUID {uuid}: {e}")
            return {}

"""Si el producto no tiene detalles actualizados en las últimas 24 horas,
se hace una llamada a GraphQL para obtener los detalles completos y actualizar la base de datos."""
@router.get("/products/{uuid}")
async def get_product_details(uuid: str, db: AsyncSession = Depends(get_db)):
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
        full_data = await fetch_full_details_from_sicar(product.sicar_uuid)
        
        # Actualizamos el modelo
        if full_data:
            product.additional_skus = full_data.get("skus")
            product.description_details = full_data.get("details")
            product.tags = full_data.get("tags")
            product.sales_unit_uuid = full_data.get("sales_unit_uuid")
            product.additional_images = full_data.get("additional_images")
            product.details_updated_at = datetime.now()
            
            await db.commit()
            await db.refresh(product)
            print(f"Producto {uuid} actualizado con éxito.")

    return product