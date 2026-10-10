"""Avisos de reabastecimiento: POST /products/{uuid}/stock-alerts, /auth/me/stock-alerts y
stock_alert_worker.process_back_in_stock. Ver CLAUDE.md, "Avisos de reabastecimiento"."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.core.security import create_client_token, decode_unsubscribe_token
from app.models.stock_alert import StockAlert
from app.models.webhook_outbox import WebhookOutbox
from app.worker.stock_alert_worker import process_back_in_stock
from tests.conftest import make_client_account, make_product


async def _out_of_stock(db, **kw):
    product = make_product(stock=Decimal("0"), **kw)
    db.add(product)
    await db.flush()
    return product


async def test_subscribe_guest_idempotent_and_conflicts(api, db):
    product = await _out_of_stock(db)
    url = f"/v1/products/{product.sicar_uuid}/stock-alerts"

    first = await api.post(url, json={"email": "Ana@Example.com"})
    assert first.status_code == 201, first.text
    assert first.json()["email"] == "ana@example.com"

    again = await api.post(url, json={"email": "ana@example.com"})
    assert again.status_code == 200 and again.json()["uuid"] == first.json()["uuid"]

    assert (await api.post(url, json={"email": "no-es-correo"})).status_code == 422
    assert (await api.post(url, json={})).status_code == 400
    assert (await api.post("/v1/products/no-existe/stock-alerts", json={"email": "a@b.com"})).status_code == 404

    in_stock = make_product(stock=Decimal("3"))
    db.add(in_stock)
    await db.flush()
    assert (await api.post(f"/v1/products/{in_stock.sicar_uuid}/stock-alerts", json={"email": "a@b.com"})).status_code == 409

    # Reservado por completo cuenta como agotado (available_stock = 0).
    reserved = make_product(stock=Decimal("2"), reserved=Decimal("2"))
    db.add(reserved)
    await db.flush()
    assert (await api.post(f"/v1/products/{reserved.sicar_uuid}/stock-alerts", json={"email": "a@b.com"})).status_code == 201


async def test_account_uses_own_email_and_can_list_and_cancel(api, db):
    client = make_client_account(name="Juan")
    db.add(client)
    product = await _out_of_stock(db)
    token = create_client_token(client.uuid)

    created = await api.post(f"/v1/products/{product.sicar_uuid}/stock-alerts",
                             json={"email": "otro@example.com"}, headers={"X-Client-Token": token})
    assert created.status_code == 201 and created.json()["email"] == client.email

    listed = await api.get("/v1/auth/me/stock-alerts", headers={"Authorization": token})
    assert listed.status_code == 200
    assert [d["productUuid"] for d in listed.json()["docs"]] == [product.sicar_uuid]

    for _ in range(2):  # idempotente
        assert (await api.delete(f"/v1/auth/me/stock-alerts/{product.sicar_uuid}", headers={"Authorization": token})).status_code == 204
    assert (await api.get("/v1/auth/me/stock-alerts", headers={"Authorization": token})).json()["docs"] == []


async def test_worker_notifies_once_only_when_back_in_stock(db):
    client = make_client_account(name="Juan")
    db.add(client)
    await db.flush()
    product = await _out_of_stock(db)
    guest = StockAlert(product_id=product.id, email="invitado@example.com", expires_at=datetime.now(timezone.utc) + timedelta(days=90))
    member = StockAlert(product_id=product.id, email=client.email, client_account_id=client.id, expires_at=datetime.now(timezone.utc) + timedelta(days=90))
    expired = StockAlert(product_id=product.id, email="viejo@example.com", expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    db.add_all([guest, member, expired])
    await db.flush()

    assert await process_back_in_stock(db) == 0  # sigue agotado: nada

    product.stock = Decimal("4")
    await db.flush()
    assert await process_back_in_stock(db) == 2
    assert await process_back_in_stock(db) == 0  # una sola vez

    rows = (await db.execute(select(WebhookOutbox).where(WebhookOutbox.event == "product-back-in-stock"))).scalars().all()
    payloads = {r.payload["email"]: r.payload for r in rows}
    assert set(payloads) == {"invitado@example.com", client.email}
    assert payloads["invitado@example.com"]["clientName"] is None
    assert payloads[client.email]["clientName"] == "Juan"
    assert payloads[client.email]["product"] == {
        "sicarUuid": product.sicar_uuid, "sku": product.sku, "name": product.name,
        "imageUrl": None, "price": 100.0, "stock": 4.0,
    }
    scope, sub = decode_unsubscribe_token(payloads["invitado@example.com"]["unsubscribeToken"])
    assert (scope, sub) == ("stock-alert", guest.uuid)
    await db.refresh(expired)
    assert expired.notified_at is None
