import json
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.webhook_signing import sign_hmac_sha256
from app.models.webhook_outbox import WebhookOutbox

# Rutas del frontend para los webhooks que pasan por el outbox - ver FRONTEND_INTEGRATION.md.
PRODUCT_BACK_IN_STOCK_PATH = "/api/webhooks/product-back-in-stock"
WISHLIST_PRICE_DROP_PATH = "/api/webhooks/wishlist-price-drop"
CART_ABANDONED_PATH = "/api/webhooks/cart-abandoned"


def enqueue_webhook(db: AsyncSession, event: str, path: str, payload: dict) -> WebhookOutbox:
    """Solo hace db.add en la transaccion del llamador (sin commit propio, mismo patron que
    los inserts de SicarSyncOutbox): encolar el correo y marcar la fuente como notificada
    quedan en un mismo commit. webhook_outbox_worker.py lo envia y reintenta."""
    row = WebhookOutbox(event=event, path=path, payload=payload, status="PENDING", attempts=0)
    db.add(row)
    return row


def build_signed_request(row: WebhookOutbox) -> tuple[str, bytes, dict]:
    """URL, body y encabezados firmados en el momento del envio (timestamp fresco en cada
    intento). Mismo esquema que los demas webhooks salientes, mas X-Webhook-Id."""
    raw_body = json.dumps(row.payload, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    signature = sign_hmac_sha256(settings.FRONTEND_WEBHOOK_SECRET, f"{ts}.".encode() + raw_body)
    url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}{row.path}"
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": ts,
        "X-Webhook-Signature": signature,
        "X-Webhook-Id": row.uuid,
        "X-Webhook-Event": row.event,
    }
    return url, raw_body, headers
