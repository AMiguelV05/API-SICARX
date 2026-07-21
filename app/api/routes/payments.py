import logging
from fastapi import APIRouter, Request, status

from app.core.database import DbDep
from app.services import payment_service
from app.services.order_history_service import get_order_by_uuid, finalize_order_payment

logger = logging.getLogger(__name__)

# Sin dependencies=[Depends(validate_api_key)] a proposito: Mercado Pago no puede mandar
# nuestra x-api-key estatica. La autenticidad de esta ruta se garantiza unicamente con
# verify_webhook_signature (x-signature/x-request-id contra MP_WEBHOOK_SECRET) - ver
# payment_service.py.
router = APIRouter(prefix="/payments", tags=["Payments (Mercado Pago)"])

@router.post("/webhook", summary="Notificaciones de Mercado Pago", status_code=status.HTTP_200_OK)
async def mercado_pago_webhook(request: Request, db: DbDep):
    """
    Unico camino para confirmar pagos hechos con Mercado Pago Wallet: ese metodo
    redirige al comprador directamente al sitio de Mercado Pago y nunca llama a
    `POST /orders/{id}/pay` (ver payment_service.create_preference/wallet_credits.md).
    Tambien sirve como respaldo para tarjeta/OXXO si el estado cambia de forma
    asincrona despues del submit inicial (p. ej. una tarjeta que pasa de en revision a
    aprobada, o un pago OXXO que finalmente se paga en tienda).

    Responde 200 incluso en no-ops (tipo de evento desconocido, orden ya en estado
    terminal) - Mercado Pago reintenta agresivamente ante cualquier respuesta que no
    sea 2xx.
    """
    if not await payment_service.verify_webhook_signature(request):
        logger.warning("Notificacion de Mercado Pago rechazada: firma invalida.")
        return {"status": "invalid signature"}

    payment_id = request.query_params.get("data.id") or request.query_params.get("id")
    if not payment_id:
        try:
            body = await request.json()
        except Exception:
            body = {}
        payment_id = (body.get("data") or {}).get("id")

    if not payment_id:
        logger.info("Notificacion de Mercado Pago ignorada: sin payment id.")
        return {"status": "ignored"}

    mp_payment = await payment_service.get_payment(str(payment_id))
    order_uuid = mp_payment.get("external_reference")
    if not order_uuid:
        logger.warning(f"Notificacion de Mercado Pago sin external_reference (payment {payment_id}).")
        return {"status": "ignored"}

    order = await get_order_by_uuid(db, order_uuid)
    if not order:
        logger.warning(f"Notificacion de Mercado Pago para una orden desconocida: {order_uuid} (payment {payment_id}).")
        return {"status": "ignored"}

    if order.status in ("PAID", "CANCELLED"):
        logger.info(f"Notificacion de Mercado Pago para la orden {order_uuid} ignorada: ya esta en estado terminal ({order.status}).")
        return {"status": "already final"}

    await finalize_order_payment(db, order, mp_payment)
    logger.info(f"Orden {order_uuid} finalizada via webhook de Mercado Pago (payment {payment_id}).")

    return {"status": "ok"}
