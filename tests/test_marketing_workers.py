"""Correos de marketing: wishlist_price_drop_worker y abandoned_cart_worker."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.models.cart import Cart
from app.models.order import Order
from app.models.webhook_outbox import WebhookOutbox
from app.models.wishlist import WishlistCollection, WishlistItem
from app.worker.abandoned_cart_worker import process_abandoned_carts
from app.worker.wishlist_price_drop_worker import process_price_drops
from tests.conftest import make_client_account, make_product

NOW = datetime.now(timezone.utc)


async def _outbox(db, event):
    return (await db.execute(select(WebhookOutbox).where(WebhookOutbox.event == event).order_by(WebhookOutbox.id))).scalars().all()


async def _wishlist(db, client, products, baseline=Decimal("100.00")):
    collections = []
    for name in ("Favoritos", "Taller"):
        c = WishlistCollection(client_account_id=client.id, name=name, is_default=name == "Favoritos")
        db.add(c)
        collections.append(c)
    await db.flush()
    for p in products:
        for c in collections:  # el mismo producto en dos listas: un solo aviso
            db.add(WishlistItem(collection_id=c.id, product_id=p.id, baseline_price=baseline))
    await db.flush()


async def test_price_drop_threshold_grouping_and_dedupe(db):
    client = make_client_account(name="Ana", marketing_emails=True)
    opted_out = make_client_account(marketing_emails=False)
    db.add_all([client, opted_out])
    await db.flush()
    dropped = make_product(price=Decimal("95.00"))  # justo 5 %
    small = make_product(price=Decimal("96.00"))  # 4 %: no
    db.add_all([dropped, small])
    await db.flush()
    await _wishlist(db, client, [dropped, small])
    await _wishlist(db, opted_out, [dropped])

    assert await process_price_drops(db) == 1
    [row] = await _outbox(db, "wishlist-price-drop")
    assert row.payload["clientEmail"] == client.email and row.payload["clientName"] == "Ana"
    assert row.payload["items"] == [{
        "product": {"sicarUuid": dropped.sicar_uuid, "sku": dropped.sku, "name": dropped.name, "imageUrl": None},
        "oldPrice": 100.0, "newPrice": 95.0,
    }]

    # Mismo precio: no se repite. Otra baja el mismo dia: espera al tope diario.
    assert await process_price_drops(db) == 0
    dropped.price = Decimal("80.00")
    await db.flush()
    assert await process_price_drops(db) == 0

    await db.refresh(client)
    client.last_price_drop_email_at = NOW - timedelta(days=2)
    await db.flush()
    assert await process_price_drops(db) == 1
    assert (await _outbox(db, "wishlist-price-drop"))[-1].payload["items"][0]["oldPrice"] == 95.0


async def test_price_rise_raises_baseline(db):
    client = make_client_account(marketing_emails=True)
    db.add(client)
    await db.flush()
    product = make_product(price=Decimal("120.00"))
    db.add(product)
    await db.flush()
    await _wishlist(db, client, [product])

    assert await process_price_drops(db) == 0
    product.price = Decimal("110.00")  # -8.3 % contra la referencia nueva (120)
    await db.flush()
    assert await process_price_drops(db) == 1
    assert (await _outbox(db, "wishlist-price-drop"))[0].payload["items"][0]["oldPrice"] == 120.0


async def _cart(db, client, product, touched):
    cart = Cart(uuid=str(uuid.uuid4()), client_account_id=client.id, items=[{"uuid": product.sicar_uuid, "quantity": 2}])
    db.add(cart)
    await db.flush()
    cart.created_at = touched
    cart.updated_at = touched
    await db.flush()
    return cart


async def test_abandoned_cart_rules(db):
    product = make_product(price=Decimal("65.00"))
    db.add(product)
    client = make_client_account(name="Luis", marketing_emails=True)
    recent_client = make_client_account(marketing_emails=True)
    ordered_client = make_client_account(marketing_emails=True)
    no_mkt = make_client_account(marketing_emails=False)
    db.add_all([client, recent_client, ordered_client, no_mkt])
    await db.flush()

    cart = await _cart(db, client, product, NOW - timedelta(hours=3))
    await _cart(db, recent_client, product, NOW - timedelta(minutes=30))
    await _cart(db, ordered_client, product, NOW - timedelta(hours=3))
    await _cart(db, no_mkt, product, NOW - timedelta(hours=3))
    db.add(Order(client_account_id=ordered_client.id, sicar_order_id=str(uuid.uuid4()), status="PAID", branch_id=1,
                 total=Decimal("1"), total_quantity=Decimal("1"), delivery_info={}, items=[], created_at=NOW - timedelta(hours=1)))
    await db.flush()

    assert await process_abandoned_carts(db) == 1
    [row] = await _outbox(db, "cart-abandoned")
    assert row.payload["clientEmail"] == client.email and row.payload["subtotal"] == 130.0
    assert row.payload["items"] == [{"sicarUuid": product.sicar_uuid, "name": product.name, "imageUrl": None,
                                     "price": 65.0, "quantity": 2.0, "available": True}]

    await db.refresh(cart)
    assert cart.updated_at == NOW - timedelta(hours=3)  # marcar no cuenta como "cambio"
    assert await process_abandoned_carts(db) == 0

    # 8 dias despues sin tocar el carrito: no se reenvia. Si cambio despues del correo: si.
    cart.abandoned_notified_at = NOW - timedelta(days=8)
    cart.updated_at = NOW - timedelta(days=9)
    await db.flush()
    assert await process_abandoned_carts(db) == 0
    cart.updated_at = NOW - timedelta(days=1)
    await db.flush()
    assert await process_abandoned_carts(db) == 1
