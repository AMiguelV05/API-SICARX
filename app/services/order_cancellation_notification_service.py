import json
import logging
import time

import httpx
from fastapi import BackgroundTasks

from app.core.config import settings
from app.models.order import Order
from app.schemas.orders import OrderPublic
from app.core.webhook_signing import sign_hmac_sha256
from app.services import admin_notification_service
from app.services.order_display_service import resolve_client_name

ORDER_CANCELLED_WEBHOOK_PATH = "/api/webhooks/order-cancelled"
WEBHOOK_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

logger = logging.getLogger(__name__)

async def _send_webhook(url: str, raw_body: bytes, headers: dict, log_context: str) -> None:
    """Nucleo HTTP puro (sin BD) - corre via BackgroundTasks, mismo motivo que
    order_notification_service._send_webhook."""
    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as client:
            response = await client.post(url, content=raw_body, headers=headers)
        if response.status_code not in (200, 201, 202):
            logger.error(f"{log_context}: rechazado por el frontend: {response.status_code} - {response.text}")
            return
        logger.info(f"{log_context}: enviado al frontend.")
    except Exception as e:
        logger.error(f"{log_context}: error inesperado enviando el webhook: {type(e).__name__}: {e!r}")

async def notify_order_cancelled(order: Order, background_tasks: BackgroundTasks) -> None:
    """Unico punto que deben llamar los 3 call sites de cancelacion; notifica frontend+admin.
    No fatal, sin reintento. Las llamadas HTTP en si corren via `background_tasks` - la
    preparacion de cada body sigue siendo sincrona porque necesita la sesion de BD abierta."""
    await _notify_frontend(order, background_tasks)
    await admin_notification_service.notify_admin_order_cancelled(order, background_tasks)

async def _notify_frontend(order: Order, background_tasks: BackgroundTasks) -> None:
    try:
        client_account = await order.awaitable_attrs.client_account
        contact_email = ((order.delivery_info or {}).get("contactInfo") or {}).get("email")
        client_email = contact_email or (client_account.email if client_account else None)
        if not client_email:
            logger.warning(f"Orden {order.uuid}: no se pudo resolver el email del cliente, se omite la notificacion de cancelacion al frontend.")
            return

        body = OrderPublic.model_validate(order).model_dump(by_alias=True, mode="json")
        body["clientEmail"] = client_email
        body["clientName"] = resolve_client_name(order, client_account)

        raw_body = json.dumps(body, separators=(",", ":")).encode()
        ts = str(int(time.time()))
        signature = sign_hmac_sha256(settings.FRONTEND_WEBHOOK_SECRET, f"{ts}.".encode() + raw_body)

        url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}{ORDER_CANCELLED_WEBHOOK_PATH}"
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": ts,
            "X-Webhook-Signature": signature,
        }
        background_tasks.add_task(
            _send_webhook, url, raw_body, headers,
            f"Notificacion de pedido cancelado para la orden {order.uuid}",
        )
    except Exception as e:
        logger.error(f"Error inesperado preparando la notificacion de cancelacion de la orden {order.uuid}: {type(e).__name__}: {e!r}")
