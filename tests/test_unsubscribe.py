"""marketingEmails (registro, PATCH /auth/me) y POST /notifications/unsubscribe."""
from datetime import datetime, timedelta, timezone

from app.core.security import create_client_token, create_unsubscribe_token
from app.models.stock_alert import StockAlert
from tests.conftest import make_client_account, make_product


async def test_marketing_defaults_false_and_patch(api, db):
    client = make_client_account()
    db.add(client)
    await db.flush()
    token = create_client_token(client.uuid)

    me = (await api.get("/v1/auth/me", headers={"Authorization": token})).json()
    assert me["marketingEmails"] is False

    patched = await api.patch("/v1/auth/me", json={"marketingEmails": True}, headers={"Authorization": token})
    assert patched.status_code == 200 and patched.json()["marketingEmails"] is True
    assert client.marketing_emails_updated_at is not None


async def test_register_accepts_marketing(api, db, monkeypatch):
    from app.services import client_service

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(client_service, "notify_verification_requested", _noop)
    body = {"name": "Ana", "email": "ana-mkt@example.com", "password": "secreta123",
            "termsAccepted": True, "termsVersion": "v1"}
    response = await api.post("/v1/auth/register", json={**body, "marketingEmails": True})
    assert response.status_code == 201, response.text
    assert response.json()["client"]["marketingEmails"] is True

    other = await api.post("/v1/auth/register", json={**body, "email": "beto-mkt@example.com"})
    assert other.json()["client"]["marketingEmails"] is False


async def test_unsubscribe_marketing(api, db):
    client = make_client_account(email="carlos@example.com", marketing_emails=True)
    db.add(client)
    await db.flush()
    token = create_unsubscribe_token("marketing", client.uuid)

    for _ in range(2):  # idempotente
        response = await api.post("/v1/notifications/unsubscribe", json={"token": token})
        assert response.status_code == 200, response.text
        assert response.json() == {"scope": "marketing", "email": "c***@example.com"}
    assert client.marketing_emails is False


async def test_unsubscribe_stock_alert(api, db):
    product = make_product()
    db.add(product)
    await db.flush()
    alert = StockAlert(product_id=product.id, email="dora@example.com", expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    db.add(alert)
    await db.flush()

    response = await api.post("/v1/notifications/unsubscribe", json={"token": create_unsubscribe_token("stock-alert", alert.uuid)})
    assert response.status_code == 200 and response.json()["scope"] == "stock-alert"
    assert alert.unsubscribed_at is not None


async def test_invalid_tokens(api, db):
    client = make_client_account()
    db.add(client)
    await db.flush()
    assert (await api.post("/v1/notifications/unsubscribe", json={"token": "basura"})).status_code == 400
    # Un token de sesion no sirve como enlace de baja...
    assert (await api.post("/v1/notifications/unsubscribe", json={"token": create_client_token(client.uuid)})).status_code == 400
    # ...ni un enlace de baja como sesion.
    unsub = create_unsubscribe_token("marketing", client.uuid)
    assert (await api.get("/v1/auth/me", headers={"Authorization": unsub})).status_code == 401


async def test_unsubscribe_survives_secret_rotation(api, db, monkeypatch):
    """Tras rotar CLIENT_JWT_SECRET, un enlace de baja ya enviado sigue sirviendo con
    CLIENT_JWT_SECRET_PREVIOUS, pero una sesion firmada con el secreto viejo no."""
    from app.core.config import settings

    client = make_client_account(email="elena@example.com", marketing_emails=True)
    db.add(client)
    await db.flush()
    old_unsub = create_unsubscribe_token("marketing", client.uuid)
    old_session = create_client_token(client.uuid)

    monkeypatch.setattr(settings, "CLIENT_JWT_SECRET_PREVIOUS", settings.CLIENT_JWT_SECRET)
    monkeypatch.setattr(settings, "CLIENT_JWT_SECRET", "secreto-rotado-para-la-prueba-0123456789")

    assert (await api.get("/v1/auth/me", headers={"Authorization": old_session})).status_code == 401
    response = await api.post("/v1/notifications/unsubscribe", json={"token": old_unsub})
    assert response.status_code == 200, response.text
    assert client.marketing_emails is False

    monkeypatch.setattr(settings, "CLIENT_JWT_SECRET_PREVIOUS", None)
    assert (await api.post("/v1/notifications/unsubscribe", json={"token": old_unsub})).status_code == 400
