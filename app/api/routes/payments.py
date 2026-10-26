import logging
from fastapi import APIRouter, BackgroundTasks, Request, status

from app.core.database import DbDep
from app.core.rate_limit import limiter
from app.services import payment_service, chargeback_service
from app.services.order_history_service import get_order_by_uuid, finalize_order_payment, MP_CHARGEBACK_STATUSES, MP_MEDIATION_STATUSES

logger = logging.getLogger(__name__)

# Sin validate_api_key a proposito: Mercado Pago no puede mandar nuestra x-api-key
# estatica. La autenticidad se garantiza con verify_mercadopago_webhook_signature
# (x-signature/x-request-id contra MP_WEBHOOK_SECRET) - ver payment_service.py.
router = APIRouter(prefix="/payments", tags=["Payments (Mercado Pago)"])

@router.post("/webhook", summary="Notificaciones de Mercado Pago", status_code=status.HTTP_200_OK)
@limiter.limit("60/minute")
async def mercado_pago_webhook(request: Request, db: DbDep, background_tasks: BackgroundTasks):
    """
    Notificaciones de la Orders API de Mercado Pago (`type=order`, `data.id=ORD...`): el
    respaldo de los cambios asincronos tras `POST /orders/{id}/pay` (OXXO pagado en tienda,
    tarjeta que sale de revision, reembolsos y contracargos hechos desde el panel de MP).
    Cualquier otra notificacion (p. ej. el evento "Contracargos", cuyo id es un `case_id`)
    se ignora: el contracargo tambien llega como notificacion de la order, con
    `transactions.chargebacks[]`. Responde 200 incluso en no-ops - Mercado Pago reintenta
    agresivamente ante cualquier respuesta que no sea 2xx. Limite generoso (60/min por IP)
    porque esta ruta no tiene x-api-key.
    """
    if not await payment_service.verify_mercadopago_webhook_signature(request):
        logger.warning("Notificacion de Mercado Pago rechazada: firma invalida.")
        return {"status": "invalid signature"}

    topic = request.query_params.get("type") or request.query_params.get("topic")
    resource_id = request.query_params.get("data.id") or request.query_params.get("id")
    if not payment_service.is_mp_order_id(resource_id):
        logger.info(f"Notificacion de Mercado Pago ignorada: no es una order (type={topic}, id={resource_id}).")
        return {"status": "ignored"}

    mp_payment = await payment_service.get_payment(resource_id)
    order_uuid = mp_payment.get("external_reference")
    if not order_uuid:
        logger.warning(f"Notificacion de Mercado Pago sin external_reference (order {resource_id}).")
        return {"status": "ignored"}

    order = await get_order_by_uuid(db, order_uuid)
    if not order:
        logger.warning(f"Notificacion de Mercado Pago para una orden desconocida: {order_uuid} (order {resource_id}).")
        return {"status": "ignored"}

    # CANCELLED no es terminal para un contracargo: una orden PAID puede cancelarse y
    # reembolsarse por otra via (admin, cliente) y el tarjetahabiente disputar el cargo
    # original despues ("disputa tras reembolso", un patron real). finalize_order_payment es
    # idempotente ante reintentos sobre cualquier estado, asi que dejar pasar solo una
    # disputa aqui no reintroduce el riesgo de notificar doble ni de resucitar la orden.
    is_dispute = mp_payment.get("status") in MP_CHARGEBACK_STATUSES | MP_MEDIATION_STATUSES
    if order.status == "CANCELLED" and not is_dispute:
        logger.info(f"Notificacion de Mercado Pago para la orden {order_uuid} ignorada: ya esta en estado terminal ({order.status}).")
        return {"status": "already final"}

    order = await finalize_order_payment(db, order, mp_payment, background_tasks)
    for case_id in mp_payment.get("chargeback_case_ids") or []:
        mp_chargeback = await payment_service.get_chargeback(case_id)
        await chargeback_service.process_chargeback(db, order, mp_chargeback, background_tasks)
    logger.info(f"Orden {order_uuid} finalizada via webhook de Mercado Pago (order {resource_id}).")

    return {"status": "ok"}
