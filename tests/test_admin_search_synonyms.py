"""/v1/admin/search/synonyms via HTTP (ASGI), con la sesion transaccional del fixture `db`
inyectada en la app - todo se revierte al final. Typesense se simula en la frontera de
typesense_client (mismo patron que test_refunds.py con Mercado Pago)."""
import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_admin_token
from app.models.audit_log import AdminAuditLog
from app.models.search_synonym import SearchSynonym
from app.schemas.admin_auth import AdminUserCreate
from app.services import admin_auth_service, typesense_client

BASE = "/v1/admin/search/synonyms"


@pytest.fixture
async def client(db):
    from app.main import app

    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def typesense_off(monkeypatch):
    """Por defecto sin Typesense: las escrituras deben funcionar igual (syncedToSearch=false)."""
    monkeypatch.setattr(settings, "TYPESENSE_URL", None)
    typesense_client.reset_breaker()


async def _headers(db, role: str) -> dict:
    admin = await admin_auth_service.create_admin_user(
        db, AdminUserCreate(email=f"{role}@example.com", name=f"Admin {role}", password="correcta123", role=role)
    )
    return {"Authorization": f"Bearer {create_admin_token(admin.uuid)}"}


async def test_requires_admin_token(client):
    assert (await client.get(BASE)).status_code == 401


async def test_create_normalizes_words_and_is_audited(client, db):
    headers = await _headers(db, "staff")
    response = await client.post(BASE, json={"root": " Inalámbrico ", "synonyms": ["Baterías", "20 V", "baterias"]}, headers=headers)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["root"] == "inalambrico"
    assert body["synonyms"] == ["bateria", "20v"]
    assert body["kind"] == "ONE_WAY"
    assert body["syncedToSearch"] is False  # Typesense no configurado
    assert set(body) >= {"uuid", "createdAt", "updatedAt"}

    audit = (await db.execute(select(AdminAuditLog).where(AdminAuditLog.resource_id == body["uuid"]))).scalars().all()
    assert [a.action for a in audit] == ["search_synonym.create"]


async def test_create_duplicate_of_seed_in_other_order_is_409(client, db):
    headers = await _headers(db, "staff")
    response = await client.post(BASE, json={"synonyms": ["Hexagonal", "allen"]}, headers=headers)
    assert response.status_code == 409


@pytest.mark.parametrize("payload", [
    {"synonyms": ["solo"]},                                  # multi-direccional con 1 palabra
    {"synonyms": ["martillo", "Martillos"]},                 # colapsan a la misma palabra
    {"root": "pija", "synonyms": ["pijas"]},                 # la raiz se repite tras normalizar
    {"synonyms": []},                                        # lista vacia (validacion del schema)
    {"synonyms": ["x" * 60, "y"]},                           # palabra demasiado larga
])
async def test_create_validation_errors_are_422(client, db, payload):
    headers = await _headers(db, "staff")
    assert (await client.post(BASE, json=payload, headers=headers)).status_code == 422


async def test_list_includes_seeds_and_filters_by_q(client, db):
    headers = await _headers(db, "staff")
    body = (await client.get(BASE, headers=headers)).json()
    assert body["total"] >= 4
    body = (await client.get(BASE, params={"q": "Hexagonal"}, headers=headers)).json()
    assert body["total"] == 1
    assert sorted(body["docs"][0]["synonyms"]) == ["allen", "hexagonal"]


async def test_update_root_to_null_makes_it_multi_way(client, db):
    headers = await _headers(db, "staff")
    created = (await client.post(BASE, json={"root": "cople", "synonyms": ["copla", "union"]}, headers=headers)).json()

    response = await client.patch(f"{BASE}/{created['uuid']}", json={"root": None}, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["root"] is None and body["kind"] == "MULTI_WAY"
    assert body["synonyms"] == ["copla", "union"]

    audit = await db.scalar(select(AdminAuditLog).where(
        AdminAuditLog.resource_id == created["uuid"], AdminAuditLog.action == "search_synonym.update"))
    assert audit.detail["before"]["root"] == "cople"
    assert audit.detail["after"]["root"] is None

    assert (await client.patch(f"{BASE}/no-existe", json={"root": None}, headers=headers)).status_code == 404


async def test_delete_requires_super_admin(client, db):
    staff = await _headers(db, "staff")
    created = (await client.post(BASE, json={"synonyms": ["broquero", "mandril"]}, headers=staff)).json()
    assert (await client.delete(f"{BASE}/{created['uuid']}", headers=staff)).status_code == 403

    boss = await _headers(db, "super_admin")
    assert (await client.delete(f"{BASE}/{created['uuid']}", headers=boss)).status_code == 204
    assert await db.scalar(select(SearchSynonym).where(SearchSynonym.uuid == created["uuid"])) is None
    assert (await client.delete(f"{BASE}/{created['uuid']}", headers=boss)).status_code == 404


async def test_push_to_typesense_success_and_failure(client, db, monkeypatch):
    monkeypatch.setattr(settings, "TYPESENSE_URL", "http://typesense.test:8108")
    monkeypatch.setattr(settings, "TYPESENSE_API_KEY", "k")
    pushed = []

    async def ok_upsert(collection, synonym_id, body):
        pushed.append((collection, synonym_id, body))

    monkeypatch.setattr(typesense_client, "upsert_synonym", ok_upsert)
    headers = await _headers(db, "staff")
    body = (await client.post(BASE, json={"root": "Taladro", "synonyms": ["rotomartillo"]}, headers=headers)).json()
    assert body["syncedToSearch"] is True
    assert pushed == [("products", body["uuid"], {"synonyms": ["rotomartillo"], "root": "taladro"})]

    async def failing_upsert(collection, synonym_id, body):
        raise typesense_client.TypesenseError("caido", 503)

    monkeypatch.setattr(typesense_client, "upsert_synonym", failing_upsert)
    response = await client.post(BASE, json={"synonyms": ["flexometro", "cinta metrica"]}, headers=headers)
    assert response.status_code == 201
    assert response.json()["syncedToSearch"] is False
    # El guardado no se revierte: la reconciliacion del worker lo aplica despues.
    assert await db.scalar(select(SearchSynonym).where(SearchSynonym.uuid == response.json()["uuid"])) is not None
