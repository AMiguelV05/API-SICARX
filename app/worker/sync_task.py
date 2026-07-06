import httpx
import asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert
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

    synced_uuids = set()
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
                        await asyncio.sleep(2 * retry_count) # Backoff
                        
                except httpx.RequestError as e:
                    print(f"Error de red en bloque {offset}: {e}. Reintento {retry_count + 1}/{MAX_RETRIES}")
                    retry_count += 1
                    await asyncio.sleep(2 * retry_count)

            # Si después de X intentos falló, abortamos este ciclo de sincronización para proteger el backend
            if not success:
                print(f"Abortando sincronización. Fallo crítico persistente en el offset {offset}.")
                break 

            if not items:
                has_more_products = False

            # Lógica de Inserción/Actualización en PostgreSQL
            for p in items:
                prices_obj = p.get("prices") or {}
                product_uuid = p.get("uuid")
                if product_uuid:
                    synced_uuids.add(product_uuid)

                values = {
                    "sicar_uuid": product_uuid,
                    "sku": p.get("sku", ""),
                    "name": p.get("description", "Sin Nombre"),
                    "image_url": p.get("imageUrl"),
                    "department_uuid": p.get("departmentUuid"),
                    "category_uuid": p.get("categoryUuid"),
                    "is_bulk": p.get("bulk", False),
                    "is_active": not p.get("hidden", False), 
                    "price": prices_obj.get(price_key, 0.0), 
                    "stock": p.get("stock", 0.0)
                }

                stmt = insert(Product).values(**values)
                update_dict = {c.name: c for c in stmt.excluded if not c.primary_key}
                
                stmt = stmt.on_conflict_do_update(
                    index_elements=['sicar_uuid'], 
                    set_=update_dict
                )
                
                await db.execute(stmt)

            await db.commit()
            total_procesados += len(items)
            print(f"Bloque procesado. Total en base de datos local: {total_procesados} productos.")
            
            offset += len(items)
        
    # Fase de limpieza
    if sync_completed_successfully:
        print(f"Iniciando barrido de productos eliminados en Sicar...")
        try:
            if synced_uuids:
                # Le pedimos a Postgres solo los UUIDs que localmente están activos
                result = await db.execute(select(Product.sicar_uuid).where(Product.is_deleted == False))
                local_undeleted_uuids = set(result.scalars().all())

                # Productos que están en la base de datos local pero no en Sicar
                uuids_to_deactivate = list(local_undeleted_uuids - synced_uuids)
                print(f"{len(synced_uuids)} productos activos en Sicar. {len(local_undeleted_uuids)} productos activos en la base de datos local.")

                if uuids_to_deactivate:
                    print(f"Se encontraron {len(uuids_to_deactivate)} productos fantasma. Desactivando en lotes...")

                    # Mandamos la actualización por lotes para no romper el límite de Postgres
                    batch_size = 10000
                    for i in range(0, len(uuids_to_deactivate), batch_size):
                        batch = uuids_to_deactivate[i:i + batch_size]
                        
                        stmt = (
                            update(Product)
                            .where(Product.sicar_uuid.in_(batch))
                            .values(is_deleted=True,
                                    deleted_at=datetime.now(timezone.utc))
                        )
                        await db.execute(stmt)
                        
                    await db.commit()
                    print(f"Limpieza completada. {len(uuids_to_deactivate)} productos fueron desactivados.")
                else:
                    print("El catálogo local ya está idéntico al de Sicar. No hay basura que limpiar.")
            else:
                print("Advertencia: Catálogo de Sicar vacío. Se aborta la limpieza por seguridad.")
                
        except Exception as e:
            await db.rollback()
            print(f"Error de base de datos durante la limpieza: {e}")
            
    else:
        print("La sincronización no terminó correctamente. Se omite la limpieza para evitar falsos borrados.")


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