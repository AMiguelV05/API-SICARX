"""Adaptador de la Orders API de Mercado Pago (payment_service). Las respuestas simuladas
copian la forma real de `/v1/orders` en el entorno de prueba (2026-10-25): el rechazo llega
como 402 con la order en `data`, OXXO como `action_required`/`waiting_payment` con
`ticket_url`. Sin red real."""
import json
import uuid
from decimal import Decimal

import httpx
import pytest

from app.models.order import Order
from app.services import payment_service

ORD_ID = "ORDTST01M5TV2XK1YZFFY6XJRCH6XV6X"
PAY_ID = "PAY01M5TV2XK1YZFFY6XJRCH6XV6Y"


@pytest.fixture(autouse=True)
def _known_payment_methods(monkeypatch):
    """Catalogo de medios de pago ya en cache, para que ninguna prueba lo pida a la red."""
    monkeypatch.setitem(payment_service._payment_methods_cache, "fetched_at", 1e12)
    monkeypatch.setitem(payment_service._payment_methods_cache, "types", {
        "master": {"credit_card", "prepaid_card"},
        "debvisa": {"debit_card"},
        "oxxo": {"ticket"},
    })


def _mock_mp(monkeypatch, handler):
    requests: list[httpx.Request] = []

    def recorder(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(payment_service.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(recorder), **kw))
    return requests


def _order(**overrides) -> Order:
    fields = dict(
        uuid=str(uuid.uuid4()),
        sicar_order_id=str(uuid.uuid4()),
        status="TO_PAY",
        total=Decimal("199.50"),
        total_quantity=Decimal("1"),
        guest_email="cliente@example.com",
        delivery_info={"deliveryType": "PICKUP", "contactInfo": {"email": "cliente@example.com", "name": "Ana López Ruiz"}},
        items=[],
    )
    fields.update(overrides)
    return Order(**fields)


def _mp_order(status, status_detail, payment_status=None, payment_detail=None, method=None, **extra):
    return {
        "id": ORD_ID,
        "status": status,
        "status_detail": status_detail,
        "external_reference": "ref-1",
        "total_amount": "199.50",
        "transactions": {"payments": [{
            "id": PAY_ID,
            "status": payment_status or status,
            "status_detail": payment_detail or status_detail,
            "payment_method": method or {"id": "master", "type": "credit_card"},
        }], **extra.pop("transactions_extra", {})},
        **extra,
    }


async def test_card_payment_builds_orders_payload_and_maps_approved(monkeypatch):
    requests = _mock_mp(monkeypatch, lambda r: httpx.Response(201, json=_mp_order("processed", "accredited")))
    order = _order()

    result = await payment_service.create_payment(order, {
        "token": "tok_123", "paymentMethodId": "master", "installments": 3,
        "issuerId": "123", "payer": {"email": "test_user_mx@testuser.com"},
    })

    assert result["id"] == ORD_ID
    assert result["status"] == "approved"
    assert result["status_detail"] == "accredited"
    sent = json.loads(requests[0].content)
    assert requests[0].url == payment_service.ORDERS_URL
    assert requests[0].headers["X-Idempotency-Key"]
    assert sent["type"] == "online" and sent["processing_mode"] == "automatic"
    assert sent["total_amount"] == "199.50" and sent["external_reference"] == order.uuid
    assert "notification_url" not in sent  # la Orders API no lo acepta; va en el panel
    assert sent["payer"] == {"email": "test_user_mx@testuser.com", "first_name": "Ana", "last_name": "López Ruiz"}
    assert sent["transactions"]["payments"] == [{
        "amount": "199.50",
        "payment_method": {"id": "master", "type": "credit_card", "token": "tok_123", "installments": 3},
    }]


async def test_rejected_card_402_is_a_result_not_an_error(monkeypatch):
    body = {
        "errors": [{"code": "failed", "message": "The following transactions failed",
                    "details": [f"{PAY_ID}: rejected_by_issuer"]}],
        "data": _mp_order("failed", "failed", payment_detail="rejected_by_issuer"),
    }
    _mock_mp(monkeypatch, lambda r: httpx.Response(402, json=body))

    result = await payment_service.create_payment(_order(), {"token": "t", "paymentMethodId": "master", "payer": {"email": "a@b.com"}})

    assert result["status"] == "rejected"
    assert result["status_detail"] == "rejected_by_issuer"  # el motivo concreto, no "failed"


