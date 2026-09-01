"""Cubre las dos ramas de finalize_order_payment agregadas para cerrar la misma clase de
bug que "charged_back" ya tenia: mp_status == "in_mediation" (etapa previa a un
contracargo formal) y mp_status == "refunded" (reembolso registrado directamente en el
dashboard de Mercado Pago, fuera de POST /admin/orders/{uuid}/refund). Ninguna de las dos
debe tocar Order.status/Product.stock/reserved. Ver CLAUDE.md, "Contracargos de Mercado
Pago"."""
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock

from fastapi import BackgroundTasks
from sqlalchemy import select, func

from app.models.order import Order
from app.models.refund import Refund
from app.services.order_history_service import finalize_order_payment
from app.services import order_history_service
from tests.conftest import make_product


def _make_paid_order(*, total="200.00", mp_payment_id="mp-123") -> Order:
    return Order(
        client_account_id=None,
        guest_email="cliente@example.com",
        sicar_order_id=str(uuid.uuid4()),
        status="PAID",
        total=Decimal(total),
        total_quantity=Decimal("2"),
        delivery_info={"deliveryType": "PICKUP", "contactInfo": {"email": "cliente@example.com", "name": "Cliente"}},
        items=[],
        mp_payment_id=mp_payment_id,
        mp_status="approved",
    )


async def test_in_mediation_marks_disputed_and_alerts_without_touching_status(db, monkeypatch):
    order = _make_paid_order()
    db.add(order)
    await db.flush()

    mock_alert = AsyncMock()
    monkeypatch.setattr(order_history_service.admin_notification_service, "notify_admin_payment_in_mediation", mock_alert)

    mp_payment = {"id": order.mp_payment_id, "status": "in_mediation", "status_detail": "in_process"}
    updated = await finalize_order_payment(db, order, mp_payment, BackgroundTasks())

    assert updated.status == "PAID"
    assert updated.disputed_at is not None
    mock_alert.assert_awaited_once()


async def test_repeated_in_mediation_notification_does_not_realert(db, monkeypatch):
    order = _make_paid_order()
    db.add(order)
    await db.flush()

    mock_alert = AsyncMock()
    monkeypatch.setattr(order_history_service.admin_notification_service, "notify_admin_payment_in_mediation", mock_alert)

    mp_payment = {"id": order.mp_payment_id, "status": "in_mediation", "status_detail": "in_process"}
    await finalize_order_payment(db, order, mp_payment, BackgroundTasks())
    await db.refresh(order)
    await finalize_order_payment(db, order, mp_payment, BackgroundTasks())

    mock_alert.assert_awaited_once()  # no se repite en un reintento del webhook


async def test_out_of_band_refund_creates_refund_row_and_alerts_without_touching_status(db, monkeypatch):
    order = _make_paid_order(total="150.00")
    db.add(order)
    await db.flush()

    mock_alert = AsyncMock()
    monkeypatch.setattr(order_history_service.admin_notification_service, "notify_admin_out_of_band_refund", mock_alert)

    mp_payment = {"id": order.mp_payment_id, "status": "refunded", "status_detail": "refunded", "transaction_amount_refunded": 150.0}
    updated = await finalize_order_payment(db, order, mp_payment, BackgroundTasks())

    assert updated.status == "PAID"  # evento solo monetario/de registro, no cancela nada
    mock_alert.assert_awaited_once()

    refund = await db.scalar(select(Refund).where(Refund.order_id == order.id))
    assert refund is not None
    assert refund.amount == Decimal("150.0")
    assert refund.issued_by_admin_id is None
    assert refund.mp_refund_id is None


async def test_repeated_refunded_notification_does_not_duplicate_refund_row(db, monkeypatch):
    order = _make_paid_order(total="150.00")
    db.add(order)
    await db.flush()

    mock_alert = AsyncMock()
    monkeypatch.setattr(order_history_service.admin_notification_service, "notify_admin_out_of_band_refund", mock_alert)

    mp_payment = {"id": order.mp_payment_id, "status": "refunded", "status_detail": "refunded", "transaction_amount_refunded": 150.0}
    await finalize_order_payment(db, order, mp_payment, BackgroundTasks())
    await db.refresh(order)
    await finalize_order_payment(db, order, mp_payment, BackgroundTasks())

    mock_alert.assert_awaited_once()
    count = await db.scalar(select(func.count()).select_from(Refund).where(Refund.order_id == order.id))
    assert count == 1


async def test_out_of_band_refund_does_not_touch_stock(db, monkeypatch):
    product = make_product(stock=Decimal("10"), reserved=Decimal("0"))
    db.add(product)
    await db.flush()

    order = _make_paid_order()
    order.items = [{"uuid": product.sicar_uuid, "quantity": "2", "sku": product.sku, "description": product.name}]
    db.add(order)
    await db.flush()

    monkeypatch.setattr(order_history_service.admin_notification_service, "notify_admin_out_of_band_refund", AsyncMock())

    mp_payment = {"id": order.mp_payment_id, "status": "refunded", "status_detail": "refunded"}
    await finalize_order_payment(db, order, mp_payment, BackgroundTasks())

    await db.refresh(product)
    assert product.stock == Decimal("10")  # un reembolso no restaura/ajusta inventario automaticamente
