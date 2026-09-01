import logging
from fastapi import APIRouter, BackgroundTasks, Request, status

from app.core.database import DbDep
from app.core.rate_limit import limiter
from app.services import payment_service, chargeback_service
from app.services.order_history_service import get_order_by_uuid, finalize_order_payment, MP_CHARGEBACK_STATUSES

logger = logging.getLogger(__name__)

# Sin validate_api_key a proposito: Mercado Pago no puede mandar nuestra x-api-key
# estatica. La autenticidad se garantiza con verify_mercadopago_webhook_signature
# (x-signature/x-request-id contra MP_WEBHOOK_SECRET) - ver payment_service.py.
router = APIRouter(prefix="/payments", tags=["Payments (Mercado Pago)"])

@router.post("/webhook", summary="Notificaciones de Mercado Pago", status_code=status.HTTP_200_OK)
@limiter.limit("60/minute")
async def mercado_pago_webhook(request: Request, db: DbDep, background_tasks: BackgroundTasks):
    """
    Unico camino para confirmar pagos con Mercado Pago Wallet (redirige al comprador y
    nunca llama a `POST /orders/{id}/pay`); tambien respalda cambios asincronos de
    tarjeta/OXXO tras el submit inicial. Responde 200 incluso en no-ops - Mercado Pago
    reintenta agresivamente ante cualquier respuesta que no sea 2xx. Limite generoso
    (60/min por IP) porque esta ruta no tiene x-api-key.
    """
    if not await payment_service.verify_mercadopago_webhook_signature(request):
        logger.warning("Notificacion de Mercado Pago rechazada: firma invalida.")
        return {"status": "invalid signature"}

    topic = request.query_params.get("topic") or request.query_params.get("type")
    resource_id = request.query_params.get("data.id") or request.query_params.get("id")
    if not resource_id:
        try:
            body = await request.json()
        except Exception:
            body = {}
        resource_id = (body.get("data") or {}).get("id")
        topic = topic or body.get("type")

    if not resource_id:
        logger.info("Notificacion de Mercado Pago ignorada: sin id de recurso.")
        return {"status": "ignored"}

    # Un contracargo ("Compra no reconocida") llega en un topic aparte, con un id que NO
    # es un payment id - hay que consultar GET /v1/chargebacks/{id}, no /v1/payments/{id}.
    # Requiere habilitar el topic "Chargebacks" en el dashboard de Mercado Pago (ver
    # CLAUDE.md, "Contracargos de Mercado Pago"); si nunca se habilita, el mismo evento se
    # detecta igual via el topic "payment" de abajo (payment.status == "charged_back").
    if topic == "chargebacks":
        mp_chargeback = await payment_service.get_chargeback(str(resource_id))
        await chargeback_service.process_chargeback_notification(db, mp_chargeback, background_tasks)
        return {"status": "ok"}

    payment_id = resource_id
    mp_payment = await payment_service.get_payment(str(payment_id))
    order_uuid = mp_payment.get("external_reference")
    if not order_uuid:
        logger.warning(f"Notificacion de Mercado Pago sin external_reference (payment {payment_id}).")
        return {"status": "ignored"}

    order = await get_order_by_uuid(db, order_uuid)
    if not order:
        logger.warning(f"Notificacion de Mercado Pago para una orden desconocida: {order_uuid} (payment {payment_id}).")
        return {"status": "ignored"}

    # CANCELLED tampoco es un estado realmente terminal para un contracargo: una orden
    # PAID puede ser cancelada+reembolsada por otra via (admin, cliente) y el cardholder
    # disputar el cargo original despues de todos modos ("disputa tras reembolso", un
    # patron real) - esa notificacion no debe descartarse solo porque la orden ya este
    # CANCELLED. finalize_order_payment ya es idempotente ante reintentos sobre cualquier
    # estado (sus propias ramas revisan order.status/la transicion antes de mutar nada),
    # asi que dejar pasar unicamente un contracargo aqui no reintroduce el riesgo de
    # notificar doble ni de resucitar la orden.
    if order.status == "CANCELLED" and mp_payment.get("status") not in MP_CHARGEBACK_STATUSES:
        logger.info(f"Notificacion de Mercado Pago para la orden {order_uuid} ignorada: ya esta en estado terminal ({order.status}).")
        return {"status": "already final"}

    await finalize_order_payment(db, order, mp_payment, background_tasks)
    logger.info(f"Orden {order_uuid} finalizada via webhook de Mercado Pago (payment {payment_id}).")

    return {"status": "ok"}
