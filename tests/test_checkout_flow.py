"""Cubre cart validation -> order creation -> stock reservation, replicando (a nivel de
servicio, sin pasar por HTTP/Mercado Pago) lo que routes/orders.py::create_order hace
antes del unico commit - ver CLAUDE.md, "Request flow: placing an order"."""
from decimal import Decimal

from app.models.order import Order
from app.services.order_service import validate_cart_items, build_order_payload
from app.services.product_stock_service import apply_reserved_deltas
from app.services.order_history_service import create_local_order
from tests.conftest import make_product


async def test_checkout_reserves_stock_and_creates_to_pay_order(db):
    product = make_product(stock=Decimal("10"), reserved=Decimal("0"), price=Decimal("199.99"))
    db.add(product)
    await db.flush()

    quantities = {product.sicar_uuid: 3}
    local_products = await validate_cart_items(db, [product.sicar_uuid], quantities)

    payload = build_order_payload(
        local_products=local_products,
        quantities=quantities,
        delivery_info={"deliveryType": "PICKUP", "contactInfo": {"email": "cliente@example.com", "name": "Cliente"}},
        branch_id=151456,
        price_list_uuid="test-price-list",
        content_id="test-content-id",
    )

    assert payload["ecOrderDto"]["total"] == "599.97"  # 199.99 * 3

    deltas = [(item["uuid"], Decimal(item["quantity"])) for item in payload["ecOrderDto"]["products"]]
    await apply_reserved_deltas(db, deltas)

    order = await create_local_order(
        db=db,
        client_account_id=None,
        order_payload_dict=payload,
        local_products=local_products,
        guest_email="cliente@example.com",
    )
    await db.flush()
    await db.refresh(product)

    assert order.status == "TO_PAY"
    assert order.total == Decimal("599.97")
    assert order.guest_email == "cliente@example.com"
    assert product.reserved == Decimal("3")
    assert product.available_stock == Decimal("7")


async def test_checkout_rejects_insufficient_stock_before_reserving_anything(db):
    product = make_product(stock=Decimal("2"), reserved=Decimal("0"), price=Decimal("50.00"))
    db.add(product)
    await db.flush()

    from fastapi import HTTPException
    import pytest

    with pytest.raises(HTTPException) as exc_info:
        await validate_cart_items(db, [product.sicar_uuid], {product.sicar_uuid: 5})
    assert exc_info.value.status_code == 409

    await db.refresh(product)
    assert product.reserved == Decimal("0")  # nada se reservo - la validacion fallo antes


async def test_checkout_two_line_order_sums_correctly_in_decimal(db):
    """Aritmetica en Decimal, no float - ver CLAUDE.md sobre el riesgo de arrastrar ruido
    de representacion binaria al sumar un carrito multi-linea."""
    p1 = make_product(stock=Decimal("10"), price=Decimal("10.10"))
    p2 = make_product(stock=Decimal("10"), price=Decimal("0.30"))
    db.add_all([p1, p2])
    await db.flush()

    quantities = {p1.sicar_uuid: 3, p2.sicar_uuid: 1}
    local_products = await validate_cart_items(db, [p1.sicar_uuid, p2.sicar_uuid], quantities)
    payload = build_order_payload(
        local_products=local_products,
        quantities=quantities,
        delivery_info={"deliveryType": "PICKUP", "contactInfo": {"email": "a@b.com", "name": "A"}},
        branch_id=151456,
        price_list_uuid="test-price-list",
        content_id="test-content-id",
    )
    # 10.10*3 + 0.30*1 = 30.60 exacto - un acumulado en float podria arrastrar ruido binario.
    assert payload["ecOrderDto"]["total"] == "30.60"
