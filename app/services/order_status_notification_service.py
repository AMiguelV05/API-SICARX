import json
import logging
import time

import httpx

from app.core.config import settings
from app.models.order import Order
from app.schemas.orders import OrderPublic
from app.core.webhook_signing import sign_hmac_sha256

ORDER_STATUS_CHANGED_WEBHOOK_PATH = "/api/webhooks/order-status-changed"
WEBHOOK_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

logger = logging.getLogger(__name__)


async def _notify(order: Order, event: str, status_message: str) -> None:
    """Nucleo compartido de los 3 eventos de este modulo - misma firma/envio (HMAC sobre
    f"{ts}." + cuerpo crudo, headers X-Webhook-*) que order_notification_service/
    order_cancellation_notification_service, reusa FRONTEND_WEBHOOK_SECRET (mismo
    receptor, el frontend de la tienda). No fatal si falla, sin reintento automatico -
    mismo patron que el resto de estos servicios. A diferencia de order-confirmed/
    order-cancelled (dos dimensiones distintas, cada una con su propio path), estos 3
    eventos son la misma dimension (dispatch_status) cambiando, asi que comparten un
    unico path con un discriminador `event`."""
    try:
        client_account = await order.awaitable_attrs.client_account
        contact_email = ((order.delivery_info or {}).get("contactInfo") or {}).get("email")
        client_email = contact_email or (client_account.email if client_account else None)
        if not client_email:
            logger.warning(f"Orden {order.uuid}: no se pudo resolver el email del cliente, se omite la notificacion '{event}'.")
            return

        body = OrderPublic.model_validate(order).model_dump(by_alias=True, mode="json")
        body["clientEmail"] = client_email
        body["clientName"] = client_account.name if client_account else None
        body["event"] = event
        body["statusMessage"] = status_message

        raw_body = json.dumps(body, separators=(",", ":")).encode()
        ts = str(int(time.time()))
        signature = sign_hmac_sha256(settings.FRONTEND_WEBHOOK_SECRET, f"{ts}.".encode() + raw_body)

        url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}{ORDER_STATUS_CHANGED_WEBHOOK_PATH}"
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
            logger.error(f"Frontend rechazo la notificacion '{event}' para la orden {order.uuid}: {response.status_code} - {response.text}")
            return
        logger.info(f"Notificacion '{event}' enviada al frontend para la orden {order.uuid}.")
    except Exception as e:
        logger.error(f"Error inesperado notificando '{event}' sobre la orden {order.uuid}: {type(e).__name__}: {e!r}")


async def notify_order_accepted(order: Order) -> None:
    await _notify(order, "order-accepted", "Tu pedido fue aceptado y está siendo preparado")


async def notify_order_ready_for_pickup(order: Order) -> None:
    """Solo debe llamarse para ordenes PICKUP que llegan a dispatch_status COMPLETE - ver
    admin_service.advance_order_dispatch_status. Deliberadamente NO se llama para
    DELIVERYMAN en COMPLETE (etapa interna, sin accion del cliente todavia)."""
    await _notify(order, "order-ready-pickup", "Tu pedido está listo para recoger")


async def notify_order_dispatched(order: Order) -> None:
    """Para ordenes DELIVERYMAN que llegan a DISPATCHED, sin importar si fue via una guia
    real de envia.com (admin_service.generate_shipping_label) o un avance manual
    (admin_service.advance_order_dispatch_status) - el cliente recibe el mismo mensaje
    en ambos casos."""
    await _notify(order, "order-dispatched", "Tu pedido está listo y ha sido enviado")
