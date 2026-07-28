import json
import logging
import time

import httpx

from app.core.config import settings
from app.models.order import Order
from app.schemas.orders import OrderPublic
from app.core.webhook_signing import sign_hmac_sha256

ORDER_CANCELLED_WEBHOOK_PATH = "/api/webhooks/order-cancelled"
SICAR_SYNC_FAILED_WEBHOOK_PATH = "/api/webhooks/order-sicar-sync-failed"
WEBHOOK_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

logger = logging.getLogger(__name__)

def _admin_webhook_configured() -> bool:
    return bool(settings.ADMIN_DASHBOARD_BASE_URL and settings.ADMIN_WEBHOOK_SECRET)

async def _post_to_admin(path: str, body: dict, log_context: str) -> None:
    """Nucleo compartido de firma/envio para los dos webhooks de este modulo - mismo
    esquema (headers X-Webhook-*, HMAC sobre f"{ts}." + cuerpo crudo) que
    order_notification_service/order_cancellation_notification_service, pero con su
    propio destino/secreto (ADMIN_DASHBOARD_BASE_URL/ADMIN_WEBHOOK_SECRET) porque el
    dashboard admin es un receptor distinto del frontend de la tienda."""
    try:
        if not _admin_webhook_configured():
            logger.info(f"{log_context}: ADMIN_DASHBOARD_BASE_URL/ADMIN_WEBHOOK_SECRET no configurados todavia (el dashboard admin no existe aun), se omite el webhook.")
            return

        raw_body = json.dumps(body, separators=(",", ":")).encode()
        ts = str(int(time.time()))
        signature = sign_hmac_sha256(settings.ADMIN_WEBHOOK_SECRET, f"{ts}.".encode() + raw_body)

        url = f"{settings.ADMIN_DASHBOARD_BASE_URL.rstrip('/')}{path}"
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
            logger.error(f"{log_context}: el dashboard admin rechazo el webhook: {response.status_code} - {response.text}")
            return
        logger.info(f"{log_context}: webhook enviado al dashboard admin.")
    except Exception as e:
        logger.error(f"{log_context}: error inesperado enviando el webhook al dashboard admin: {type(e).__name__}: {e!r}")

async def notify_admin_order_cancelled(order: Order) -> None:
    """Avisa al futuro dashboard admin que una orden paso a CANCELLED - llamada desde
    order_cancellation_notification_service.notify_order_cancelled, nunca directamente.
    Señal puramente informativa (distinta de notify_admin_sicar_sync_failed abajo, que
    señala que algo necesita intervencion manual)."""
    client_account = await order.awaitable_attrs.client_account
    body = OrderPublic.model_validate(order).model_dump(by_alias=True, mode="json")
    body["clientEmail"] = client_account.email if client_account else None
    body["clientName"] = client_account.name if client_account else None
    await _post_to_admin(ORDER_CANCELLED_WEBHOOK_PATH, body, f"Orden {order.uuid} cancelada")

async def notify_admin_sicar_sync_failed(order: Order, last_error: str) -> None:
    """Avisa al futuro dashboard admin que app/worker/sicar_sync_worker.py agoto sus
    reintentos de cancelacion en Sicar X para esta orden - a diferencia de
    notify_admin_order_cancelled, esta es una señal de "necesita reconciliacion manual
    en el panel de Sicar X", no una notificacion de rutina."""
    body = {
        "orderUuid": order.uuid,
        "sicarOrderId": order.sicar_order_id,
        "lastError": last_error,
    }
    await _post_to_admin(SICAR_SYNC_FAILED_WEBHOOK_PATH, body, f"Sincronizacion con Sicar X agotada para la orden {order.uuid}")
