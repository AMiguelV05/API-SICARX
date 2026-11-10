"""Cotizacion y cobro de envio en el checkout: POST /shipping/quotes, `shipping` en POST /orders
y el uso de la cotizacion al generar la guia en el admin. envia.com se mockea en la frontera
del servicio (shipping_service.fetch_rates / generate_shipping_label)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from app.core.config import settings
from app.core.security import create_client_token
from app.models.client import ClientAddress
from app.models.order import Order
from app.models.shipping_quote import ShippingQuote
from app.schemas.admin import ShippingGenerateRequest, ShippingQuoteRequest
from app.services import admin_service, order_status_notification_service, shipping_service
from app.services.order_service import build_order_payload
from tests.conftest import make_client_account, make_product

ADDRESS = {
    "street": "Av. Juárez", "extNumber": "100", "neighborhood": "Centro", "city": "Monterrey",
    "county": "Monterrey", "state": "Nuevo León", "zipCode": "64000",
}
CONTACT = {"name": "Ana López", "phone": "8112345678", "email": "ana@example.com"}

RATES = [
    {"carrier": "fedex", "service": "ground", "serviceDescription": "FedEx Ground", "deliveryEstimate": "3 días", "totalPrice": 150.0, "currency": "MXN"},
    {"carrier": "fedex", "service": "express", "serviceDescription": "FedEx Express", "deliveryEstimate": "1 día", "totalPrice": 300.0, "currency": "MXN"},
    {"carrier": "dhl", "service": "x", "totalPrice": 100.0, "currency": "MXN"},
    {"carrier": "estafeta", "service": "x", "totalPrice": 180.0, "currency": "MXN"},
    {"carrier": "redpack", "service": "x", "totalPrice": 200.0, "currency": "MXN"},
    {"carrier": "paquetexpress", "service": "x", "totalPrice": 210.0, "currency": "MXN"},
    {"carrier": "ups", "service": "x", "totalPrice": 400.0, "currency": "MXN"},
    {"carrier": "gratis", "service": "x", "totalPrice": 0, "currency": "MXN"},  # se descarta
]


@pytest.fixture(autouse=True)
def checkout_settings(monkeypatch):
    monkeypatch.setattr(settings, "SHIPPING_CHECKOUT_ENABLED", True)
    monkeypatch.setattr(settings, "SHIPPING_MARGIN_PERCENT", Decimal("10"))
    monkeypatch.setattr(settings, "SHIPPING_MARGIN_FIXED_MXN", Decimal("5"))
    monkeypatch.setattr(settings, "SHIPPING_QUOTE_CARRIERS", "")


class FakeEnvia:
    def __init__(self):
        self.rates = list(RATES)
        self.error: Exception | None = None
        self.calls: list[dict] = []

    async def __call__(self, origin, destination, packages, *, context, carriers=None):
        self.calls.append({"destination": destination, "packages": packages, "carriers": carriers})
        if self.error:
            raise self.error
        return self.rates


@pytest.fixture
def envia(monkeypatch):
    fake = FakeEnvia()
    monkeypatch.setattr(shipping_service, "fetch_rates", fake)
    return fake


async def _products(db, *, with_dims=True, n=1):
    products = []
    for _ in range(n):
        extra = dict(package_weight_kg=Decimal("2.000"), package_length_cm=Decimal("30"), package_width_cm=Decimal("20"),
                     package_height_cm=Decimal("10")) if with_dims else {}
        products.append(make_product(price=Decimal("100.00"), stock=Decimal("50"), **extra))
    db.add_all(products)
    await db.flush()
    return products


def _cart(products, qty=2):
    return [{"uuid": p.sicar_uuid, "quantity": qty} for p in products]


async def _quote(api, products, qty=2, address=None, headers=None, address_uuid=None):
    body = {"products": _cart(products, qty)}
    if address_uuid:
        body["addressUuid"] = address_uuid
    else:
        body["address"] = address or ADDRESS
    r = await api.post("/v1/shipping/quotes", json=body, headers=headers or {})
    assert r.status_code == 201, r.text
    return r.json()


def _order_body(products, qty=2, shipping=None, address=None, delivery_type="DELIVERYMAN"):
    delivery = {"contactInfo": CONTACT, "deliveryType": delivery_type}
    if delivery_type == "DELIVERYMAN":
        delivery["address"] = address or ADDRESS
    body = {"products": _cart(products, qty), "deliveryInfo": delivery}
    if shipping is not None:
        body["shipping"] = shipping
    return body


# --- POST /shipping/quotes

async def test_quote_shortlists_cheapest_per_carrier_with_margin_and_reuses(api, db, envia):
    products = await _products(db)
    quote = await _quote(api, products, qty=3)

    assert quote["status"] == "QUOTED" and quote["reason"] is None
    # Mas barata por paqueteria, top 5, precio = tarifa * 1.10 + 5
    assert [(o["carrier"], o["price"]) for o in quote["options"]] == [
        ("dhl", 115.0), ("fedex", 170.0), ("estafeta", 203.0), ("redpack", 225.0), ("paquetexpress", 236.0),
    ]
    assert [o["optionId"] for o in quote["options"]] == ["1", "2", "3", "4", "5"]
    assert "carrierPrice" not in quote["options"][0]

    [call] = envia.calls
    assert call["destination"]["postalCode"] == "64000" and call["destination"]["state"] == "NLE"
    assert len(call["packages"]) == 1 and call["packages"][0]["weight"] == 6.0  # 3 piezas de 2 kg en una caja

    again = await _quote(api, products, qty=3)
    assert again["quoteUuid"] == quote["quoteUuid"] and len(envia.calls) == 1  # reusada, sin llamar a envia


async def test_quote_missing_dimensions_skips_envia(api, db, envia):
    with_dims = await _products(db)
    without = await _products(db, with_dims=False)
    quote = await _quote(api, with_dims + without)
    assert quote["status"] == "NOT_QUOTABLE" and quote["reason"] == "MISSING_DIMENSIONS"
    assert quote["missingDimensions"] == [without[0].sicar_uuid]
    assert quote["options"] == [] and envia.calls == []


@pytest.mark.parametrize("error,rates,reason", [
    (HTTPException(status_code=502, detail="envia caido"), None, "UPSTREAM_ERROR"),
    (httpx.ConnectError("sin red"), None, "UPSTREAM_ERROR"),
    (None, [], "NO_RATES"),
])
async def test_quote_without_rates_is_not_quotable(api, db, envia, error, rates, reason):
    envia.error = error
    if rates is not None:
        envia.rates = rates
    products = await _products(db)
    quote = await _quote(api, products)
    assert (quote["status"], quote["reason"]) == ("NOT_QUOTABLE", reason)


async def test_quote_rejects_unavailable_product(api, db, envia):
    [p] = await _products(db)
    p.is_active = False
    await db.flush()
    r = await api.post("/v1/shipping/quotes", json={"products": _cart([p]), "address": ADDRESS})
    assert r.status_code == 409


# --- POST /orders

async def test_order_charges_chosen_option(api, db, envia):
    products = await _products(db)
    quote = await _quote(api, products)
    fedex = next(o for o in quote["options"] if o["carrier"] == "fedex")

    r = await api.post("/v1/orders", json=_order_body(products, shipping={"quoteUuid": quote["quoteUuid"], "optionId": fedex["optionId"]}))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["deliveryCost"] == 170.0 and body["total"] == 370.0  # 2 x 100 + 170

    order = await db.scalar(select(Order).where(Order.uuid == body["orderUuid"]))
    assert order.total == Decimal("370.00") and order.subtotal == Decimal("200.00") and order.delivery_cost == Decimal("170.00")
    assert order.shipping_quote["status"] == "QUOTED"
    assert (order.shipping_quote["carrier"], order.shipping_quote["service"], order.shipping_quote["carrierPrice"]) == ("fedex", "ground", "150.0")
    assert order.shipping_quote["packages"][0]["weightKg"] == 4.0

    public = (await api.get(f"/v1/orders/guest/{order.uuid}")).json()
    assert public["deliveryCost"] == 170.0
    assert public["shippingQuote"]["carrier"] == "fedex" and "carrierPrice" not in public["shippingQuote"]


async def test_build_order_payload_adds_shipping_after_discount():
    p = make_product(price=Decimal("100.00"))
    payload = build_order_payload({p.sicar_uuid: p}, {p.sicar_uuid: 1}, {}, 1, "pl", "c",
                                  discount_amount=Decimal("50.00"), shipping_amount=Decimal("30.00"))
    dto = payload["ecOrderDto"]
    assert (dto["subtotal"], dto["discountAmount"], dto["shippingAmount"], dto["total"]) == ("100.00", "50.00", "30.00", "80.00")


async def test_order_requires_shipping_when_quotable(api, db, envia):
    products = await _products(db)
    r = await api.post("/v1/orders", json=_order_body(products))
    assert r.status_code == 409 and "cotízalo" in r.json()["detail"]


async def _expire(db, quote_uuid):
    await db.execute(update(ShippingQuote).where(ShippingQuote.uuid == quote_uuid).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))


@pytest.mark.parametrize("case,fragment", [
    ("expired", "expiró"),
    ("cart_changed", "carrito cambió"),
    ("address_changed", "dirección de entrega cambió"),
    ("bad_option", "opción de envío elegida no existe"),
    ("unknown_quote", "no es válida"),
])
async def test_order_rejects_quote_that_does_not_match(api, db, envia, case, fragment):
    products = await _products(db)
    quote = await _quote(api, products)
    shipping = {"quoteUuid": quote["quoteUuid"], "optionId": "1"}
    kwargs = {}
    if case == "expired":
        await _expire(db, quote["quoteUuid"])
    elif case == "cart_changed":
        kwargs["qty"] = 3
    elif case == "address_changed":
        kwargs["address"] = {**ADDRESS, "zipCode": "64001"}
    elif case == "bad_option":
        shipping["optionId"] = "99"
    elif case == "unknown_quote":
        shipping["quoteUuid"] = "no-existe"
    r = await api.post("/v1/orders", json=_order_body(products, shipping=shipping, **kwargs))
    assert r.status_code == 409 and fragment in r.json()["detail"], r.text
    assert await db.scalar(select(Order).where(Order.guest_email == CONTACT["email"])) is None


async def test_account_quote_cannot_be_used_by_another_caller(api, db, envia):
    client = make_client_account()
    db.add(client)
    await db.flush()
    address = ClientAddress(client_account_id=client.id, street=ADDRESS["street"], ext_number="100", neighborhood="Centro",
                            city="Monterrey", county="Monterrey", state="Nuevo León", zip_code="64000")
    db.add(address)
    await db.flush()
    headers = {"X-Client-Token": create_client_token(client.uuid)}
    products = await _products(db)

    quote = await _quote(api, products, headers=headers, address_uuid=address.uuid)
    assert quote["status"] == "QUOTED"

    shipping = {"quoteUuid": quote["quoteUuid"], "optionId": "1"}
    guest_body = _order_body(products, shipping=shipping)
    account_body = _order_body(products, shipping=shipping)
    del account_body["deliveryInfo"]["address"]
    account_body["deliveryInfo"]["addressUuid"] = address.uuid

    # Un invitado con el mismo carrito y la misma direccion no puede usar la cotizacion de la cuenta.
    # (El 409 hace rollback en la sesion compartida: los bodies se arman antes.)
    r = await api.post("/v1/orders", json=guest_body)
    assert r.status_code == 409

    r = await api.post("/v1/orders", json=account_body, headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["deliveryCost"] == 115.0


async def test_missing_dimensions_order_is_free_and_manual(api, db, envia):
    products = await _products(db, with_dims=False)
    r = await api.post("/v1/orders", json=_order_body(products))
    assert r.status_code == 201, r.text
    assert r.json()["deliveryCost"] is None and r.json()["total"] == 200.0
    order = await db.scalar(select(Order).where(Order.uuid == r.json()["orderUuid"]))
    assert order.delivery_cost is None
    assert order.shipping_quote == {"quoteUuid": None, "status": "MANUAL", "reason": "MISSING_DIMENSIONS"}


async def test_not_quotable_quote_lets_order_through_without_cost(api, db, envia):
    envia.rates = []
    products = await _products(db)
    quote = await _quote(api, products)
    r = await api.post("/v1/orders", json=_order_body(products, shipping={"quoteUuid": quote["quoteUuid"]}))
    assert r.status_code == 201, r.text
    order = await db.scalar(select(Order).where(Order.uuid == r.json()["orderUuid"]))
    assert order.delivery_cost is None and order.total == Decimal("200.00")
    assert order.shipping_quote["status"] == "MANUAL" and order.shipping_quote["reason"] == "NO_RATES"


async def test_flag_off_keeps_previous_behavior(api, db, envia, monkeypatch):
    monkeypatch.setattr(settings, "SHIPPING_CHECKOUT_ENABLED", False)
    products = await _products(db)
    r = await api.post("/v1/orders", json=_order_body(products))
    assert r.status_code == 201, r.text
    order = await db.scalar(select(Order).where(Order.uuid == r.json()["orderUuid"]))
    assert order.delivery_cost is None and order.shipping_quote is None and order.total == Decimal("200.00")


async def test_pickup_rejects_shipping(api, db, envia):
    products = await _products(db)
    r = await api.post("/v1/orders", json=_order_body(products, delivery_type="PICKUP", shipping={"quoteUuid": "x"}))
    assert r.status_code == 400


# --- Admin: la guia usa la cotizacion guardada

async def _paid_complete_order(api, db, envia) -> Order:
    products = await _products(db)
    quote = await _quote(api, products)
    r = await api.post("/v1/orders", json=_order_body(products, shipping={"quoteUuid": quote["quoteUuid"], "optionId": "2"}))
    assert r.status_code == 201, r.text
    order = await db.scalar(select(Order).where(Order.uuid == r.json()["orderUuid"]))
    order.dispatch_status = "COMPLETE"
    await db.flush()
    return order


async def test_admin_generate_defaults_to_checkout_choice(api, db, envia, monkeypatch):
    order = await _paid_complete_order(api, db, envia)
    captured = {}

    async def fake_generate(order_, boxes, carrier, service, origin_overrides=None):
        captured.update(boxes=boxes, carrier=carrier, service=service)
        return {"carrier": carrier, "service": service, "totalPrice": 160.0, "currency": "MXN",
                "weight": boxes[0]["weight"], "length": boxes[0]["length"], "width": boxes[0]["width"],
                "height": boxes[0]["height"], "generatedAt": datetime.now(timezone.utc).isoformat()}

    async def no_notify(order_):
        return None

    monkeypatch.setattr(shipping_service, "generate_shipping_label", fake_generate)
    monkeypatch.setattr(order_status_notification_service, "notify_order_dispatched", no_notify)

    await admin_service.generate_shipping_label(db, order.uuid, ShippingGenerateRequest())
    assert (captured["carrier"], captured["service"]) == ("fedex", "ground")
    # Las cajas que se empacaron al cotizar: 2 piezas de 30x20x10, con holgura -> 30x22x22.
    assert captured["boxes"] == [{"weight": 4.0, "length": 30.0, "width": 22.0, "height": 22.0}]
    assert order.dispatch_status == "DISPATCHED"


async def test_admin_quote_rejects_partial_dimensions(api, db, envia):
    order = await _paid_complete_order(api, db, envia)
    with pytest.raises(HTTPException) as exc:
        await admin_service.quote_shipping(db, order.uuid, ShippingQuoteRequest(weight=1))
    assert exc.value.status_code == 422


# --- fetch_rates: filtro de paqueterias

async def test_fetch_rates_only_queries_configured_carriers(monkeypatch):
    async def carriers():
        return ["fedex", "dhl", "estafeta"]

    asked = []

    def handler(request: httpx.Request):
        import json
        asked.append(json.loads(request.content)["carrier"])
        return httpx.Response(200, json={"meta": "rate", "data": [{"carrier": "dhl", "service": "x", "totalPrice": 99, "currency": "MXN"}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(shipping_service, "_fetch_available_carriers", carriers)
    monkeypatch.setattr(shipping_service.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))

    rates = await shipping_service.fetch_rates({}, {}, [], context="prueba", carriers=["DHL"])
    assert asked == ["dhl"] and rates[0]["totalPrice"] == 99.0
