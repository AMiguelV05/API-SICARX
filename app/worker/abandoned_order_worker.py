import logging
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.order import Order
# Necesario para que SQLAlchemy resuelva Order.client_account (relationship por string) -
# sin este import falla con "failed to locate a name ('ClientAccount')" (mismo motivo que
# en sicar_sync_worker.py).
from app.models.client import ClientAccount  # noqa: F401
from app.services.order_history_service import prepare_local_cancellation
from app.services import admin_notification_service

logger = logging.getLogger(__name__)

MAX_ROWS_PER_TICK = 100

async def _find_abandoned_order_ids(session: AsyncSession, cutoff: datetime, limit: int) -> list[int]:
    """`mp_payment_id IS NULL` es la senal real de "abandonado" - una orden TO_PAY con un
    pago en curso (ticket OXXO, tarjeta en revision) tambien queda TO_PAY por horas/dias,
    pero eso es un pago lento, no una orden abandonada, y no debe tocarse aqui.
    `accepted_at IS NULL` es defensa en profundidad: hoy solo se acepta una orden ya PAID
    (ver CLAUDE.md), asi que una TO_PAY nunca deberia tener accepted_at poblado."""
    result = await session.execute(
        select(Order.id)
        .where(
            Order.status == "TO_PAY",
            Order.deleted_at.is_(None),
            Order.accepted_at.is_(None),
            Order.mp_payment_id.is_(None),
            Order.created_at < cutoff,
        )
        .order_by(Order.created_at)
        .limit(limit)
    )
    return [row[0] for row in result.all()]

async def _cancel_abandoned_order(order_id: int) -> None:
    """Sesion nueva por orden, mismo motivo que sicar_sync_worker.py::_process_claimed_row -
    una fila problematica no debe sostener una transaccion/lock abierto sobre el resto del lote."""
    async with AsyncSessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return

        order.cancellation_reason = (
            f"Cancelado automáticamente: la orden quedó reservada sin ningún intento de "
            f"pago durante más de {settings.ABANDONED_ORDER_TIMEOUT_MINUTES} minutos."
        )
        try:
            order = await prepare_local_cancellation(session, order, require_status="TO_PAY")
            await session.commit()
        except HTTPException:
            # Otra solicitud (el cliente pago, o ya la cancelo manualmente) gano la carrera
            # entre el SELECT candidato y el lock de esta orden - no es un error, se omite.
            await session.rollback()
            logger.info(f"Orden {order_id}: ya no estaba TO_PAY al momento de cancelarla (carrera con otra solicitud), se omite del barrido de abandonadas.")
            return
        except Exception as e:
            await session.rollback()
            logger.error(f"Error inesperado cancelando la orden abandonada {order_id}: {type(e).__name__}: {e!r}")
            return

        logger.info(f"Orden {order.uuid} (sicar_order_id={order.sicar_order_id}) cancelada automáticamente por abandono de checkout (TO_PAY sin intento de pago).")

        try:
            background_tasks = BackgroundTasks()
            await admin_notification_service.notify_admin_order_cancelled(order, background_tasks)
            await background_tasks()
        except Exception as e:
            logger.error(f"Fallo notificando al dashboard admin la cancelación automática de la orden {order.uuid}: {type(e).__name__}: {e!r}")

async def sweep_abandoned_orders() -> None:
    """Drena hasta MAX_ROWS_PER_TICK ordenes TO_PAY abandonadas, cada una en su propia
    transaccion corta - mismo patron que sicar_sync_worker.py::sync_pending_cancellations."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.ABANDONED_ORDER_TIMEOUT_MINUTES)
    async with AsyncSessionLocal() as session:
        order_ids = await _find_abandoned_order_ids(session, cutoff, MAX_ROWS_PER_TICK)
    for order_id in order_ids:
        await _cancel_abandoned_order(order_id)

async def scheduled_abandoned_order_job() -> None:
    try:
        await sweep_abandoned_orders()
    except Exception as e:
        logger.error(f"Fallo en la tarea programada de cancelación de órdenes abandonadas: {e}")
