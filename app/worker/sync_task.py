import httpx
import asyncio
import logging
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from sqlalchemy import update, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert
from uuid import uuid4
from app.core.database import AsyncSessionLocal
from app.models.product import Product, SyncStatus
from app.services import admin_notification_service
# Necesario para que SQLAlchemy resuelva el ForeignKey de Product.variant_group_uuid -
# sin este import falla con "could not find table 'variant_groups'".
from app.models.attribute import VariantGroup  # noqa: F401
from datetime import datetime, timezone
from app.core.config import settings
from app.services.sicar_auth import sicar_auth
from app.core.sicar_headers import bearer_json_headers
from app.core.error_tracking import capture_exception
from app.worker.sicar_sync_worker import scheduled_sicar_sync_job
from app.worker.abandoned_order_worker import scheduled_abandoned_order_job
from apscheduler.schedulers.asyncio import AsyncIOScheduler

handler = RotatingFileHandler(
    "sync.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=3
)

formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
handler.setFormatter(formatter)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[handler, stream_handler]
)

logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.pool").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

SICAR_LIST_URL = "https://api.sicarx.com/product/v1/product/list"
PRICE_LIST_ID = settings.SICAR_PRICE_LIST_ID
MAX_RETRIES = 4

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
    price_key = f"N{PRICE_LIST_ID.split("-")[-1]}"

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

            retry_count = 0
            success = False
            items = []

            while retry_count < MAX_RETRIES and not success:
                try:
                    current_token = await sicar_auth.get_token()
                    
                    headers = bearer_json_headers(current_token)

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

            if not success:
                logger.error(f"Abortando sincronizacion. Fallo critico persistente en el offset {offset}.")
                break 

            if not items:
                has_more_products = False
                break

            product_values = []
            for p in items:
                prices_obj = p.get("prices") or {}

                if price_key not in prices_obj:
                    logger.warning(
                        f"price_key '{price_key}' no encontrado en prices para producto "
                        f"{p.get('uuid')}. Precio se guardara como 0.00. Prices disponibles: {list(prices_obj.keys())}"
                    )

                # Decimal(str(...)): evita error de representacion binaria de float en la columna Numeric.
                product_values.append({
                    "sicar_uuid": p.get("uuid"),
                    "sku": p.get("sku", ""),
                    "name": p.get("description", "Sin Nombre"),
                    "image_url": p.get("imageUrl"),
                    "department_uuid": p.get("departmentUuid"),
                    "category_uuid": p.get("categoryUuid"),
                    "is_bulk": p.get("bulk", False),
                    "is_active": not p.get("hidden", False),
                    "price": Decimal(str(prices_obj.get(price_key, 0.0))),
                    "stock": Decimal(str(p.get("stock", 0.0))),
                    "is_deleted": False,
                    "deleted_at": None,
                    "last_sync_id": current_sync_id
                })
            if product_values:
                stmt = insert(Product)

                # Whitelist de campos que vienen de Sicar X y que se pueden actualizar en un conflicto.
                sicar_fields = product_values[0].keys() - {"sicar_uuid"}
                update_dict = {field: getattr(stmt.excluded, field) for field in sicar_fields}
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
        
    deactivated_count = 0
    if sync_completed_successfully:
        logger.info("Iniciando limpieza de productos eliminados")
        try:
            # is_deleted == False evita re-tocar registros ya borrados previamente.
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
            deactivated_count = result.rowcount

            logger.info(f"Limpieza completada. {result.rowcount} productos fueron desactivados.")

        except Exception as e:
            await db.rollback()
            logger.error(f"Error de base de datos durante la limpieza: {e}")

        # Alerta de deriva: Product.reserved > Product.stock significa que el stock real de
        # Sicar X bajo por una razon ajena a este backend (venta en tienda, otro canal)
        # mientras habia unidades reservadas localmente - ver Product.available_stock y
        # admin_notification_service.notify_admin_stock_drift.
        try:
            drift_result = await db.execute(
                select(Product.sicar_uuid, Product.sku, Product.name, Product.stock, Product.reserved)
                .where(Product.reserved > Product.stock, Product.is_deleted == False, Product.is_active == True)
            )
            drift_rows = drift_result.all()
            if drift_rows:
                drift_products = [
                    {
                        "sicarUuid": row.sicar_uuid,
                        "sku": row.sku,
                        "name": row.name,
                        "stock": float(row.stock),
                        "reserved": float(row.reserved),
                    }
                    for row in drift_rows
                ]
                logger.warning(f"Deriva de stock detectada: {len(drift_products)} producto(s) con reserved > stock.")
                await admin_notification_service.notify_admin_stock_drift(drift_products)
        except Exception as e:
            logger.error(f"Error verificando deriva de stock (reserved > stock): {e}")

    return total_procesados, deactivated_count, sync_completed_successfully

async def _record_sync_start() -> None:
    """Sesion propia y corta (ver GET /v1/admin/sync/catalog-status), separada de la
    sesion larga de sync_sicar_catalog para que un fallo aqui no afecte la sincronizacion real."""
    async with AsyncSessionLocal() as session:
        stmt = insert(SyncStatus).values(id=1, last_run_started_at=datetime.now(timezone.utc))
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={"last_run_started_at": stmt.excluded.last_run_started_at},
        )
        await session.execute(stmt)
        await session.commit()

async def _record_sync_result(*, success: bool, products_processed: int | None, products_deactivated: int | None, error: str | None) -> None:
    async with AsyncSessionLocal() as session:
        now = datetime.now(timezone.utc)
        values = {
            "id": 1,
            "last_run_finished_at": now,
            "products_processed": products_processed,
            "products_deactivated": products_deactivated,
            "last_error": error,
        }
        update_cols = {
            "last_run_finished_at": None,
            "products_processed": None,
            "products_deactivated": None,
            "last_error": None,
        }
        if success:
            values["last_success_at"] = now
            update_cols["last_success_at"] = None
        stmt = insert(SyncStatus).values(**values)
        update_cols = {k: getattr(stmt.excluded, k) for k in update_cols}
        stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_cols)
        await session.execute(stmt)
        await session.commit()

async def scheduled_job():
    try:
        await _record_sync_start()
        async with AsyncSessionLocal() as session:
            processed, deactivated, completed = await sync_sicar_catalog(session)
        await _record_sync_result(success=completed, products_processed=processed, products_deactivated=deactivated, error=None)
    except Exception as e:
        logger.error(f"Fallo en la tarea programada: {e}")
        capture_exception(e)
        try:
            await _record_sync_result(success=False, products_processed=None, products_deactivated=None, error=str(e)[:2000])
        except Exception as inner_e:
            logger.error(f"Fallo tambien al registrar el error del sync en SyncStatus: {inner_e}")

async def main():
    scheduler = AsyncIOScheduler()

    scheduler.add_job(scheduled_job, 'interval', minutes=5, max_instances=1, coalesce=True, next_run_time=datetime.now())
    # Drena sicar_sync_outbox; cadencia mas corta que el sync de catalogo por ser sensible al tiempo.
    scheduler.add_job(scheduled_sicar_sync_job, 'interval', minutes=1, max_instances=1, coalesce=True, next_run_time=datetime.now())
    # Cancela ordenes TO_PAY abandonadas (sin ningun intento de pago) y libera su reserva de
    # stock - 5 min es de sobra de granularidad contra un timeout de 30 min por defecto.
    scheduler.add_job(scheduled_abandoned_order_job, 'interval', minutes=5, max_instances=1, coalesce=True, next_run_time=datetime.now())
    scheduler.start()

    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler apagado correctamente.")