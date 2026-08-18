import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.order import Order, SicarSyncOutbox
# Necesario para que SQLAlchemy resuelva Order.client_account (relationship por string) -
# sin este import falla con "failed to locate a name ('ClientAccount')".
from app.models.client import ClientAccount  # noqa: F401
from app.services.sicar_stock_service import apply_order_stock_delta
from app.services.product_stock_service import apply_stock_deltas, apply_reserved_deltas
from app.services.order_service import _to_decimal
from app.services import admin_notification_service
from app.core.error_tracking import capture_exception

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
MAX_ROWS_PER_TICK = 25
STALE_LEASE_MINUTES = 5

async def _claim_next_row(session: AsyncSession) -> SicarSyncOutbox | None:
    """Claim atomico (UPDATE ... SELECT ... FOR UPDATE SKIP LOCKED ... RETURNING), commit
    inmediato - el lock nunca se sostiene durante la llamada HTTP a Sicar X posterior.
    Filas IN_PROGRESS abandonadas (worker caido) vuelven a ser reclamables tras STALE_LEASE_MINUTES."""
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(minutes=STALE_LEASE_MINUTES)

    claimable = (
        select(SicarSyncOutbox.id)
        .where(
            or_(
                and_(SicarSyncOutbox.status == "PENDING", SicarSyncOutbox.next_attempt_at <= now),
                and_(SicarSyncOutbox.status == "IN_PROGRESS", SicarSyncOutbox.updated_at < stale_before),
            )
        )
        .order_by(SicarSyncOutbox.next_attempt_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    stmt = (
        update(SicarSyncOutbox)
        .where(SicarSyncOutbox.id == claimable.scalar_subquery())
        .values(status="IN_PROGRESS")
        .returning(SicarSyncOutbox)
    )
    result = await session.execute(stmt)
    row = result.scalars().first()
    await session.commit()
    return row

async def _process_claimed_row(row_id: int) -> None:
    """Sesion nueva por fila: llama a Sicar X sin lock de Postgres sostenido y persiste
    el resultado (status/attempts) en un solo commit al final."""
    async with AsyncSessionLocal() as session:
        row = await session.get(SicarSyncOutbox, row_id)
        if row is None or row.status != "IN_PROGRESS":
            return

        # Sin filtro de deleted_at: debe poder avisarle a Sicar X de ordenes ya soft-deleted.
        order = await session.get(Order, row.order_id)
        if order is None:
            logger.error(f"sicar_sync_outbox {row.id} referencia una orden inexistente (order_id={row.order_id}) - marcando FAILED.")
            row.status = "FAILED"
            row.last_error = "La orden asociada ya no existe."
            await session.commit()
            return

        try:
            item_deltas = [(item.get("uuid"), _to_decimal(item.get("quantity", 0))) for item in (order.items or [])]

            if row.action == "ACCEPT":
                # Unico punto donde este backend le avisa algo a Sicar X: descuento de inventario.
                await apply_order_stock_delta(order.items, order.branch_id, sign=-1)
                # Espejo local, en el mismo commit que SUCCEEDED (ver comentario de atomicidad
                # abajo): descuenta Product.stock de inmediato (en vez de esperar el proximo
                # sync de 5 min) y libera el hold en Product.reserved - la reserva ya quedo
                # materializada permanentemente en el stock real de Sicar X.
                await apply_stock_deltas(session, [(uuid, -qty) for uuid, qty in item_deltas])
                await apply_reserved_deltas(session, [(uuid, -qty) for uuid, qty in item_deltas])
            elif row.action == "CANCEL":
                # Reversion del descuento; solo se encola si la orden ya habia sido aceptada.
                await apply_order_stock_delta(order.items, order.branch_id, sign=1)
                # Espejo local de la restauracion - reserved no se toca aqui, ya se libero al
                # aceptar (rama ACCEPT arriba).
                await apply_stock_deltas(session, item_deltas)
            else:
                raise ValueError(f"Accion de sincronizacion desconocida: {row.action!r}")

            row.status = "SUCCEEDED"
            # Atomico junto con el espejo local de stock/reserved arriba: si el proceso
            # muere antes de este commit, la fila sigue IN_PROGRESS y se vuelve a reclamar
            # tras STALE_LEASE_MINUTES, reintentando la llamada a Sicar X y el espejo local
            # juntos - nunca queda uno aplicado sin el otro.
            await session.commit()
            logger.info(f"Sincronizacion con Sicar X exitosa para la orden {order.uuid} (sicar_sync_outbox {row.id}, accion={row.action}).")
        except Exception as e:
            row.attempts += 1
            row.last_error = str(e)[:2000]
            if row.attempts >= MAX_ATTEMPTS:
                row.status = "FAILED"
                logger.critical(
                    f"Sincronizacion con Sicar X agotada para la orden {order.uuid} "
                    f"(sicar_sync_outbox {row.id}, accion={row.action}) tras {row.attempts} intentos: {e}"
                )
                # Ademas del webhook admin de abajo (senal especifica de este dominio) -
                # ver error_tracking.py.
                capture_exception(e, order_uuid=order.uuid, outbox_id=row.id, action=row.action, attempts=row.attempts)
            else:
                row.status = "PENDING"
                # Backoff exponencial: 1, 2, 4, 8, 16 minutos.
                row.next_attempt_at = datetime.now(timezone.utc) + timedelta(minutes=2 ** (row.attempts - 1))
            await session.commit()

            if row.status == "FAILED":
                await admin_notification_service.notify_admin_sicar_sync_failed(order, row.last_error)

async def sync_pending_cancellations() -> None:
    """Drena hasta MAX_ROWS_PER_TICK filas de sicar_sync_outbox, cada una en su propia
    transaccion corta en vez de una sola larga que abarque toda la corrida."""
    for _ in range(MAX_ROWS_PER_TICK):
        async with AsyncSessionLocal() as session:
            row = await _claim_next_row(session)
        if row is None:
            break
        await _process_claimed_row(row.id)

async def scheduled_sicar_sync_job() -> None:
    try:
        await sync_pending_cancellations()
    except Exception as e:
        logger.error(f"Fallo en la tarea programada de sincronizacion con Sicar X: {e}")
        capture_exception(e)