async def test_oxxo_is_pending_with_ticket_and_no_card_fields(monkeypatch):
    ticket = "https://www.mercadopago.com.mx/payments/186114286130/ticket"
    requests = _mock_mp(monkeypatch, lambda r: httpx.Response(201, json=_mp_order(
        "action_required", "waiting_payment", method={"id": "oxxo", "type": "ticket", "ticket_url": ticket})))

    result = await payment_service.create_payment(_order(), {"paymentMethodId": "oxxo", "payer": {"email": "a@b.com"}})

    assert result["status"] == "pending"
    assert result["transaction_details"]["external_resource_url"] == ticket
    method = json.loads(requests[0].content)["transactions"]["payments"][0]["payment_method"]
    assert method == {"id": "oxxo", "type": "ticket"}


async def test_payment_type_from_brick_wins_and_debit_is_inferred(monkeypatch):
    requests = _mock_mp(monkeypatch, lambda r: httpx.Response(201, json=_mp_order("processed", "accredited")))

    await payment_service.create_payment(_order(), {"token": "t", "paymentMethodId": "master", "paymentTypeId": "prepaid_card", "payer": {"email": "a@b.com"}})
    await payment_service.create_payment(_order(), {"token": "t", "paymentMethodId": "debvisa", "payer": {"email": "a@b.com"}})
    await payment_service.create_payment(_order(), {"token": "t", "paymentMethodId": "debvisa", "paymentTypeId": "wallet_purchase", "payer": {"email": "a@b.com"}})

    types = [json.loads(r.content)["transactions"]["payments"][0]["payment_method"]["type"] for r in requests]
    assert types == ["prepaid_card", "debit_card", "debit_card"]  # un valor fuera de la lista se ignora


async def test_get_payment_reads_the_order(monkeypatch):
    requests = _mock_mp(monkeypatch, lambda r: httpx.Response(200, json=_mp_order("processing", "in_review")))

    assert (await payment_service.get_payment(ORD_ID))["status"] == "in_process"
    assert [r.url.path for r in requests] == [f"/v1/orders/{ORD_ID}"]


@pytest.mark.parametrize("call", [
    lambda: payment_service.get_payment("186114286130"),
    lambda: payment_service.refund_payment("186114286130"),
    lambda: payment_service.cancel_payment("186114286130"),
])
async def test_legacy_numeric_ids_are_rejected_without_calling_mp(monkeypatch, call):
    """Pedidos de prueba cobrados con la app anterior (Payments API): ya no se gestionan."""
    from fastapi import HTTPException

    requests = _mock_mp(monkeypatch, lambda r: httpx.Response(500))
    with pytest.raises(HTTPException) as exc:
        await call()
    assert exc.value.status_code == 409
    assert requests == []


def test_chargeback_case_ids_come_from_the_order():
    mp_order = _mp_order("charged_back", "in_process", transactions_extra={"chargebacks": [
        {"id": "CBK1", "transaction_id": PAY_ID, "case_id": "123456789", "status": "in_process"},
        {"id": "CBK2", "transaction_id": PAY_ID, "case_id": None},
    ]})
    assert payment_service._order_as_payment(mp_order)["chargeback_case_ids"] == ["123456789"]


@pytest.mark.parametrize("status,detail,expected", [
    ("charged_back", "in_process", "in_mediation"),
    ("charged_back", "settled", "charged_back"),
    ("action_required", "waiting_capture", "authorized"),
    ("canceled", "canceled", "cancelled"),
    ("expired", "expired", "cancelled"),
    ("refunded", "refunded", "refunded"),
])
def test_status_mapping(status, detail, expected):
    assert payment_service._order_as_payment(_mp_order(status, detail))["status"] == expected


def test_refunded_amount_ignores_failed_refund_attempts():
    """Caso real del entorno de prueba: un reembolso que MP rechazo con 409 queda en
    `refunds[]` como `failed` y no debe sumarse (daba 250 en una order de 150)."""
    refunds = [
        {"amount": "50.00", "status": "processed"},
        {"amount": "100.00", "status": "failed"},
        {"amount": "100.00", "status": "processed"},
    ]
    from_list = _mp_order("refunded", "refunded", transactions_extra={"refunds": refunds})
    assert payment_service._order_as_payment(from_list)["transaction_amount_refunded"] == "150.00"

    from_field = _mp_order("refunded", "refunded", transactions_extra={"refunds": refunds})
    from_field["transactions"]["payments"][0]["refunded_amount"] = "150.00"
    assert payment_service._order_as_payment(from_field)["transaction_amount_refunded"] == "150.00"

    without = _mp_order("refunded", "refunded", total_paid_amount="199.50")
    assert payment_service._order_as_payment(without)["transaction_amount_refunded"] == "199.50"


