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
STOCK_DRIFT_WEBHOOK_PATH = "/api/webhooks/product-stock-drift"
WEBHOOK_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

logger = logging.getLogger(__name__)

def _admin_webhook_configured() -> bool:
    return bool(settings.ADMIN_DASHBOARD_BASE_URL and settings.ADMIN_WEBHOOK_SECRET)

async def _post_to_admin(path: str, body: dict, log_context: str) -> None:
    """Nucleo compartido de firma/envio de los 2 webhooks admin; mismo esquema que los webhooks al frontend pero con su propio destino/secreto (dashboard admin, no la tienda)."""
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
    """Señal informativa al dashboard admin de que la orden se cancelo; llamar solo desde notify_order_cancelled."""
    client_account = await order.awaitable_attrs.client_account
    body = OrderPublic.model_validate(order).model_dump(by_alias=True, mode="json")
    body["clientEmail"] = client_account.email if client_account else None
    body["clientName"] = client_account.name if client_account else None
    await _post_to_admin(ORDER_CANCELLED_WEBHOOK_PATH, body, f"Orden {order.uuid} cancelada")

async def notify_admin_sicar_sync_failed(order: Order, last_error: str) -> None:
    """Señal de que el worker agoto reintentos con Sicar X - requiere reconciliacion manual, a diferencia de la notificacion de rutina de arriba."""
    body = {
        "orderUuid": order.uuid,
        "sicarOrderId": order.sicar_order_id,
        "lastError": last_error,
    }
    await _post_to_admin(SICAR_SYNC_FAILED_WEBHOOK_PATH, body, f"Sincronizacion con Sicar X agotada para la orden {order.uuid}")

async def notify_admin_stock_drift(products: list[dict]) -> None:
    """Señal de que Product.reserved supera a Product.stock para uno o mas productos - el
    stock real de Sicar X bajo por una razon ajena a este backend (venta en tienda, otro
    canal) mientras habia unidades reservadas localmente, asi que Product.available_stock
    quedo en 0 aunque `reserved` siga reteniendo mas de lo que fisicamente existe. Llamada
    desde sync_task.py tras cada corrida exitosa del sync de catalogo."""
    body = {"products": products}
    await _post_to_admin(STOCK_DRIFT_WEBHOOK_PATH, body, f"Deriva de stock detectada en {len(products)} producto(s) (reserved > stock)")
