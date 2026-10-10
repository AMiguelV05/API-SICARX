"""Avisos de reabastecimiento: encola el webhook product-back-in-stock para cada suscripcion
activa cuyo producto ya tenga existencia. Una suscripcion solo se puede crear con el
producto agotado, asi que "activa + con existencia" significa "volvio a haber" - sin tocar
el sync ni agregar triggers, y cubriendo a todos los escritores de stock (sync de Sicar X,
CANCEL del outbox de Sicar X, liberacion de reservas). Si la existencia vuelve a 0 antes de
este tick, no se manda nada. Ver CLAUDE.md, "Avisos de reabastecimiento"."""
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.error_tracking import capture_exception
from app.core.security import create_unsubscribe_token
from app.models.client import ClientAccount
from app.models.product import Product
from app.models.stock_alert import StockAlert
from app.services.webhook_outbox_service import PRODUCT_BACK_IN_STOCK_PATH, enqueue_webhook

logger = logging.getLogger(__name__)

MAX_ALERTS_PER_TICK = 500


def build_payload(alert: StockAlert, product: Product, client_name: str | None) -> dict:
    return {
        "email": alert.email,
        "clientName": client_name,
        "product": {
            "sicarUuid": product.sicar_uuid,
            "sku": product.sku,
            "name": product.name,
            "imageUrl": product.image_url,
            "price": float(product.price),
            "stock": float(product.available_stock),
        },
        "unsubscribeToken": create_unsubscribe_token("stock-alert", alert.uuid),
    }


async def process_back_in_stock(session: AsyncSession) -> int:
    """Encola y marca notified_at en una sola transaccion. Devuelve cuantas se notificaron."""
    rows = (await session.execute(
        select(StockAlert, Product, ClientAccount.name)
        .join(Product, Product.id == StockAlert.product_id)
        .outerjoin(ClientAccount, ClientAccount.id == StockAlert.client_account_id)
        .where(
            StockAlert.notified_at.is_(None),
            StockAlert.unsubscribed_at.is_(None),
            StockAlert.expires_at > func.now(),
            Product.is_deleted == False,  # noqa: E712
            Product.is_active == True,  # noqa: E712
            Product.available_stock > 0,
        )
        .order_by(StockAlert.id)
        .limit(MAX_ALERTS_PER_TICK)
        .with_for_update(of=StockAlert, skip_locked=True)
    )).tuples().all()

    now = datetime.now(timezone.utc)
    for alert, product, client_name in rows:
        enqueue_webhook(session, "product-back-in-stock", PRODUCT_BACK_IN_STOCK_PATH, build_payload(alert, product, client_name))
        alert.notified_at = now
    await session.commit()
    if rows:
        logger.info(f"Avisos de reabastecimiento encolados: {len(rows)}.")
    return len(rows)


async def scheduled_stock_alert_job() -> None:
    try:
        async with AsyncSessionLocal() as session:
            await process_back_in_stock(session)
    except Exception as e:
        logger.error(f"Fallo en la tarea programada de avisos de reabastecimiento: {e!r}")
        capture_exception(e, job="stock_alerts")
