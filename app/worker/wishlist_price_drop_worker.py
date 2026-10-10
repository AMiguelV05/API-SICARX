"""Aviso de baja de precio en favoritos (webhook wishlist-price-drop, correo de marketing).

No toca el sync de catalogo: compara el precio actual de cada producto guardado contra
WishlistItem.baseline_price (fijado al guardar). Asi funciona con cualquier escritor de
precio y detecta bajas acumuladas. Reglas:
- baja >= 5 % contra la referencia, y distinta del ultimo precio ya avisado;
- solo cuentas activas con marketing_emails = true;
- un webhook por cliente cada 24 h como maximo, agrupando sus productos (un producto en
  varias listas cuenta una vez);
- si el precio sube, la referencia sube con el; tras avisar, la referencia queda en el
  precio avisado.
Limitacion aceptada: una subida y bajada dentro del mismo intervalo del job no se ve. Ver
CLAUDE.md, "Correos de marketing"."""
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.error_tracking import capture_exception
from app.core.security import create_unsubscribe_token
from app.models.client import ClientAccount
from app.models.product import Product
from app.models.wishlist import WishlistCollection, WishlistItem
from app.services.webhook_outbox_service import WISHLIST_PRICE_DROP_PATH, enqueue_webhook

logger = logging.getLogger(__name__)

DROP_THRESHOLD = Decimal("0.95")  # precio <= 95 % de la referencia
MIN_INTERVAL_PER_CLIENT = timedelta(hours=24)
MAX_ROWS_PER_TICK = 5000


async def _sync_baselines(session: AsyncSession) -> None:
    # Filas sin referencia (no deberia haber tras la migracion, salvo inserciones por otra via).
    await session.execute(text("""
        UPDATE wishlist_items wi SET baseline_price = p.price
        FROM products p WHERE p.id = wi.product_id AND wi.baseline_price IS NULL
    """))
    # El precio subio: la referencia sube con el, para avisar si despues vuelve a bajar.
    await session.execute(text("""
        UPDATE wishlist_items wi SET baseline_price = p.price
        FROM products p WHERE p.id = wi.product_id AND p.price > wi.baseline_price
    """))


async def process_price_drops(session: AsyncSession) -> int:
    """Encola los webhooks y actualiza referencias en una sola transaccion. Devuelve cuantos
    clientes se avisaron."""
    await _sync_baselines(session)
    now = datetime.now(timezone.utc)

    rows = (await session.execute(
        select(WishlistItem.id, WishlistItem.baseline_price, ClientAccount, Product)
        .join(WishlistCollection, WishlistCollection.id == WishlistItem.collection_id)
        .join(ClientAccount, ClientAccount.id == WishlistCollection.client_account_id)
        .join(Product, Product.id == WishlistItem.product_id)
        .where(
            ClientAccount.marketing_emails == True,  # noqa: E712
            ClientAccount.is_active == True,  # noqa: E712
            or_(ClientAccount.last_price_drop_email_at.is_(None), ClientAccount.last_price_drop_email_at < now - MIN_INTERVAL_PER_CLIENT),
            Product.is_deleted == False,  # noqa: E712
            Product.is_active == True,  # noqa: E712
            Product.price > 0,
            Product.price <= WishlistItem.baseline_price * DROP_THRESHOLD,
            or_(WishlistItem.last_notified_price.is_(None), WishlistItem.last_notified_price != Product.price),
        )
        .order_by(ClientAccount.id)
        .limit(MAX_ROWS_PER_TICK)
    )).tuples().all()

    by_client: dict[int, dict] = defaultdict(lambda: {"client": None, "products": {}, "item_ids": []})
    for item_id, baseline, client, product in rows:
        entry = by_client[client.id]
        entry["client"] = client
        entry["item_ids"].append(item_id)
        known = entry["products"].get(product.id)
        if known is None or baseline > known[1]:
            entry["products"][product.id] = (product, baseline)

    for entry in by_client.values():
        client = entry["client"]
        items = [
            {
                "product": {"sicarUuid": p.sicar_uuid, "sku": p.sku, "name": p.name, "imageUrl": p.image_url},
                "oldPrice": float(baseline),
                "newPrice": float(p.price),
            }
            for p, baseline in entry["products"].values()
        ]
        enqueue_webhook(session, "wishlist-price-drop", WISHLIST_PRICE_DROP_PATH, {
            "clientUuid": client.uuid,
            "clientEmail": client.email,
            "clientName": client.name,
            "items": items,
            "unsubscribeToken": create_unsubscribe_token("marketing", client.uuid),
        })
        await session.execute(text("""
            UPDATE wishlist_items wi SET baseline_price = p.price, last_notified_price = p.price
            FROM products p WHERE p.id = wi.product_id AND wi.id = ANY(CAST(:ids AS integer[]))
        """), {"ids": entry["item_ids"]})
        await session.execute(
            update(ClientAccount).where(ClientAccount.id == client.id)
            .values(last_price_drop_email_at=now)
            .execution_options(synchronize_session=False)
        )

    await session.commit()
    if by_client:
        logger.info(f"Avisos de baja de precio encolados para {len(by_client)} clientes.")
    return len(by_client)


async def scheduled_price_drop_job() -> None:
    try:
        async with AsyncSessionLocal() as session:
            await process_price_drops(session)
    except Exception as e:
        logger.error(f"Fallo en la tarea programada de avisos de baja de precio: {e!r}")
        capture_exception(e, job="wishlist_price_drop")
