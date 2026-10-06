import httpx
import asyncio
import json
import logging
from decimal import Decimal, ROUND_HALF_UP
from logging.handlers import RotatingFileHandler
from sqlalchemy import select, and_, not_, text
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
from app.core.sicar_headers import bearer_json_headers, graphql_bearer_headers
from app.core.sicar_validation import is_safe_sicar_id
from app.core.retry import request_with_backoff
from app.core.error_tracking import capture_exception, init_error_tracking
from app.worker.sicar_sync_worker import scheduled_sicar_sync_job
from app.worker.abandoned_order_worker import scheduled_abandoned_order_job
from app.worker.search_index_worker import (
    ensure_index_on_startup,
    scheduled_drain_job,
    scheduled_synonyms_job,
)
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

init_error_tracking("worker")

SICAR_LIST_URL = "https://api.sicarx.com/product/v1/product/list"
GRAPHQL_URL = "https://api.sicarx.com/graph/v1/"
PRICE_LIST_ID = settings.SICAR_PRICE_LIST_ID
MAX_RETRIES = 4


async def _fetch_hidden_map(client: httpx.AsyncClient, uuids: list) -> dict:
    safe_uuids = [u for u in uuids if is_safe_sicar_id(u)]
    if not safe_uuids:
        return {}

    query = f"""{{
        products(uuids: {json.dumps(safe_uuids)}, priceListId: {json.dumps(PRICE_LIST_ID)}) {{
            uuid
            hidden
        }}
    }}"""

    async def attempt_fetch(token: str):
        headers = graphql_bearer_headers(token)
        return await client.post(GRAPHQL_URL, content=query, headers=headers)

    try:
        async def call_with_auth():
            return await sicar_auth.request_with_retry(attempt_fetch)

        response = await request_with_backoff(call_with_auth, max_attempts=2, context="Sicar X hidden status en bloque")

        if response.status_code != 200:
            logger.warning(f"No se pudo obtener el estado 'hidden' en bloque (status {response.status_code}). Se asumira visible para este bloque.")
            return {}

        data = response.json()
        if "errors" in data:
            logger.warning(f"Errores GraphQL consultando 'hidden' en bloque: {data['errors']}")
            return {}

        products = data.get("data", {}).get("products") or []
        return {p["uuid"]: bool(p.get("hidden", False)) for p in products if p.get("uuid")}
    except httpx.RequestError as e:
        logger.warning(f"Error de red consultando el estado 'hidden' en bloque: {e}")
        return {}

PRICE_QUANTUM = Decimal("0.01")
STOCK_QUANTUM = Decimal("0.001")

# Campos de Sicar X que deciden si una fila cambio. last_sync_id no entra: cambia en cada
# pasada por definicion.
_COMPARED_FIELDS = (
    "sku", "name", "image_url", "department_uuid", "category_uuid", "is_bulk",
    "is_active", "price", "stock", "is_deleted", "deleted_at",
)


async def _changed_product_values(db: AsyncSession, product_values: list[dict]) -> list[dict]:
    """Filtra la pagina a los productos nuevos o con algun campo distinto al guardado.

    Reescribir las ~87k filas cada 5 minutos, cambiaran o no, generaba ~400 MB de WAL por
    pasada (last_sync_id esta indexado, asi que ninguna era HOT y cada una reescribia todos
    los indices, incluidos los GIN de trigramas). Se compara en Python y no con un WHERE en
    el ON CONFLICT porque Postgres bloquea la fila en conflicto antes de evaluar ese WHERE,
    y ese bloqueo tambien escribe WAL."""
    columns = [getattr(Product, f) for f in _COMPARED_FIELDS]
    result = await db.execute(
        select(Product.sicar_uuid, *columns)
        .where(Product.sicar_uuid.in_([v["sicar_uuid"] for v in product_values]))
    )
    stored = {row.sicar_uuid: row for row in result.all()}

    changed = []
    for values in product_values:
        row = stored.get(values["sicar_uuid"])
        if row is None or any(getattr(row, f) != values[f] for f in _COMPARED_FIELDS):
            changed.append(values)
    return changed


async def _upsert_products(db: AsyncSession, product_values: list[dict]) -> None:
    stmt = insert(Product)

    # Whitelist de campos que vienen de Sicar X y que se pueden actualizar en un conflicto.
    sicar_fields = product_values[0].keys() - {"sicar_uuid"}
    update_dict = {field: getattr(stmt.excluded, field) for field in sicar_fields}
    stmt = stmt.on_conflict_do_update(
        index_elements=['sicar_uuid'],
        set_=update_dict
    )
    await db.execute(stmt, product_values)


