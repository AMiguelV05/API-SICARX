"""Cubre admin_service.refund_order - reembolsos parciales/totales sobre una orden PAID.
No toca Order.status ni Product.stock/reserved (evento solo monetario), ver CLAUDE.md,
"Reembolsos parciales". payment_service.refund_payment se monkeypatchea: nunca debe
llamar a Mercado Pago de verdad en un test."""
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.models.order import Order
from app.models.refund import Refund
from app.services import admin_service
from app.services.admin_auth_service import create_admin_user
from app.schemas.admin_auth import AdminUserCreate
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


async def _make_super_admin(db):
    return await create_admin_user(db, AdminUserCreate(email="refunds@example.com", name="Admin", password="correcta123", role="super_admin"))


async def test_partial_refund_creates_refund_row_leaves_order_paid(db, monkeypatch):
    order = _make_paid_order()
    db.add(order)
    await db.flush()
    admin = await _make_super_admin(db)

    mock_refund = AsyncMock(return_value={"id": 999, "amount": 50.0})
    monkeypatch.setattr(admin_service.payment_service, "refund_payment", mock_refund)

    refund = await admin_service.refund_order(db, order.uuid, 50.0, "Producto dañado", admin)
    await db.flush()
    await db.refresh(order)

    assert refund.amount == Decimal("50.0")
    assert refund.mp_refund_id == "999"
    assert refund.issued_by_admin_id == admin.id
    assert order.status == "PAID"  # nunca lo toca un reembolso parcial

    mock_refund.assert_awaited_once()
    _, kwargs = mock_refund.call_args
    assert kwargs.get("amount") == Decimal("50.0")


async def test_refund_amount_exceeding_remaining_raises_400(db, monkeypatch):
    order = _make_paid_order(total="100.00")
    db.add(order)
    await db.flush()
    admin = await _make_super_admin(db)

    mock_refund = AsyncMock(return_value={"id": 1, "amount": 100.0})
    monkeypatch.setattr(admin_service.payment_service, "refund_payment", mock_refund)

    # Primer reembolso de 80 dentro del limite (100 total).
    await admin_service.refund_order(db, order.uuid, 80.0, "Primer reembolso", admin)
    await db.flush()

    # Un segundo reembolso de 30 excederia lo restante (20).
    with pytest.raises(HTTPException) as exc_info:
        await admin_service.refund_order(db, order.uuid, 30.0, "Excede", admin)
    assert exc_info.value.status_code == 400
    mock_refund.assert_awaited_once()  # el segundo intento nunca llego a llamar a Mercado Pago


async def test_refund_on_non_paid_order_raises_409(db):
    order = _make_paid_order()
    order.status = "TO_PAY"
    db.add(order)
    await db.flush()
    admin = await _make_super_admin(db)

    with pytest.raises(HTTPException) as exc_info:
        await admin_service.refund_order(db, order.uuid, 10.0, "No deberia proceder", admin)
    assert exc_info.value.status_code == 409


async def test_refund_on_order_without_mp_payment_id_raises_409(db):
    order = _make_paid_order(mp_payment_id=None)
    db.add(order)
    await db.flush()
    admin = await _make_super_admin(db)

    with pytest.raises(HTTPException) as exc_info:
        await admin_service.refund_order(db, order.uuid, 10.0, "Sin pago asociado", admin)
    assert exc_info.value.status_code == 409
