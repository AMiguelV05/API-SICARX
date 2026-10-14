"""Historial del consentimiento de correos de marketing (marketing_consent_events)."""
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.core.security import create_client_token, create_unsubscribe_token
from app.models.client import ClientAccount
from app.models.marketing_consent import MarketingConsentEvent
from tests.conftest import admin_headers, make_client_account

REGISTER = {"name": "Ana", "password": "secreta123", "termsAccepted": True, "termsVersion": "v1"}


@pytest.fixture(autouse=True)
def _no_webhooks(monkeypatch):
    from app.services import client_service

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(client_service, "notify_verification_requested", _noop)


async def _events(db, client_id):
    rows = await db.scalars(
        select(MarketingConsentEvent).where(MarketingConsentEvent.client_account_id == client_id)
        .order_by(MarketingConsentEvent.id)
    )
    return list(rows.all())


async def _account(db, email):
    return await db.scalar(select(ClientAccount).where(ClientAccount.email == email))


async def test_register_records_initial_state_with_version_ip_and_ua(api, db):
    response = await api.post(
        "/v1/auth/register",
        json={**REGISTER, "email": "consent-1@example.com", "marketingEmails": True,
              "marketingConsentSource": "register_checkbox", "privacyNoticeVersion": "2026-10-12"},
        headers={"User-Agent": "Mozilla/5.0 prueba", "X-Forwarded-For": "201.1.2.3"},
    )
    assert response.status_code == 201, response.text
    client_json = response.json()["client"]
    assert client_json["marketingEmails"] is True and client_json["marketingConsentAt"] is not None

    client = await _account(db, "consent-1@example.com")
    (event,) = await _events(db, client.id)
    assert (event.value, event.source, event.privacy_notice_version) == (True, "register_checkbox", "2026-10-12")
    assert (event.ip_address, event.user_agent) == ("201.1.2.3", "Mozilla/5.0 prueba")
    assert event.changed_at == client.marketing_emails_updated_at


async def test_register_records_unchecked_box_too(api, db):
    response = await api.post("/v1/auth/register", json={**REGISTER, "email": "consent-2@example.com", "marketingEmails": False})
    assert response.status_code == 201
    client = await _account(db, "consent-2@example.com")
    (event,) = await _events(db, client.id)
    assert (event.value, event.source, event.privacy_notice_version) == (False, "register_checkbox", None)


async def test_register_rejects_a_foreign_source(api):
    response = await api.post("/v1/auth/register", json={**REGISTER, "email": "consent-3@example.com", "marketingConsentSource": "admin"})
    assert response.status_code == 422


async def test_trusted_frontend_forwards_visitor_ip_and_ua(api, db, monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_PROXY_SECRET", "proxy-secreto")
    response = await api.post(
        "/v1/auth/register", json={**REGISTER, "email": "consent-4@example.com", "marketingEmails": True},
        headers={"X-Frontend-Auth": "proxy-secreto", "X-Client-IP": "189.9.9.9",
                 "X-Client-User-Agent": "Safari visitante", "User-Agent": "node-fetch"},
    )
    assert response.status_code == 201, response.text
    (event,) = await _events(db, (await _account(db, "consent-4@example.com")).id)
    assert (event.ip_address, event.user_agent) == ("189.9.9.9", "Safari visitante")


async def test_patch_records_only_real_changes(api, db):
    client = make_client_account()
    db.add(client)
    await db.flush()
    token = create_client_token(client.uuid)

    same = await api.patch("/v1/auth/me", json={"marketingEmails": False}, headers={"Authorization": token})
    assert same.status_code == 200 and await _events(db, client.id) == []

    on = await api.patch("/v1/auth/me", json={"marketingEmails": True, "privacyNoticeVersion": "2026-10-12"}, headers={"Authorization": token})
    assert on.status_code == 200 and on.json()["marketingConsentAt"] is not None
    (event,) = await _events(db, client.id)
    assert (event.value, event.source, event.privacy_notice_version) == (True, "account_toggle", "2026-10-12")

    renamed = await api.patch("/v1/auth/me", json={"name": "Otro nombre"}, headers={"Authorization": token})
    assert renamed.status_code == 200 and len(await _events(db, client.id)) == 1


@pytest.mark.parametrize("body, source", [
    ({}, "email_unsubscribe"),
    ({"source": "one_click_unsubscribe"}, "one_click_unsubscribe"),
])
async def test_unsubscribe_records_source_once(api, db, body, source):
    client = make_client_account(marketing_emails=True)
    db.add(client)
    await db.flush()
    token = create_unsubscribe_token("marketing", client.uuid)

    for _ in range(2):  # la segunda es no-op: ya estaba dado de baja
        response = await api.post("/v1/notifications/unsubscribe", json={"token": token, **body})
        assert response.status_code == 200, response.text
    (event,) = await _events(db, client.id)
    assert (event.value, event.source) == (False, source)
    assert client.marketing_emails is False


async def test_google_signup_records_consent(api, db, monkeypatch):
    from app.api.routes import auth as auth_routes

    async def fake_verify(_token):
        return {"sub": "google-sub-consent", "email": "consent-g@example.com", "name": "G", "email_verified": True}
    monkeypatch.setattr(auth_routes, "verify_google_id_token", fake_verify)

    body = {"idToken": "x", "marketingEmails": True, "privacyNoticeVersion": "2026-10-12"}
    assert (await api.post("/v1/auth/google", json=body)).status_code == 200
    client = await _account(db, "consent-g@example.com")
    (event,) = await _events(db, client.id)
    assert (event.value, event.source, event.privacy_notice_version) == (True, "google_signup", "2026-10-12")

    # Login posterior con la cuenta ya creada: no registra nada nuevo.
    assert (await api.post("/v1/auth/google", json={**body, "marketingEmails": False})).status_code == 200
    assert len(await _events(db, client.id)) == 1


async def test_history_is_append_only(db):
    client = make_client_account()
    db.add(client)
    await db.flush()
    db.add(MarketingConsentEvent(client_account_id=client.id, value=True, source="account_toggle"))
    await db.flush()

    for statement in (
        update(MarketingConsentEvent).where(MarketingConsentEvent.client_account_id == client.id).values(value=False),
        delete(MarketingConsentEvent).where(MarketingConsentEvent.client_account_id == client.id),
    ):
        savepoint = await db.begin_nested()
        with pytest.raises(DBAPIError, match="solo admite INSERT"):
            await db.execute(statement)
        await savepoint.rollback()


async def test_admin_consent_history(api, db):
    client = make_client_account(email="arco@example.com")
    db.add(client)
    await db.flush()
    token = create_client_token(client.uuid)
    await api.patch("/v1/auth/me", json={"marketingEmails": True, "privacyNoticeVersion": "v1"}, headers={"Authorization": token})
    await api.patch("/v1/auth/me", json={"marketingEmails": False}, headers={"Authorization": token})

    headers = await admin_headers(db, role="staff")
    response = await api.get(f"/v1/admin/clients/{client.uuid}/consents", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == "arco@example.com" and body["marketingEmails"] is False
    assert [(e["value"], e["source"]) for e in body["events"]] == [(False, "account_toggle"), (True, "account_toggle")]
    assert body["events"][1]["privacyNoticeVersion"] == "v1"
    assert body["marketingConsentAt"] == body["events"][0]["changedAt"]

    assert (await api.get("/v1/admin/clients/no-existe/consents", headers=headers)).status_code == 404
    assert (await api.get(f"/v1/admin/clients/{client.uuid}/consents")).status_code == 401
