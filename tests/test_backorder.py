"""Fase 3 de URREA: venta sobre pedido (backorder_service, POST /orders, carrito, admin).
URREA se mockea en urrea_client.check_availability."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app.core.config import settings
from app.models.order import Order
from app.models.product import Product
from app.models.supplier_product import SupplierProduct
from app.schemas.orders import OrderPublic
from app.services import backorder_service, urrea_client
from app.services.order_history_service import prepare_local_cancellation
from app.services.order_service import validate_cart_items
from app.services.urrea_client import UrreaAvailability, UrreaError
from tests.conftest import make_product

CONTACT = {"name": "Ana López", "phone": "8112345678", "email": "ana@example.com"}


class FakeUrrea:
    def __init__(self):
        self.calls: list[tuple[str, int]] = []
        self.sufficient = True
        self.error: Exception | None = None

    async def __call__(self, code: str, quantity: int) -> UrreaAvailability:
        self.calls.append((code, quantity))
        if self.error:
            raise self.error
        return UrreaAvailability(code, quantity, found=True, sufficient=self.sufficient)


@pytest.fixture(autouse=True)
def backorder_on(monkeypatch):
    monkeypatch.setattr(settings, "URREA_BACKORDER_ENABLED", True)
    monkeypatch.setattr(settings, "URREA_USER", "u")
    monkeypatch.setattr(settings, "URREA_PASSWORD", "p")
    monkeypatch.setattr(settings, "URREA_BACKORDER_LEAD_DAYS", 5)
    monkeypatch.setattr(settings, "SHIPPING_CHECKOUT_ENABLED", False)
    backorder_service.clear_cache()
    yield
    backorder_service.clear_cache()


@pytest.fixture
def urrea(monkeypatch):
    fake = FakeUrrea()
    monkeypatch.setattr(urrea_client, "check_availability", fake)
    return fake


async def _linked(db, *, stock="2", supplier_available=True, multiple=6, **overrides) -> Product:
    product = make_product(stock=Decimal(stock), supplier_available=supplier_available, **overrides)
    db.add(product)
    await db.flush()
    db.add(SupplierProduct(
        supplier="URREA", supplier_code=f"C{product.id}", product_id=product.id, match_method="SKU_PREFIX",
        sale_multiple=multiple, list_price=Decimal("57"),
    ))
    await db.flush()
    return product


def _order_body(product, qty):
    return {"products": [{"uuid": product.sicar_uuid, "quantity": qty}], "deliveryInfo": {"contactInfo": CONTACT, "deliveryType": "PICKUP"}}


async def _reserved(db, sicar_uuid: str) -> Decimal:
    # Por sicar_uuid ya guardado: tras el rollback de la ruta en un 409/503 los objetos ORM expiran.
    return await db.scalar(select(Product.reserved).where(Product.sicar_uuid == sicar_uuid).execution_options(populate_existing=True))


async def _reload(db, product) -> Product:
    return await db.scalar(select(Product).where(Product.id == product.id).execution_options(populate_existing=True))


# --- Oferta en el catalogo

async def test_availability_badge(db, monkeypatch):
    in_stock = make_product(stock=Decimal("3"), supplier_available=True)
    backorder = make_product(stock=Decimal("0"), supplier_available=True)
    unavailable = make_product(stock=Decimal("0"), supplier_available=False)
    bulk = make_product(stock=Decimal("0"), supplier_available=True, is_bulk=True)
    assert (in_stock.availability, in_stock.backorder_lead_days) == ("IN_STOCK", 5)
    assert (backorder.availability, backorder.backorder_lead_days) == ("BACKORDER", 5)
    assert (unavailable.availability, unavailable.backorder_lead_days) == ("OUT_OF_STOCK", None)
    assert (bulk.availability, bulk.backorder_lead_days) == ("OUT_OF_STOCK", None)

    monkeypatch.setattr(settings, "URREA_BACKORDER_ENABLED", False)
    assert (backorder.availability, backorder.backorder_lead_days) == ("OUT_OF_STOCK", None)


async def test_catalog_and_detail_expose_availability(db, api):
    brand = f"Backorder-{uuid.uuid4().hex[:6]}"
    product = await _linked(db, stock="0", brand=brand, details_updated_at=datetime.now(timezone.utc))
    docs = (await api.post("/v1/products", json={"brand": brand})).json()["docs"]
    assert (docs[0]["availability"], docs[0]["backorderLeadDays"]) == ("BACKORDER", 5)
    detail = (await api.get(f"/v1/products/{product.sicar_uuid}")).json()
    assert (detail["availability"], detail["backorderLeadDays"]) == ("BACKORDER", 5)


# --- GET /products/{uuid}/availability

async def test_availability_endpoint(db, api, urrea):
    product = await _linked(db, stock="2")
    url = f"/v1/products/{product.sicar_uuid}/availability"

    body = (await api.get(url, params={"quantity": 2})).json()
    assert body == {"quantity": 2.0, "stock": 2.0, "backorderQuantity": 0.0, "available": True, "leadDays": None}
    assert urrea.calls == []

    body = (await api.get(url, params={"quantity": 5})).json()
    assert body == {"quantity": 5.0, "stock": 2.0, "backorderQuantity": 3.0, "available": True, "leadDays": 5}
    assert urrea.calls == [(f"C{product.id}", 3)]

    # "Suficiente" para 3 cubre 2 sin volver a llamar.
    assert (await api.get(url, params={"quantity": 4})).json()["available"] is True
    assert len(urrea.calls) == 1

    urrea.sufficient = False
    body = (await api.get(url, params={"quantity": 10})).json()
    assert (body["available"], body["backorderQuantity"]) == (False, 0.0)


async def test_availability_endpoint_without_urrea_answer_is_null(db, api, urrea):
    product = await _linked(db, stock="0")
    urrea.error = UrreaError("caido")
    body = (await api.get(f"/v1/products/{product.sicar_uuid}/availability", params={"quantity": 1})).json()
    assert body["available"] is None


async def test_availability_endpoint_never_asks_urrea_for_products_without_backorder(db, api, urrea):
    product = await _linked(db, stock="0", supplier_available=False)
    body = (await api.get(f"/v1/products/{product.sicar_uuid}/availability", params={"quantity": 1})).json()
    assert body["available"] is False
    assert urrea.calls == []
    assert (await api.get(f"/v1/products/{uuid.uuid4()}/availability", params={"quantity": 1})).status_code == 404


# --- POST /orders

async def test_order_with_backorder_line_reserves_full_quantity(db, api, urrea):
    product = await _linked(db, stock="2")

    r = await api.post("/v1/orders", json=_order_body(product, 5))
    assert r.status_code == 201, r.text
    assert urrea.calls == [(f"C{product.id}", 3)]

    order = await db.scalar(select(Order).where(Order.uuid == r.json()["orderUuid"]))
    line = order.items[0]
    assert (line["fulfillment"], line["backorderQuantity"], line["backorderLeadDays"]) == ("BACKORDER", "3", 5)
    # Reserva completa: cuando llegue la mercancia de URREA no queda libre para otro cliente.
    assert (await _reload(db, product)).reserved == Decimal("5")

    public = OrderPublic.model_validate(order).model_dump(by_alias=True)
    assert (public["hasBackorder"], public["backorderLeadDays"]) == (True, 5)
    assert public["items"][0]["backorderQuantity"] == 3.0

    # Cancelar antes de aceptar libera todo lo reservado.
    await prepare_local_cancellation(db, order)
    await db.flush()
    assert (await _reload(db, product)).reserved == Decimal("0")


async def test_order_within_stock_does_not_call_urrea(db, api, urrea):
    product = await _linked(db, stock="5")
    r = await api.post("/v1/orders", json=_order_body(product, 5))
    assert r.status_code == 201, r.text
    assert urrea.calls == []
    order = await db.scalar(select(Order).where(Order.uuid == r.json()["orderUuid"]))
    assert (order.items[0]["fulfillment"], order.items[0]["backorderQuantity"]) == ("STOCK", "0")
    assert OrderPublic.model_validate(order).has_backorder is False


async def test_order_rejected_when_urrea_has_no_stock(db, api, urrea):
    product = await _linked(db, stock="0")
    product_uuid = product.sicar_uuid
    urrea.sufficient = False
    r = await api.post("/v1/orders", json=_order_body(product, 2))
    assert r.status_code == 409
    assert await _reserved(db, product_uuid) == Decimal("0")


async def test_order_503_when_urrea_is_down_without_fallback(db, api, urrea):
    product = await _linked(db, stock="0")
    product_uuid = product.sicar_uuid
    urrea.error = UrreaError("caido")
    r = await api.post("/v1/orders", json=_order_body(product, 2))
    assert r.status_code == 503
    assert await _reserved(db, product_uuid) == Decimal("0")


async def test_order_uses_recent_answer_when_urrea_is_down(db, api, urrea):
    product = await _linked(db, stock="0")
    backorder_service._remember(f"C{product.id}", 4, True)
    # Mas vieja que la ventana "fresca": hay que llamar, URREA falla, sirve de respaldo.
    backorder_service._cache[f"C{product.id}"] = [(at - backorder_service.FRESH_SECONDS - 1, q, s) for at, q, s in backorder_service._cache[f"C{product.id}"]]
    urrea.error = UrreaError("caido")
    r = await api.post("/v1/orders", json=_order_body(product, 2))
    assert r.status_code == 201, r.text


async def test_flag_off_keeps_previous_behavior(db, api, urrea, monkeypatch):
    monkeypatch.setattr(settings, "URREA_BACKORDER_ENABLED", False)
    product = await _linked(db, stock="1")
    r = await api.post("/v1/orders", json=_order_body(product, 2))
    assert r.status_code == 409
    assert urrea.calls == []


async def test_lock_rejects_when_shortfall_grew_after_precheck(db):
    product = await _linked(db, stock="1")
    with pytest.raises(HTTPException) as exc:
        await validate_cart_items(db, [product.sicar_uuid], {product.sicar_uuid: 4}, {product.sicar_uuid: Decimal("2")})
    assert exc.value.status_code == 409
    products = await validate_cart_items(db, [product.sicar_uuid], {product.sicar_uuid: 3}, {product.sicar_uuid: Decimal("2")})
    assert product.sicar_uuid in products


# --- Deriva, carrito y admin

async def test_open_backorders_are_not_stock_drift(db, api, urrea):
    product = await _linked(db, stock="0")
    r = await api.post("/v1/orders", json=_order_body(product, 3))
    assert r.status_code == 201, r.text

    sub = backorder_service.open_backorder_quantities_subquery()
    rows = dict((await db.execute(select(sub.c.product_uuid, sub.c.qty).where(sub.c.product_uuid == product.sicar_uuid))).all())
    assert rows == {product.sicar_uuid: Decimal("3")}
    # reserved (3) - sobre pedido abierto (3) <= stock (0): no es deriva.
    p = await _reload(db, product)
    assert p.reserved - rows[product.sicar_uuid] <= p.stock

    await db.execute(text("UPDATE orders SET accepted_at = now() WHERE uuid = :u"), {"u": r.json()["orderUuid"]})
    rows = (await db.execute(select(sub.c.qty).where(sub.c.product_uuid == product.sicar_uuid))).all()
    assert rows == []


async def test_cart_shows_backorder_quantity(db, api):
    product = await _linked(db, stock="2")
    body = (await api.put("/v1/cart", json={"items": [{"uuid": product.sicar_uuid, "quantity": 5}]})).json()
    line = body["items"][0]
    assert (line["availability"], line["backorderQuantity"], line["backorderLeadDays"]) == ("IN_STOCK", 3.0, 5)


async def test_admin_pending_purchases_and_has_backorder_filter(db, api, urrea):
    from tests.conftest import admin_headers

    product = await _linked(db, stock="0", multiple=6)
    paid = (await api.post("/v1/orders", json=_order_body(product, 4))).json()["orderUuid"]
    unpaid = (await api.post("/v1/orders", json=_order_body(product, 3))).json()["orderUuid"]
    await db.execute(text("UPDATE orders SET status = 'PAID' WHERE uuid = :u"), {"u": paid})

    headers = await admin_headers(db, role="staff")
    docs = (await api.get("/v1/admin/suppliers/urrea/pending-purchases", headers=headers)).json()["docs"]
    mine = [d for d in docs if d["productUuid"] == product.sicar_uuid]
    assert len(mine) == 1
    doc = mine[0]
    # Solo la orden pagada; 4 piezas -> se piden 6 (multiplo de URREA).
    assert (Decimal(str(doc["backorderQuantity"])), Decimal(str(doc["suggestedPurchaseQuantity"])), doc["ordersCount"]) == (
        Decimal("4"), Decimal("6"), 1)
    assert doc["supplierCode"] == f"C{product.id}"

    listed = (await api.get("/v1/admin/orders", params={"hasBackorder": "true", "limit": 200}, headers=headers)).json()["docs"]
    uuids = {o["uuid"] for o in listed}
    assert {paid, unpaid} <= uuids
    assert all(o["hasBackorder"] for o in listed)
    without = (await api.get("/v1/admin/orders", params={"hasBackorder": "false", "limit": 200}, headers=headers)).json()["docs"]
    assert not ({paid, unpaid} & {o["uuid"] for o in without})
