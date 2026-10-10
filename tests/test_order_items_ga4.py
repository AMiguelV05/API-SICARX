"""Renglones de pedido con los campos numericos que pide GA4 (productUuid/unitPrice/lineTotal),
derivados al serializar de lo que build_order_payload ya guarda - ver
schemas/orders.enrich_order_items."""
import uuid
from decimal import Decimal

from app.models.order import Order
from app.schemas.orders import enrich_order_items


def _order(items) -> Order:
    return Order(
        uuid=str(uuid.uuid4()),
        client_account_id=None,
        guest_email="invitado@example.com",
        sicar_order_id=str(uuid.uuid4()),
        status="PAID",
        dispatch_status="PENDING_ACCEPTANCE",
        branch_id=1,
        total=Decimal("230.00"),
        subtotal=Decimal("250.00"),
        discount_amount=Decimal("20.00"),
        coupon_code="PROMO",
        total_quantity=Decimal("3"),
        delivery_info={"deliveryType": "PICKUP", "contactInfo": {"email": "invitado@example.com", "name": "Invitado"}},
        items=items,
    )


def test_enrich_adds_numeric_fields_without_mutating_input():
    items = [{"uuid": "p-1", "sku": "A", "quantity": "2", "priceTax": "100.00", "priceBaseTax": "100.00", "amountTax": "200.00"}]
    enriched = enrich_order_items(items)

    assert enriched[0]["productUuid"] == "p-1"
    assert enriched[0]["unitPrice"] == 100.0
    assert enriched[0]["lineTotal"] == 200.0
    assert enriched[0]["quantity"] == "2"  # el contrato existente no cambia
    assert "productUuid" not in items[0]


def test_old_line_without_prices_gets_nulls():
    enriched = enrich_order_items([{"uuid": "p-1", "quantity": "1"}])
    assert enriched[0]["unitPrice"] is None and enriched[0]["lineTotal"] is None


async def test_guest_endpoint_returns_ga4_fields_and_totals(api, db):
    order = _order([{"uuid": "p-1", "sku": "A", "description": "Martillo", "quantity": "1", "priceTax": "50.00", "amountTax": "50.00"},
                    {"uuid": "p-2", "sku": "B", "description": "Clavo", "quantity": "2", "priceTax": "100.00", "amountTax": "200.00"}])
    db.add(order)
    await db.flush()

    response = await api.get(f"/v1/orders/guest/{order.uuid}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [(i["productUuid"], i["unitPrice"], i["lineTotal"]) for i in body["items"]] == [("p-1", 50.0, 50.0), ("p-2", 100.0, 200.0)]
    assert body["total"] == 230.0 and body["subtotal"] == 250.0
    assert body["discountAmount"] == 20.0 and body["couponCode"] == "PROMO"

