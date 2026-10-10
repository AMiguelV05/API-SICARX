"""Recordatorio de carrito abandonado (webhook cart-abandoned, correo de marketing).

Un carrito califica si es de una cuenta activa con marketing_emails = true, tiene
productos, no se ha tocado en 2 h, y la cuenta no ha hecho ningun pedido desde la ultima
vez que lo toco. Como maximo un correo por carrito cada 7 dias, y solo si el carrito cambio
despues del correo anterior (no se reenvia semana tras semana un carrito que nadie toco).
Los carritos anonimos no califican: no hay correo. Distinto de abandoned_order_worker.py,
que cancela pedidos TO_PAY sin pago. Ver CLAUDE.md, "Correos de marketing"."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.error_tracking import capture_exception
from app.core.security import create_unsubscribe_token
from app.models.cart import Cart
from app.models.client import ClientAccount
from app.models.order import Order
from app.services.cart_service import _enrich
from app.services.webhook_outbox_service import CART_ABANDONED_PATH, enqueue_webhook

logger = logging.getLogger(__name__)

IDLE_BEFORE_REMINDER = timedelta(hours=2)
MIN_INTERVAL_PER_CART = timedelta(days=7)
MAX_CARTS_PER_TICK = 200


async def process_abandoned_carts(session: AsyncSession) -> int:
    """Encola los webhooks y marca abandoned_notified_at en una sola transaccion. Devuelve
    cuantos se encolaron."""
    now = datetime.now(timezone.utc)
    last_touch = func.coalesce(Cart.updated_at, Cart.created_at)
    rows = (await session.execute(
        select(Cart, ClientAccount)
        .join(ClientAccount, ClientAccount.id == Cart.client_account_id)
        .where(
            ClientAccount.marketing_emails == True,  # noqa: E712
            ClientAccount.is_active == True,  # noqa: E712
            func.json_array_length(Cart.items) > 0,
            last_touch < now - IDLE_BEFORE_REMINDER,
            ~exists().where(Order.client_account_id == Cart.client_account_id, Order.created_at > last_touch),
            or_(
                Cart.abandoned_notified_at.is_(None),
                and_(Cart.abandoned_notified_at < now - MIN_INTERVAL_PER_CART, last_touch > Cart.abandoned_notified_at),
            ),
        )
        .order_by(last_touch)
        .limit(MAX_CARTS_PER_TICK)
        .with_for_update(of=Cart, skip_locked=True)
    )).tuples().all()

    sent = 0
    for cart, client in rows:
        lines, subtotal, _, _ = await _enrich(session, cart.items or [])
        # Sin ningun producto disponible no tiene caso el correo; igual se marca, para no
        # reevaluar este carrito en cada pasada hasta que cambie.
        if any(line.available for line in lines):
            enqueue_webhook(session, "cart-abandoned", CART_ABANDONED_PATH, {
                "clientUuid": client.uuid,
                "clientEmail": client.email,
                "clientName": client.name,
                "items": [
                    {"sicarUuid": line.productUuid, "name": line.name, "imageUrl": line.imageUrl,
                     "price": line.price, "quantity": line.quantity, "available": line.available}
                    for line in lines
                ],
                "subtotal": subtotal,
                "unsubscribeToken": create_unsubscribe_token("marketing", client.uuid),
            })
            sent += 1
        # UPDATE core fijando updated_at a su propio valor: via ORM, onupdate lo moveria y
        # el carrito pareceria "cambiado despues del correo".
        await session.execute(
            update(Cart).where(Cart.id == cart.id)
            .values(abandoned_notified_at=now, updated_at=Cart.updated_at)
            .execution_options(synchronize_session=False)
        )

    await session.commit()
    if sent:
        logger.info(f"Recordatorios de carrito abandonado encolados: {sent}.")
    return sent


async def scheduled_abandoned_cart_job() -> None:
    try:
        async with AsyncSessionLocal() as session:
            await process_abandoned_carts(session)
    except Exception as e:
        logger.error(f"Fallo en la tarea programada de carritos abandonados: {e!r}")
        capture_exception(e, job="abandoned_carts")
