import httpx
import asyncio
import logging
import asyncio
from logging.handlers import RotatingFileHandler
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert
from uuid import uuid4
from app.core.database import AsyncSessionLocal
from app.models.product import Product
from datetime import datetime, timezone
from app.core.config import settings
from app.services.sicar_auth import sicar_auth
from apscheduler.schedulers.asyncio import AsyncIOScheduler

handler = RotatingFileHandler(
    "sync.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=5
)

formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    datefmt="%Y-%m-%d %H:%M:%S"
)
handler.setFormatter(formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[handler]
)

logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.pool").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

SICAR_LIST_URL = "https://api.sicarx.com/product/v1/product/list"
PRICE_LIST_ID = settings.SICAR_PRICE_LIST_ID
MAX_RETRIES = 5

async def sync_sicar_catalog(db: AsyncSession, offset: int = 0):
    items_per_page = 300
    total_procesados = 0
    has_more_products = True
    timeout = httpx.Timeout(
        connect=5.0,
        read=30.0,
        write=5.0,
        pool=5.0
    )
    logger.debug("Iniciando sincronizacion paginada con Sicar X")
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
                        logger.info(f"No hay mas productos en Sicar. Offset {offset}. Finalizando sincronizacion.")
                        has_more_products = False
                        sync_completed_successfully = True
                        success = True
                        break

                    elif response.status_code == 401:
                        logger.warning(f"Token expirado en bloque {offset}. Renovando con AWS Lambda...")
                        logger.debug(f"Respuesta de Sicar: {response.text}")
                        try:
                            await sicar_auth.refresh_token()
                        except Exception as e:
                            logger.exception(e)
                        retry_count += 1
                        
                    else:
                        logger.warning(f"Sicar fallo con {response.status_code} en bloque {offset}. Reintento {retry_count + 1}/{MAX_RETRIES}")
                        logger.debug(f"Respuesta de Sicar: {response.text}")
                        logger.debug(f"{len(items)} items procesados hasta ahora.")
                        retry_count += 1
                        await asyncio.sleep(2 ** retry_count)
                        
                except httpx.RequestError as e:
                    logger.error(f"Error de red en bloque {offset}: {e}. Reintento {retry_count + 1}/{MAX_RETRIES}")
                    retry_count += 1
                    await asyncio.sleep(2 ** retry_count)

            # Si después de X intentos falló, abortamos este ciclo de sincronización
            if not success:
                logger.error(f"Abortando sincronizacion. Fallo critico persistente en el offset {offset}.")
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
                    "is_deleted": False,
                    "deleted_at": None,
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
            logger.debug(f"Bloque procesado. Total en base de datos local: {total_procesados} productos.")
            
            offset += len(items)
        logger.info(f"Sincronizacion finalizada")
        
    # Fase de limpieza
    if sync_completed_successfully:
        logger.info("Iniciando limpieza de productos eliminados")
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

            logger.info(f"Limpieza completada. {result.rowcount} productos fueron desactivados.")
                
        except Exception as e:
            await db.rollback()
            logger.error(f"Error de base de datos durante la limpieza: {e}")

async def scheduled_job():
    try:
        async with AsyncSessionLocal() as session:
            await sync_sicar_catalog(session)
    except Exception as e:
        logger.error(f"Fallo en la tarea programada: {e}")

async def main():
    scheduler = AsyncIOScheduler()
    
    scheduler.add_job(scheduled_job, 'interval', minutes=5, max_instances=1, coalesce=True, next_run_time=datetime.now())
    scheduler.start()
    
    # Mantiene el hilo principal vivo
    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler apagado correctamente.")