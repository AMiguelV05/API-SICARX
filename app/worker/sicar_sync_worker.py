import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.order import Order, SicarSyncOutbox
# Order.client_account = relationship("ClientAccount", ...) es una referencia por string:
# necesita que la clase ClientAccount este importada en este proceso antes de que
# SQLAlchemy configure el mapper de Order, o falla con "failed to locate a name
# ('ClientAccount')". El proceso api lo obtiene gratis via sus rutas de auth/addresses;
# el worker no importa nada de app.models.client, asi que se agrega aqui explicitamente.
from app.models.client import ClientAccount  # noqa: F401
from app.services.cancel_service import process_order_cancellation, advance_dispatch_status
from app.services import admin_notification_service

# dispatchStatus al que "aceptar" (ACCEPT) avanza un pedido - PENDING_ACCEPTANCE -> PENDING,
# confirmado en vivo (ver cancel_service.advance_dispatch_status). El mismo endpoint de
# Sicar X acepta cualquier valor de la secuencia PENDING/PREPARING/COMPLETE/DISPATCHED, pero
# la accion ACCEPT del outbox solo cubre este primer avance por ahora.
ACCEPT_TARGET_DISPATCH_STATUS = "PENDING"

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
MAX_ROWS_PER_TICK = 25
STALE_LEASE_MINUTES = 5

async def _claim_next_row(session: AsyncSession) -> SicarSyncOutbox | None:
    """Reclama una fila en una unica sentencia atomica (UPDATE ... WHERE id = (SELECT
    ... FOR UPDATE SKIP LOCKED) ... RETURNING) y hace commit de inmediato - el lock de
    Postgres se sostiene por milisegundos, nunca durante la llamada HTTP a Sicar X que
    viene despues en _process_claimed_row (mantener una fila bloqueada durante una
    llamada de red de duracion desconocida es exactamente el anti-patron que este
    diseño evita). La rama IN_PROGRESS-y-vieja es un rescate de "lease": si el worker
    se cae a medio procesar (p. ej. un reinicio de contenedor), la fila no queda
    huerfana para siempre - vuelve a ser reclamable pasados STALE_LEASE_MINUTES."""
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
    """Carga la fila reclamada y su orden en una sesion nueva, llama a Sicar X (sin
    ningun lock de Postgres sostenido durante esa llamada) y registra el resultado en
    un unico commit al final - tanto el nuevo status/attempts de la fila de outbox
    como cualquier `order.sicar_document_uuid` recien resuelto por
    cancel_service.process_order_cancellation se persisten juntos, sin necesitar un
    rollback intermedio (una excepcion de la llamada HTTP no deja nada pendiente sucio
    en la sesion - solo mutaciones de atributos en memoria, validas de commitear)."""
    async with AsyncSessionLocal() as session:
        row = await session.get(SicarSyncOutbox, row_id)
        if row is None or row.status != "IN_PROGRESS":
            return

        # Deliberadamente SIN filtro de deleted_at: el worker debe poder seguir
        # avisandole a Sicar X de ordenes ya borradas (soft-delete) por el cliente via
        # DELETE /orders/{id}.
        order = await session.get(Order, row.order_id)
        if order is None:
            logger.error(f"sicar_sync_outbox {row.id} referencia una orden inexistente (order_id={row.order_id}) - marcando FAILED.")
            row.status = "FAILED"
            row.last_error = "La orden asociada ya no existe."
            await session.commit()
            return

        try:
            if row.action == "CANCEL":
                await process_order_cancellation(order, row.cash_register_uuid)
            elif row.action == "ACCEPT":
                # Mutacion real capturada en vivo (agent-browser contra el tablero de
                # despacho de app.sicarx.com, 2026-07-29) - ver
                # cancel_service.advance_dispatch_status. A diferencia de CANCEL, esta
                # mutacion usa order.sicar_order_id directamente (no requiere resolver
                # sicar_document_uuid primero).
                await advance_dispatch_status(order, ACCEPT_TARGET_DISPATCH_STATUS)
                order.dispatch_status = ACCEPT_TARGET_DISPATCH_STATUS
            elif row.action == "DISPATCH":
                # Guia de envio generada con envia.com (ver admin_service.generate_shipping_label)
                # - a diferencia de ACCEPT, dispatch_status ya se puso en "DISPATCHED"
                # localmente de forma sincrona en el momento en que envia.com respondio
                # exitosamente (el hecho real ya ocurrio, no es un simple espejo de Sicar
                # X), asi que aqui solo se le avisa a Sicar X - no se vuelve a tocar
                # order.dispatch_status, mismo patron que CANCEL arriba.
                await advance_dispatch_status(order, "DISPATCHED")
            else:
                raise ValueError(f"Accion de sincronizacion desconocida: {row.action!r}")

            row.status = "SUCCEEDED"
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
            else:
                row.status = "PENDING"
                # Backoff exponencial: 1, 2, 4, 8, 16 minutos.
                row.next_attempt_at = datetime.now(timezone.utc) + timedelta(minutes=2 ** (row.attempts - 1))
            await session.commit()

            if row.status == "FAILED":
                await admin_notification_service.notify_admin_sicar_sync_failed(order, row.last_error)

async def sync_pending_cancellations() -> None:
    """Drena sicar_sync_outbox hasta MAX_ROWS_PER_TICK filas o hasta que ya no quede
    ninguna reclamable, la que ocurra primero. Cada fila usa sus propias transacciones
    cortas (ver _claim_next_row/_process_claimed_row) en vez de una sola larga que
    abarque toda la corrida."""
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
