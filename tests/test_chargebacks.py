"""Cubre la deteccion de contracargos ("Compra no reconocida"): el camino por defecto via
finalize_order_payment (topic "payment" de Mercado Pago, payment.status == "charged_back")
y el camino enriquecido via chargeback_service.process_chargeback_notification (topic
"chargebacks"). Ninguno debe tocar Order.status/Product.stock/reserved - evento solo
monetario/de registro, mismo criterio que un reembolso parcial. Ver CLAUDE.md,
"Contracargos de Mercado Pago"."""
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock

from fastapi import BackgroundTasks
from sqlalchemy import select

from app.models.order import Order
from app.models.chargeback import Chargeback
from app.services.order_history_service import finalize_order_payment
from app.services import chargeback_service, order_history_service


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


def _mp_chargeback_payload(*, chargeback_id="cb-1", mp_payment_id="mp-123", coverage_applied=None, amount=200.0):
    return {
        "id": chargeback_id,
        "payments": [mp_payment_id],
        "amount": amount,
        "reason": "general",
        "coverage_applied": coverage_applied,
        "coverage_elegible": True,  # nombre real del campo de Mercado Pago, ver chargeback_service.py
        "documentation_required": False,
        "date_documentation_deadline": None,
    }


async def test_charged_back_payment_marks_disputed_and_leaves_order_paid(db):
    order = _make_paid_order()
    db.add(order)
    await db.flush()

    mp_payment = {"id": order.mp_payment_id, "status": "charged_back", "status_detail": "in_process"}
    updated = await finalize_order_payment(db, order, mp_payment, BackgroundTasks())

    assert updated.status == "PAID"  # un contracargo no cambia el status local
    assert updated.disputed_at is not None
    assert updated.mp_status == "charged_back"


async def test_repeated_charged_back_notification_does_not_reset_disputed_at(db):
    """Un reintento del webhook para la misma orden ya marcada no debe repetir la alerta
    ni pisar el timestamp original."""
    order = _make_paid_order()
    db.add(order)
    await db.flush()

    mp_payment = {"id": order.mp_payment_id, "status": "charged_back", "status_detail": "in_process"}
    updated = await finalize_order_payment(db, order, mp_payment, BackgroundTasks())
    first_disputed_at = updated.disputed_at

    await db.refresh(order)
    updated_again = await finalize_order_payment(db, order, mp_payment, BackgroundTasks())
    assert updated_again.disputed_at == first_disputed_at


async def test_chargebacks_topic_notification_creates_row_and_marks_disputed(db):
    order = _make_paid_order(mp_payment_id="mp-555")
    db.add(order)
    await db.flush()

    await chargeback_service.process_chargeback_notification(
        db, _mp_chargeback_payload(mp_payment_id="mp-555"), BackgroundTasks()
    )

    await db.refresh(order)
    assert order.disputed_at is not None
    assert order.status == "PAID"

    chargeback = await db.scalar(select(Chargeback).where(Chargeback.order_id == order.id))
    assert chargeback is not None
    assert chargeback.status == "IN_PROCESS"
    assert chargeback.mp_chargeback_id == "cb-1"
    assert chargeback.amount == Decimal("200.0")


async def test_chargeback_lost_resolution_updates_status_without_touching_order(db):
    order = _make_paid_order(mp_payment_id="mp-777")
    db.add(order)
    await db.flush()

    await chargeback_service.process_chargeback_notification(
        db, _mp_chargeback_payload(chargeback_id="cb-2", mp_payment_id="mp-777"), BackgroundTasks()
    )
    await chargeback_service.process_chargeback_notification(
        db, _mp_chargeback_payload(chargeback_id="cb-2", mp_payment_id="mp-777", coverage_applied=False), BackgroundTasks()
    )

    chargeback = await db.scalar(select(Chargeback).where(Chargeback.mp_chargeback_id == "cb-2"))
    assert chargeback.status == "LOST"
    assert chargeback.resolved_at is not None

    await db.refresh(order)
    assert order.status == "PAID"  # perder un contracargo no cancela ni reembolsa nada automaticamente


async def test_chargeback_won_resolution_updates_status(db):
    order = _make_paid_order(mp_payment_id="mp-888")
    db.add(order)
    await db.flush()

    await chargeback_service.process_chargeback_notification(
        db, _mp_chargeback_payload(chargeback_id="cb-3", mp_payment_id="mp-888"), BackgroundTasks()
    )
    await chargeback_service.process_chargeback_notification(
        db, _mp_chargeback_payload(chargeback_id="cb-3", mp_payment_id="mp-888", coverage_applied=True), BackgroundTasks()
    )

    chargeback = await db.scalar(select(Chargeback).where(Chargeback.mp_chargeback_id == "cb-3"))
    assert chargeback.status == "WON"


