"""Cubre finalize_order_payment - el punto unico donde /pay (sincrono) y el webhook de
Mercado Pago (unico camino para Wallet) convergen. Ver CLAUDE.md, "Payments with Mercado
Pago"."""
import uuid
from decimal import Decimal

from fastapi import BackgroundTasks

from app.models.order import Order
from app.services.order_history_service import finalize_order_payment
from tests.conftest import make_product


def _make_order(*, product_uuid: str, quantity: str = "2", total: str = "200.00") -> Order:
    return Order(
        client_account_id=None,
        guest_email="cliente@example.com",
        sicar_order_id=str(uuid.uuid4()),
        status="TO_PAY",
        dispatch_status="PENDING_ACCEPTANCE",
        branch_id=151456,
        total=Decimal(total),
        total_quantity=Decimal(quantity),
        delivery_info={"deliveryType": "PICKUP", "contactInfo": {"email": "cliente@example.com", "name": "Cliente"}},
        items=[{"uuid": product_uuid, "quantity": quantity, "sku": "SKU-1", "description": "Producto"}],
    )


async def test_approved_payment_transitions_to_paid_and_increments_sales_count(db):
    product = make_product(stock=Decimal("10"), reserved=Decimal("2"), sales_count=Decimal("0"))
    db.add(product)
    await db.flush()

    order = _make_order(product_uuid=product.sicar_uuid)
    db.add(order)
    await db.flush()

    mp_payment = {"id": 12345, "status": "approved", "status_detail": "accredited"}
    updated = await finalize_order_payment(db, order, mp_payment, BackgroundTasks())

    assert updated.status == "PAID"
    assert updated.mp_payment_id == "12345"
    assert updated.mp_status == "approved"

    await db.refresh(product)
    assert product.sales_count == Decimal("2")


async def test_retried_webhook_for_already_paid_order_does_not_double_count_sales(db):
    """Un reintento del webhook de Mercado Pago para una orden ya PAID no debe volver a
    incrementar sales_count - solo la transicion REAL TO_PAY->PAID lo hace."""
    product = make_product(stock=Decimal("10"), reserved=Decimal("2"), sales_count=Decimal("0"))
    db.add(product)
    await db.flush()

    order = _make_order(product_uuid=product.sicar_uuid)
    db.add(order)
    await db.flush()

    mp_payment = {"id": 12345, "status": "approved", "status_detail": "accredited"}
    await finalize_order_payment(db, order, mp_payment, BackgroundTasks())
    await db.refresh(product)
    assert product.sales_count == Decimal("2")

    # Reintento del mismo webhook (misma orden, mismo status "approved").
    await db.refresh(order)
    await finalize_order_payment(db, order, mp_payment, BackgroundTasks())
    await db.refresh(product)
    assert product.sales_count == Decimal("2")  # sin cambio, no se duplico


async def test_rejected_payment_cancels_order_and_releases_reservation(db):
    product = make_product(stock=Decimal("10"), reserved=Decimal("2"))
    db.add(product)
    await db.flush()

    order = _make_order(product_uuid=product.sicar_uuid)
    db.add(order)
    await db.flush()

    mp_payment = {"id": 999, "status": "rejected", "status_detail": "cc_rejected_insufficient_amount"}
    updated = await finalize_order_payment(db, order, mp_payment, BackgroundTasks())

    assert updated.status == "CANCELLED"

    await db.refresh(product)
    assert product.reserved == Decimal("0")  # la reserva de checkout se libero


async def test_pending_payment_keeps_order_in_to_pay(db):
    product = make_product(stock=Decimal("10"), reserved=Decimal("2"))
    db.add(product)
    await db.flush()

    order = _make_order(product_uuid=product.sicar_uuid)
    db.add(order)
    await db.flush()

    mp_payment = {"id": 777, "status": "pending", "status_detail": "pending_waiting_payment"}
    updated = await finalize_order_payment(db, order, mp_payment, BackgroundTasks())

    assert updated.status == "TO_PAY"
    await db.refresh(product)
    assert product.reserved == Decimal("2")  # sigue reservado, nada se libero ni se descuento