async def _mark_missing_as_deleted(db: AsyncSession, seen_uuids: set[str]) -> int:
    """Marca eliminados los productos que Sicar X ya no devolvio en esta pasada. Antes se
    detectaban por last_sync_id, lo que obligaba a reescribir cada fila en cada pasada.

    Anti-join contra unnest() y no `NOT IN (...)`: son ~87k uuids, por encima del limite de
    parametros de asyncpg. last_sync_id IS NOT NULL deja fuera productos que nunca vinieron
    del sync; is_deleted = false evita re-tocar los ya borrados."""
    result = await db.execute(
        text(
            "UPDATE products p SET is_deleted = true, deleted_at = :now "
            "WHERE p.is_deleted = false AND p.last_sync_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM unnest(CAST(:seen AS text[])) AS s(uuid) WHERE s.uuid = p.sicar_uuid)"
        ),
        {"now": datetime.now(timezone.utc), "seen": list(seen_uuids)},
    )
    return result.rowcount


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
    price_key = f"N{PRICE_LIST_ID.split('-')[-1]}"

    current_sync_id = str(uuid4())
    sync_completed_successfully = False
    seen_uuids: set[str] = set()
    total_cambiados = 0

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
                        await asyncio.sleep(2 ** retry_count)

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

            hidden_map = await _fetch_hidden_map(client, [p.get("uuid") for p in items if p.get("uuid")])

            product_values = []
            for p in items:
                prices_obj = p.get("prices") or {}

                if price_key not in prices_obj:
                    logger.warning(
                        f"price_key '{price_key}' no encontrado en prices para producto "
                        f"{p.get('uuid')}. Precio se guardara como 0.00. Prices disponibles: {list(prices_obj.keys())}"
                    )

                # Decimal(str(...)): evita error de representacion binaria de float en la columna Numeric.
                # quantize: redondea igual que Numeric(10,2)/(12,3) al guardar, para que la
                # comparacion de _changed_product_values no vea un cambio que no existe.
                product_values.append({
                    "sicar_uuid": p.get("uuid"),
                    "sku": p.get("sku", ""),
                    "name": p.get("description", "Sin Nombre"),
                    "image_url": p.get("imageUrl"),
                    "department_uuid": p.get("departmentUuid"),
                    "category_uuid": p.get("categoryUuid"),
                    "is_bulk": p.get("bulk", False),
                    "is_active": not hidden_map.get(p.get("uuid"), False),
                    "price": Decimal(str(prices_obj.get(price_key, 0.0))).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP),
                    "stock": Decimal(str(p.get("stock", 0.0))).quantize(STOCK_QUANTUM, rounding=ROUND_HALF_UP),
                    "is_deleted": False,
                    "deleted_at": None,
                    "last_sync_id": current_sync_id
                })
            seen_uuids.update(v["sicar_uuid"] for v in product_values if v["sicar_uuid"])
            if product_values:
                changed = await _changed_product_values(db, product_values)
                if changed:
                    await _upsert_products(db, changed)
                    await db.commit()
                total_cambiados += len(changed)

            total_procesados += len(items)
            logger.debug(f"Bloque procesado. Total en base de datos local: {total_procesados} productos.")
            
            offset += len(items)
        logger.info(f"Sincronizacion finalizada. {total_cambiados} de {total_procesados} productos cambiaron.")

    deactivated_count = 0
    if sync_completed_successfully and not seen_uuids:
        # Un 204 desde la primera pagina marcaria todo el catalogo como eliminado.
        logger.warning("Sicar X no devolvio ningun producto; se omite la limpieza de eliminados.")
    elif sync_completed_successfully:
        logger.info("Iniciando limpieza de productos eliminados")
        try:
            deactivated_count = await _mark_missing_as_deleted(db, seen_uuids)
            await db.commit()

            logger.info(f"Limpieza completada. {deactivated_count} productos fueron desactivados.")

        except Exception as e:
            await db.rollback()
            logger.error(f"Error de base de datos durante la limpieza: {e}")

        # Alerta de deriva: Product.reserved > Product.stock significa que el stock real de
        # Sicar X bajo por una razon ajena a este backend (venta en tienda, otro canal)
        # mientras habia unidades reservadas localmente - ver Product.available_stock y
        # admin_notification_service.notify_admin_stock_drift. Se excluye el caso
        # stock < 0 y reserved <= 0: ahi no hay ninguna reserva local en juego, es un
        # stock negativo que ya viene asi de Sicar X, no una deriva causada por reservas.
        try:
            drift_result = await db.execute(
                select(Product.sicar_uuid, Product.sku, Product.name, Product.stock, Product.reserved)
                .where(
                    Product.reserved > Product.stock,
                    Product.is_deleted == False,
                    Product.is_active == True,
                    not_(and_(Product.stock < 0, Product.reserved <= 0)),
                )
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

    # Indice de busqueda en Typesense (no-op sin TYPESENSE_URL/TYPESENSE_API_KEY) - ver
    # search_index_worker.py. El arranque reconstruye si falta el indice; un Typesense caido
    # no debe impedir que arranquen el sync de catalogo ni el outbox, de ahi el try.
    try:
        await ensure_index_on_startup()
    except Exception as e:
        logger.error(f"No se pudo preparar el indice de busqueda al arrancar: {e!r}")
        capture_exception(e, job="search_index_startup")
    scheduler.add_job(scheduled_drain_job, 'interval', seconds=30, max_instances=1, coalesce=True)
    scheduler.add_job(scheduled_synonyms_job, 'interval', minutes=5, max_instances=1, coalesce=True)
    # Sin reconstruccion programada: ver full_rebuild en search_index_worker.py.
    scheduler.start()

    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler apagado correctamente.")