async def test_partial_refund_targets_the_transaction(monkeypatch):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=_mp_order("processed", "accredited"))
        return httpx.Response(201, json=_mp_order("processed", "partially_refunded",
                                                  transactions_extra={"refunds": [{"id": "REF01", "amount": "50.00"}]}))
    requests = _mock_mp(monkeypatch, handler)

    result = await payment_service.refund_payment(ORD_ID, amount=Decimal("50"))

    assert result == {"id": "REF01", "amount": "50.00"}  # el ultimo reembolso no fallido
    post = requests[-1]
    assert post.url.path == f"/v1/orders/{ORD_ID}/refund"
    assert json.loads(post.content) == {"transactions": [{"id": PAY_ID, "amount": "50.00"}]}
    assert post.headers["X-Idempotency-Key"]


async def test_full_refund_sends_no_transactions(monkeypatch):
    requests = _mock_mp(monkeypatch, lambda r: httpx.Response(201, json=_mp_order(
        "refunded", "refunded", transactions_extra={"refunds": [{"id": "REF02", "amount": "199.50"}]})))

    result = await payment_service.refund_payment(ORD_ID)

    assert result == {"id": "REF02", "amount": "199.50"}
    assert len(requests) == 1 and json.loads(requests[0].content) == {}


async def test_cancel_uses_orders_cancel_endpoint(monkeypatch):
    requests = _mock_mp(monkeypatch, lambda r: httpx.Response(200, json=_mp_order("canceled", "canceled")))

    result = await payment_service.cancel_payment(ORD_ID)

    assert result["status"] == "cancelled"
    assert requests[0].method == "POST" and requests[0].url.path == f"/v1/orders/{ORD_ID}/cancel"
    assert requests[0].headers["X-Idempotency-Key"]


def test_order_ids_are_recognized():
    assert payment_service.is_mp_order_id("ORD01JS2V6CM8KJ0EC4H502TGK1WP")
    assert payment_service.is_mp_order_id("ORDTST01M5TV2XK1YZFFY6XJRCH6XV6X")
    assert not payment_service.is_mp_order_id("186114286130")
    assert not payment_service.is_mp_order_id(None)


async def test_order_webhook_marks_order_paid(api, db, monkeypatch):
    """Notificacion real de la Orders API: `type=order` y `data.id` = `ORD...` (en
    mayusculas; la firma usa el id en minusculas). Debe consultar `/v1/orders/{id}` y dejar
    el pedido en PAID con ese id guardado."""
    from app.models.order import Order as OrderModel
    from tests.conftest import make_product
    from tests.test_chargebacks import _signed_webhook

    product = make_product(stock=Decimal("10"), reserved=Decimal("1"))
    db.add(product)
    await db.flush()
    order = _order(items=[{"uuid": product.sicar_uuid, "quantity": "1", "sku": "SKU-1", "description": "Producto"}],
                   dispatch_status="PENDING_ACCEPTANCE", branch_id=151456)
    db.add(order)
    await db.flush()

    approved = _mp_order("processed", "accredited")
    approved["external_reference"] = order.uuid
    requests = _mock_mp(monkeypatch, lambda r: httpx.Response(200, json=approved)
                        if r.url.path == f"/v1/orders/{ORD_ID}" else httpx.Response(204))

    url, headers = _signed_webhook(ORD_ID, "order")
    response = await api.post(url, headers=headers, json={"type": "order", "action": "order.processed", "data": {"id": ORD_ID}})

    assert response.json() == {"status": "ok"}
    assert any(r.url.path == f"/v1/orders/{ORD_ID}" for r in requests)
    refreshed = await db.get(OrderModel, order.id)
    assert refreshed.status == "PAID"
    assert refreshed.mp_payment_id == ORD_ID
    assert refreshed.mp_status == "approved"
