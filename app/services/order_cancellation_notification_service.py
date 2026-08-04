import json
import logging
import time

import httpx

from app.core.config import settings
from app.models.order import Order
from app.schemas.orders import OrderPublic
from app.core.webhook_signing import sign_hmac_sha256
from app.services import admin_notification_service

ORDER_CANCELLED_WEBHOOK_PATH = "/api/webhooks/order-cancelled"
WEBHOOK_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

logger = logging.getLogger(__name__)

async def notify_order_cancelled(order: Order) -> None:
    """Unico punto que deben llamar los 3 call sites de cancelacion; notifica frontend+admin. No fatal, sin reintento."""
    await _notify_frontend(order)
    await admin_notification_service.notify_admin_order_cancelled(order)

async def _notify_frontend(order: Order) -> None:
    try:
        client_account = await order.awaitable_attrs.client_account
        contact_email = ((order.delivery_info or {}).get("contactInfo") or {}).get("email")
        client_email = contact_email or (client_account.email if client_account else None)
        if not client_email:
            logger.warning(f"Orden {order.uuid}: no se pudo resolver el email del cliente, se omite la notificacion de cancelacion al frontend.")
            return

        body = OrderPublic.model_validate(order).model_dump(by_alias=True, mode="json")
        body["clientEmail"] = client_email
        body["clientName"] = client_account.name if client_account else None

        raw_body = json.dumps(body, separators=(",", ":")).encode()
        ts = str(int(time.time()))
        signature = sign_hmac_sha256(settings.FRONTEND_WEBHOOK_SECRET, f"{ts}.".encode() + raw_body)

        url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}{ORDER_CANCELLED_WEBHOOK_PATH}"
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
            logger.error(f"Frontend rechazo la notificacion de pedido cancelado para la orden {order.uuid}: {response.status_code} - {response.text}")
            return
        logger.info(f"Notificacion de pedido cancelado enviada al frontend para la orden {order.uuid}.")
    except Exception as e:
        logger.error(f"Error inesperado notificando al frontend sobre la cancelacion de la orden {order.uuid}: {type(e).__name__}: {e!r}")
