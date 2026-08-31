import logging
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.models.chargeback import Chargeback
from app.services import admin_notification_service

logger = logging.getLogger(__name__)


def _map_coverage_to_status(coverage_applied: bool | None) -> str:
    if coverage_applied is None:
        return "IN_PROCESS"
    return "WON" if coverage_applied else "LOST"


async def process_chargeback_notification(db: AsyncSession, mp_chargeback: dict, background_tasks: BackgroundTasks) -> None:
    """Aplica el detalle de `GET /v1/chargebacks/{id}` (topic "chargebacks" de Mercado
    Pago - debe habilitarse explicitamente en su dashboard, ver CLAUDE.md) a la orden
    local. Camino "enriquecido": a diferencia de la deteccion via topic "payment" en
    `finalize_order_payment` (siempre disponible pero solo sabe que hay un contracargo),
    este trae deadline/elegibilidad y, al resolverse, el resultado real
    (`coverage_applied`).

    Idempotente: se identifica por `mp_chargeback_id`, asi que una notificacion repetida
    (incluida la de resolucion) solo refresca los campos de la fila existente en vez de
    duplicarla. No toca `Order.status` ni `Product.stock`/`reserved` - evento solo
    monetario/de registro, mismo criterio que un reembolso parcial."""
    mp_chargeback_id = str(mp_chargeback.get("id")) if mp_chargeback.get("id") is not None else None
    payments = mp_chargeback.get("payments") or []
    mp_payment_id = str(payments[0]) if payments else None

    if not mp_payment_id:
        logger.warning(f"Notificacion de contracargo sin payments[] (chargeback {mp_chargeback_id}) - no se puede resolver la orden.")
        return

    locked_result = await db.execute(
        select(Order).where(Order.mp_payment_id == mp_payment_id).with_for_update()
    )
    order = locked_result.scalar_one_or_none()
    if not order:
        logger.warning(f"Notificacion de contracargo para un mp_payment_id desconocido: {mp_payment_id} (chargeback {mp_chargeback_id}).")
        return

    chargeback = None
    if mp_chargeback_id:
        chargeback = await db.scalar(select(Chargeback).where(Chargeback.mp_chargeback_id == mp_chargeback_id))

    is_new = chargeback is None
    if is_new:
        chargeback = Chargeback(order_id=order.id, mp_chargeback_id=mp_chargeback_id, mp_payment_id=mp_payment_id)
        db.add(chargeback)

    previous_status = None if is_new else chargeback.status

    amount = mp_chargeback.get("amount")
    if amount is not None:
        chargeback.amount = Decimal(str(amount))
    chargeback.reason = mp_chargeback.get("reason") or chargeback.reason
    # "coverage_elegible" (sin la segunda "i") es el nombre real del campo en la respuesta
    # de Mercado Pago - confirmado en su propia documentacion, no es un typo de este lado.
    if "coverage_elegible" in mp_chargeback:
        chargeback.coverage_eligible = mp_chargeback["coverage_elegible"]
    if "documentation_required" in mp_chargeback:
        chargeback.documentation_required = mp_chargeback["documentation_required"]
    deadline = mp_chargeback.get("date_documentation_deadline")
    if deadline:
        chargeback.documentation_deadline = datetime.fromisoformat(deadline)

    new_status = _map_coverage_to_status(mp_chargeback.get("coverage_applied"))
    chargeback.status = new_status
    if new_status != "IN_PROCESS" and chargeback.resolved_at is None:
        chargeback.resolved_at = datetime.now(timezone.utc)

    if order.disputed_at is None:
        order.disputed_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(order)
    await db.refresh(chargeback)

    if is_new:
        logger.critical(f"Orden {order.uuid} recibio un contracargo (chargeback {mp_chargeback_id}).")
        try:
            await admin_notification_service.notify_admin_chargeback_received(order, background_tasks)
        except Exception as e:
            logger.error(f"Fallo inesperado notificando el contracargo recibido de la orden {order.uuid}: {type(e).__name__}: {e!r}")
    elif previous_status == "IN_PROCESS" and new_status != "IN_PROCESS":
        logger.critical(f"Contracargo resuelto ({new_status}) en la orden {order.uuid}.")
        try:
            await admin_notification_service.notify_admin_chargeback_resolved(order, chargeback, background_tasks)
        except Exception as e:
            logger.error(f"Fallo inesperado notificando la resolucion del contracargo de la orden {order.uuid}: {type(e).__name__}: {e!r}")
