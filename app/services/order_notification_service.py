import json
import logging
import time

import httpx

from app.core.config import settings
from app.models.order import Order
from app.schemas.orders import OrderPublic
from app.core.webhook_signing import sign_hmac_sha256

ORDER_CONFIRMED_WEBHOOK_PATH = "/api/webhooks/order-confirmed"
WEBHOOK_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

logger = logging.getLogger(__name__)

async def notify_order_confirmed(order: Order) -> None:
    """Avisa al frontend (via webhook firmado) que un pedido paso a PAID, para que el
    frontend envie el correo de confirmacion con su propio template de react-email y su
    propia cuenta de Resend - ver FRONTEND_INTEGRATION.md para el contrato completo. No
    fatal si falla - mismo patron que create_preference en payment_service.py: una
    notificacion que no llega no debe bloquear ni revertir el pago ya aplicado en
    finalize_order_payment (unico llamador). A diferencia de create_preference, no hay
    reintento automatico si esto falla - ver CLAUDE.md.

    Esta firma (headers X-Webhook-*) es un esquema propio de este backend, sin relacion
    con el esquema de Mercado Pago (x-signature/x-request-id, ver
    payment_service.verify_mercadopago_webhook_signature) - no confundir ambos."""
    try:
        client_account = await order.awaitable_attrs.client_account
        contact_email = ((order.delivery_info or {}).get("contactInfo") or {}).get("email")
        client_email = contact_email or (client_account.email if client_account else None)
        if not client_email:
            logger.warning(f"Orden {order.uuid}: no se pudo resolver el email del cliente, se omite la notificacion al frontend.")
            return

        body = OrderPublic.model_validate(order).model_dump(by_alias=True, mode="json")
        body["clientEmail"] = client_email
        body["clientName"] = client_account.name if client_account else None

        raw_body = json.dumps(body, separators=(",", ":")).encode()
        ts = str(int(time.time()))
        signature = sign_hmac_sha256(settings.FRONTEND_WEBHOOK_SECRET, f"{ts}.".encode() + raw_body)

        url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}{ORDER_CONFIRMED_WEBHOOK_PATH}"
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as client:
            response = await client.post(
                url,
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Timestamp": ts,
                    "X-Webhook-Signature": signature,
                },
            )
        if response.status_code not in (200, 201, 202):
            logger.error(f"Frontend rechazo la notificacion de pedido confirmado para la orden {order.uuid}: {response.status_code} - {response.text}")
            return
        logger.info(f"Notificacion de pedido confirmado enviada al frontend para la orden {order.uuid}.")
    except Exception as e:
        logger.error(f"Error inesperado notificando al frontend sobre la orden {order.uuid}: {type(e).__name__}: {e!r}")
