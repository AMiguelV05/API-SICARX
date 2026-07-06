import httpx
import asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert
from uuid import uuid4
from app.models.product import Product
from datetime import datetime, timezone
from app.core.config import settings
from app.services.sicar_auth import sicar_auth

# Imports para testeo
from app.core.database import AsyncSessionLocal

SICAR_LIST_URL = "https://api.sicarx.com/product/v1/product/list"
PRICE_LIST_ID = settings.SICAR_PRICE_LIST_ID
MAX_RETRIES = 5

async def sync_sicar_catalog(db: AsyncSession, offset: int = 0):
    items_per_page = 300
    total_procesados = 0
    has_more_products = True
    timeout = httpx.Timeout(
        connect=5.0,
        read=10.0,
        write=5.0,
        pool=5.0
    )

    print("Iniciando sincronización paginada con Sicar X...")
    price_key = PRICE_LIST_ID.split("-")[-1]

    current_sync_id = str(uuid4())
    sync_completed_successfully = False

    async with httpx.AsyncClient(timeout=timeout) as client:
        while has_more_products:
            payload = {
                "items": items_per_page,
                "offset": str(offset),
                "priceListId": PRICE_LIST_ID,
                "creationOrder": 2,
                "stock": 1
            }

            # SISTEMA DE REINTENTOS Y AUTO-LOGIN
            retry_count = 0
            success = False
            items = []

            while retry_count < MAX_RETRIES and not success:
                try:
                    # Siempre pedimos el token más fresco de la memoria
                    current_token = await sicar_auth.get_token()
                    
                    headers = {
                        "Authorization": f"Bearer {current_token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    }

                    response = await client.post(SICAR_LIST_URL, json=payload, headers=headers)
                    
                    if response.status_code == 200:
                        success = True
                        items = response.json()
                        break
                    
                    elif response.status_code == 204:
                        print(f"No hay más productos en Sicar. Offset {offset}. Finalizando sincronización.")
                        has_more_products = False
                        sync_completed_successfully = True
                        success = True
                        break

                    elif response.status_code == 401:
                        print(f"Token expirado en bloque {offset}. Renovando con AWS Lambda...")
                        print(f"Respuesta de Sicar: {response.text}")
                        await sicar_auth.refresh_token()
                        retry_count += 1
                        
                    else:
                        print(f"Sicar falló con {response.status_code} en bloque {offset}. Reintento {retry_count + 1}/{MAX_RETRIES}")
                        print(f"Respuesta de Sicar: {response.text}")
                        print(f"{len(items)} items procesados hasta ahora.")
                        retry_count += 1
                        await asyncio.sleep(2 ** retry_count) # Backoff
                        
                except httpx.RequestError as e:
                    print(f"Error de red en bloque {offset}: {e}. Reintento {retry_count + 1}/{MAX_RETRIES}")
                    retry_count += 1
                    await asyncio.sleep(2 ** retry_count)

            # Si después de X intentos falló, abortamos este ciclo de sincronización para proteger el backend
            if not success:
                print(f"Abortando sincronización. Fallo crítico persistente en el offset {offset}.")
                break 

            if not items:
                has_more_products = False
                break

            # Lógica de Inserción/Actualización en PostgreSQL
            product_values = []
            for p in items:
                prices_obj = p.get("prices") or {}
                

                product_values.append({
                    "sicar_uuid": p.get("uuid"),
                    "sku": p.get("sku", ""),
                    "name": p.get("description", "Sin Nombre"),
                    "image_url": p.get("imageUrl"),
                    "department_uuid": p.get("departmentUuid"),
                    "category_uuid": p.get("categoryUuid"),
                    "is_bulk": p.get("bulk", False),
                    "is_active": not p.get("hidden", False), 
                    "price": prices_obj.get(price_key, 0.0), 
                    "stock": p.get("stock", 0.0),
                    "last_sync_id": current_sync_id
                })
            if product_values:
                stmt = insert(Product)

                update_dict = {c.name: c for c in stmt.excluded if not c.primary_key}
                stmt = stmt.on_conflict_do_update(
                    index_elements=['sicar_uuid'], 
                    set_=update_dict
                )
                
                await db.execute(stmt, product_values)
                await db.commit()

            total_procesados += len(items)
            print(f"Bloque procesado. Total en base de datos local: {total_procesados} productos.")
            
            offset += len(items)
        
    # Fase de limpieza
    if sync_completed_successfully:
        print("Iniciando limpieza de productos eliminados...")
        try:
            # Filtramos por is_deleted == False para no actualizar registros que ya estaban borrados previamente.
            stmt = (
                update(Product)
                .where(Product.last_sync_id != current_sync_id)
                .where(Product.last_sync_id.is_not(None))
                .where(Product.is_deleted == False)
                .values(
                    is_deleted=True,
                    deleted_at=datetime.now(timezone.utc)
                )
            )
            
            result = await db.execute(stmt)
            await db.commit()

            print(f"Limpieza completada. {result.rowcount} productos fueron desactivados.")
                
        except Exception as e:
            await db.rollback()
            print(f"Error de base de datos durante la limpieza: {e}")

"""
Testeo manual de la función de sincronización.
"""
async def run_manual_test():
    print("Iniciando prueba manual de sincronización...")
    async with AsyncSessionLocal() as session:
        await sync_sicar_catalog(session)
    print("Prueba manual finalizada.")

if __name__ == "__main__":
    asyncio.run(run_manual_test())