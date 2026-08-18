"""Cubre prepare_local_cancellation - el punto unico compartido por /cancel, DELETE y la
rama rejected/cancelled de finalize_order_payment. Ver CLAUDE.md, "Local-first order
cancellation"."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from app.models.order import Order, SicarSyncOutbox
from app.services.order_history_service import prepare_local_cancellation
from tests.conftest import make_product


def _make_order(*, product_uuid: str, status: str = "PAID", accepted_at=None) -> Order:
    return Order(
        client_account_id=None,
        guest_email="cliente@example.com",
        sicar_order_id=str(uuid.uuid4()),
        status=status,
        dispatch_status="PENDING_ACCEPTANCE" if accepted_at is None else "PENDING",
        branch_id=151456,
        total=Decimal("200.00"),
        total_quantity=Decimal("2"),
        delivery_info={"deliveryType": "PICKUP", "contactInfo": {"email": "cliente@example.com", "name": "Cliente"}},
        items=[{"uuid": product_uuid, "quantity": "2", "sku": "SKU-1", "description": "Producto"}],
        accepted_at=accepted_at,
    )


async def test_cancel_before_acceptance_releases_reservation_no_outbox_row(db):
    """Sicar X nunca supo de esta orden (accepted_at is None) - solo se libera la reserva
    local, sin encolar nada hacia Sicar X."""
    product = make_product(stock=Decimal("10"), reserved=Decimal("2"))
    db.add(product)
    await db.flush()

    order = _make_order(product_uuid=product.sicar_uuid, status="PAID", accepted_at=None)
    db.add(order)
    await db.flush()

    updated = await prepare_local_cancellation(db, order)
    await db.flush()
    await db.refresh(product)

    assert updated.status == "CANCELLED"
    assert product.reserved == Decimal("0")
    assert product.stock == Decimal("10")  # stock (verdad de Sicar) no se toca aqui

    outbox_count = await db.scalar(select(func.count()).select_from(SicarSyncOutbox).where(SicarSyncOutbox.order_id == order.id))
    assert outbox_count == 0


async def test_cancel_after_acceptance_enqueues_cancel_outbox_leaves_stock_untouched(db):
    """Orden ya aceptada (Sicar X ya desconto el stock real) - la restauracion de
    Product.stock queda diferida al exito del outbox CANCEL, no ocurre aqui."""
    product = make_product(stock=Decimal("8"), reserved=Decimal("0"))  # ya descontado por ACCEPT
    db.add(product)
    await db.flush()

    order = _make_order(product_uuid=product.sicar_uuid, status="PAID", accepted_at=datetime.now(timezone.utc))
    db.add(order)
    await db.flush()

    updated = await prepare_local_cancellation(db, order, cash_register_uuid="test-caja")
    await db.flush()
    await db.refresh(product)

    assert updated.status == "CANCELLED"
    assert product.stock == Decimal("8")  # sin cambio local todavia - lo hace el worker al exito
    assert product.reserved == Decimal("0")  # ya se habia liberado al aceptar, nada que hacer aqui

    row = await db.scalar(select(SicarSyncOutbox).where(SicarSyncOutbox.order_id == order.id))
    assert row is not None
    assert row.action == "CANCEL"
    assert row.status == "PENDING"
    assert row.cash_register_uuid == "test-caja"


async def test_cancel_already_cancelled_order_raises_409(db):
    product = make_product(stock=Decimal("10"), reserved=Decimal("0"))
    db.add(product)
    await db.flush()

    order = _make_order(product_uuid=product.sicar_uuid, status="CANCELLED")
    db.add(order)
    await db.flush()

    with pytest.raises(HTTPException) as exc_info:
        await prepare_local_cancellation(db, order)
    assert exc_info.value.status_code == 409


async def test_cancel_require_status_mismatch_raises_409(db):
    """DELETE /orders/{id} exige require_status="TO_PAY" dentro del lock - una orden ya
    PAID (p. ej. /pay gano la carrera) debe rechazar, no cancelar silenciosamente."""
    product = make_product(stock=Decimal("10"), reserved=Decimal("2"))
    db.add(product)
    await db.flush()

    order = _make_order(product_uuid=product.sicar_uuid, status="PAID")
    db.add(order)
    await db.flush()

    with pytest.raises(HTTPException) as exc_info:
        await prepare_local_cancellation(db, order, require_status="TO_PAY")
    assert exc_info.value.status_code == 409