async def test_repeated_notification_for_same_chargeback_id_does_not_duplicate_row(db):
    order = _make_paid_order(mp_payment_id="mp-999")
    db.add(order)
    await db.flush()

    payload = _mp_chargeback_payload(chargeback_id="cb-4", mp_payment_id="mp-999")
    await chargeback_service.process_chargeback_notification(db, payload, BackgroundTasks())
    await chargeback_service.process_chargeback_notification(db, payload, BackgroundTasks())

    count = len((await db.execute(select(Chargeback).where(Chargeback.mp_chargeback_id == "cb-4"))).scalars().all())
    assert count == 1


async def test_chargeback_notification_for_unknown_payment_id_is_ignored(db):
    """No debe lanzar excepcion - es un webhook, no una llamada de un cliente autenticado."""
    await chargeback_service.process_chargeback_notification(
        db, _mp_chargeback_payload(mp_payment_id="mp-no-existe"), BackgroundTasks()
    )
    result = await db.execute(select(Chargeback))
    assert result.scalars().all() == []


async def test_charged_back_payment_on_cancelled_order_still_marks_disputed(db):
    """Regresion: una orden PAID puede ser cancelada+reembolsada por otra via y el
    cardholder disputar el cargo original de todos modos ("disputa tras reembolso") -
    finalize_order_payment debe seguir marcando disputed_at aunque order.status ya sea
    CANCELLED (el guard en payments.py que decide si esto se llega a invocar se prueba
    aparte a nivel de ruta - ver la nota en payments.py; esto prueba que la logica que ese
    guard protege es efectivamente segura de invocar en este estado)."""
    order = _make_paid_order()
    order.status = "CANCELLED"
    db.add(order)
    await db.flush()

    mp_payment = {"id": order.mp_payment_id, "status": "charged_back", "status_detail": "in_process"}
    updated = await finalize_order_payment(db, order, mp_payment, BackgroundTasks())

    assert updated.status == "CANCELLED"  # el contracargo no resucita la orden
    assert updated.disputed_at is not None


async def test_approved_replay_does_not_resurrect_a_cancelled_order(db, monkeypatch):
    """Una notificacion "approved" tardia/repetida nunca debe regresar una orden CANCELLED
    a PAID - protege el carve-out de la prueba anterior contra el riesgo señalado en la
    auditoria (permitir contracargos sobre ordenes CANCELLED no debe abrir la puerta a
    que otros estados si las resuciten)."""
    order = _make_paid_order()
    order.status = "CANCELLED"
    db.add(order)
    await db.flush()

    mp_payment = {"id": order.mp_payment_id, "status": "approved", "status_detail": "accredited"}
    updated = await finalize_order_payment(db, order, mp_payment, BackgroundTasks())

    assert updated.status == "CANCELLED"


async def test_mediation_then_real_chargeback_fires_both_alerts(db, monkeypatch):
    """Escalacion real mediacion -> contracargo sobre la MISMA orden debe disparar las dos
    alertas - antes de este fix, la segunda se habria suprimido porque disputed_at ya
    quedaba puesto por la mediacion (mismo defecto que el resto de esta auditoria, solo
    que este lo encontre disenando el propio fix en vez de grepeando por el)."""
    order = _make_paid_order()
    db.add(order)
    await db.flush()

    mock_mediation = AsyncMock()
    mock_chargeback = AsyncMock()
    monkeypatch.setattr(order_history_service.admin_notification_service, "notify_admin_payment_in_mediation", mock_mediation)
    monkeypatch.setattr(order_history_service.admin_notification_service, "notify_admin_chargeback_received", mock_chargeback)

    mediation_payment = {"id": order.mp_payment_id, "status": "in_mediation", "status_detail": "in_process"}
    await finalize_order_payment(db, order, mediation_payment, BackgroundTasks())
    await db.refresh(order)
    first_disputed_at = order.disputed_at
    assert first_disputed_at is not None
    mock_mediation.assert_awaited_once()

    chargeback_payment = {"id": order.mp_payment_id, "status": "charged_back", "status_detail": "in_process"}
    await finalize_order_payment(db, order, chargeback_payment, BackgroundTasks())
    await db.refresh(order)

    mock_chargeback.assert_awaited_once()  # la escalacion SI debe re-avisar
    assert order.disputed_at == first_disputed_at  # marcador historico, no se pisa
