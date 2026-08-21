import json
import logging
import time

from fastapi import BackgroundTasks

from app.core.config import settings
from app.core.webhook_client import send_signed_webhook
from app.models.order import Order
from app.schemas.orders import OrderPublic
from app.core.webhook_signing import sign_hmac_sha256
from app.services.order_display_service import resolve_client_name

ORDER_CONFIRMED_WEBHOOK_PATH = "/api/webhooks/order-confirmed"

logger = logging.getLogger(__name__)

async def notify_order_confirmed(order: Order, background_tasks: BackgroundTasks) -> None:
    """Avisa al frontend que la orden paso a PAID (el frontend envia el correo de
    confirmacion con su propio Resend). No fatal ni con reintento - no debe bloquear un
    pago ya aplicado. Solo la llamada HTTP en si se difiere a `background_tasks` - la
    preparacion del body (que necesita `order.awaitable_attrs.client_account`) sigue
    siendo sincrona porque necesita la sesion de BD todavia abierta."""
    try:
        client_account = await order.awaitable_attrs.client_account
        contact_email = ((order.delivery_info or {}).get("contactInfo") or {}).get("email")
        client_email = contact_email or (client_account.email if client_account else None)
        if not client_email:
            logger.warning(f"Orden {order.uuid}: no se pudo resolver el email del cliente, se omite la notificacion al frontend.")
            return

        body = OrderPublic.model_validate(order).model_dump(by_alias=True, mode="json")
        body["clientEmail"] = client_email
        body["clientName"] = resolve_client_name(order, client_account)

        raw_body = json.dumps(body, separators=(",", ":")).encode()
        ts = str(int(time.time()))
        signature = sign_hmac_sha256(settings.FRONTEND_WEBHOOK_SECRET, f"{ts}.".encode() + raw_body)

        url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}{ORDER_CONFIRMED_WEBHOOK_PATH}"
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": ts,
            "X-Webhook-Signature": signature,
        }
        background_tasks.add_task(
            send_signed_webhook, url, raw_body, headers,
            f"Notificacion de pedido confirmado para la orden {order.uuid}",
        )
    except Exception as e:
        logger.error(f"Error inesperado preparando la notificacion de la orden {order.uuid}: {type(e).__name__}: {e!r}")
