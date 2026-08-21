import json
import logging
import time
from typing import Optional

from fastapi import BackgroundTasks

from app.core.config import settings
from app.core.webhook_client import send_signed_webhook
from app.models.order import Order
from app.schemas.orders import OrderPublic
from app.core.webhook_signing import sign_hmac_sha256

ORDER_CANCELLED_WEBHOOK_PATH = "/api/webhooks/order-cancelled"
SICAR_SYNC_FAILED_WEBHOOK_PATH = "/api/webhooks/order-sicar-sync-failed"
STOCK_DRIFT_WEBHOOK_PATH = "/api/webhooks/product-stock-drift"

logger = logging.getLogger(__name__)

def _admin_webhook_configured() -> bool:
    return bool(settings.ADMIN_DASHBOARD_BASE_URL and settings.ADMIN_WEBHOOK_SECRET)

def _build_admin_request(path: str, body: dict) -> Optional[tuple[str, bytes, dict]]:
    """Prepara url/body firmado/headers - sin llamada de red. None si el webhook admin no
    esta configurado todavia (dashboard admin aun no existe, ver CLAUDE.md)."""
    if not _admin_webhook_configured():
        return None
    raw_body = json.dumps(body, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    signature = sign_hmac_sha256(settings.ADMIN_WEBHOOK_SECRET, f"{ts}.".encode() + raw_body)
    url = f"{settings.ADMIN_DASHBOARD_BASE_URL.rstrip('/')}{path}"
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": ts,
        "X-Webhook-Signature": signature,
    }
    return url, raw_body, headers

async def notify_admin_order_cancelled(order: Order, background_tasks: BackgroundTasks) -> None:
    """Señal informativa al dashboard admin de que la orden se cancelo; llamar solo desde
    notify_order_cancelled (siempre dentro de un request, por eso recibe BackgroundTasks -
    a diferencia de las dos notificaciones de abajo, que corren desde el worker)."""
    client_account = await order.awaitable_attrs.client_account
    body = OrderPublic.model_validate(order).model_dump(by_alias=True, mode="json")
    body["clientEmail"] = client_account.email if client_account else None
    body["clientName"] = client_account.name if client_account else None
    log_context = f"Orden {order.uuid} cancelada"
    request = _build_admin_request(ORDER_CANCELLED_WEBHOOK_PATH, body)
    if request is None:
        logger.info(f"{log_context}: ADMIN_DASHBOARD_BASE_URL/ADMIN_WEBHOOK_SECRET no configurados todavia (el dashboard admin no existe aun), se omite el webhook.")
        return
    background_tasks.add_task(send_signed_webhook, *request, log_context)

async def notify_admin_sicar_sync_failed(order: Order, last_error: str) -> None:
    """Señal de que el worker agoto reintentos con Sicar X - requiere reconciliacion manual,
    a diferencia de la notificacion de rutina de arriba. Llamada desde sicar_sync_worker.py
    (fuera de un ciclo request/response), asi que se espera directo - no hay respuesta HTTP
    que agilizar aqui."""
    body = {
        "orderUuid": order.uuid,
        "sicarOrderId": order.sicar_order_id,
        "lastError": last_error,
    }
    log_context = f"Sincronizacion con Sicar X agotada para la orden {order.uuid}"
    request = _build_admin_request(SICAR_SYNC_FAILED_WEBHOOK_PATH, body)
    if request is None:
        logger.info(f"{log_context}: ADMIN_DASHBOARD_BASE_URL/ADMIN_WEBHOOK_SECRET no configurados todavia (el dashboard admin no existe aun), se omite el webhook.")
        return
    await send_signed_webhook(*request, log_context)

async def notify_admin_stock_drift(products: list[dict]) -> None:
    """Señal de que Product.reserved supera a Product.stock para uno o mas productos - el
    stock real de Sicar X bajo por una razon ajena a este backend (venta en tienda, otro
    canal) mientras habia unidades reservadas localmente, asi que Product.available_stock
    quedo en 0 aunque `reserved` siga reteniendo mas de lo que fisicamente existe. Llamada
    desde sync_task.py tras cada corrida exitosa del sync de catalogo (fuera de un ciclo
    request/response), asi que se espera directo."""
    body = {"products": products}
    log_context = f"Deriva de stock detectada en {len(products)} producto(s) (reserved > stock)"
    request = _build_admin_request(STOCK_DRIFT_WEBHOOK_PATH, body)
    if request is None:
        logger.info(f"{log_context}: ADMIN_DASHBOARD_BASE_URL/ADMIN_WEBHOOK_SECRET no configurados todavia (el dashboard admin no existe aun), se omite el webhook.")
        return
    await send_signed_webhook(*request, log_context)
