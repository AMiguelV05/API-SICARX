"""Sync de catalogo (app/worker/sync_task.py): solo reescribe los productos que cambiaron y
detecta eliminados por el conjunto de uuids vistos, no por last_sync_id. Ver CLAUDE.md,
"Local catalog vs. live SICAR X data"."""
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text

from app.models.product import Product
from tests.conftest import make_product


@pytest.fixture(autouse=True)
def _reset_hidden_refresh(monkeypatch):
    """_last_full_hidden_refresh es global del modulo y cada pasada exitosa lo actualiza: sin
    reiniciarlo, el modo (revision completa o solo nuevos) de un test dependeria del anterior."""
    from app.worker import sync_task

    monkeypatch.setattr(sync_task, "_last_full_hidden_refresh", None)


def _sicar_values(product: Product, **overrides) -> dict:
    """Lo que sync_sicar_catalog construye para un producto que Sicar X devolvio igual."""
    values = {
        "sicar_uuid": product.sicar_uuid,
        "sku": product.sku,
        "name": product.name,
        "image_url": product.image_url,
        "department_uuid": product.department_uuid,
        "category_uuid": product.category_uuid,
        "is_bulk": product.is_bulk,
        "is_active": product.is_active,
        "price": product.price,
        "stock": product.stock,
        "is_deleted": False,
        "deleted_at": None,
        "last_sync_id": "pasada-nueva",
    }
    values.update(overrides)
    return values


async def _add(db, **overrides) -> Product:
    product = make_product(**{"is_bulk": False, "last_sync_id": "pasada-vieja", **overrides})
    db.add(product)
    await db.flush()
    return product


async def _reload(db, product: Product) -> Product:
    await db.refresh(product)
    return product


async def _ctid(db, product) -> str:
    return await db.scalar(text("SELECT ctid::text FROM products WHERE id = :id"), {"id": product.id})


async def test_unchanged_product_is_not_rewritten(db):
    from app.worker.sync_task import _changed_product_values

    product = await _add(db, price=Decimal("12.50"), stock=Decimal("3"))
    # Sicar X manda 12.5 / 3.0: mismo valor, otra representacion.
    values = _sicar_values(product, price=Decimal("12.50"), stock=Decimal("3.000"))

    assert await _changed_product_values(db, [values]) == []


async def test_changed_new_and_revived_products_are_upserted(db):
    from app.worker.sync_task import _changed_product_values, _upsert_products

    unchanged = await _add(db)
    repriced = await _add(db, price=Decimal("100.00"))
    revived = await _add(db, is_deleted=True, deleted_at=datetime.now(timezone.utc))
    unchanged_ctid = await _ctid(db, unchanged)

    new_values = _sicar_values(make_product(is_bulk=False))
    page = [
        _sicar_values(unchanged),
        _sicar_values(repriced, price=Decimal("120.00")),
        _sicar_values(revived),
        new_values,
    ]

    changed = await _changed_product_values(db, page)
    assert [v["sicar_uuid"] for v in changed] == [repriced.sicar_uuid, revived.sicar_uuid, new_values["sicar_uuid"]]

    await _upsert_products(db, changed)

    assert await _ctid(db, unchanged) == unchanged_ctid
    assert (await _reload(db, repriced)).price == Decimal("120.00")
    revived_row = await _reload(db, revived)
    assert revived_row.is_deleted is False and revived_row.deleted_at is None
    assert await db.scalar(select(Product.id).where(Product.sicar_uuid == new_values["sicar_uuid"])) is not None


async def test_missing_products_are_marked_deleted(db):
    from app.worker.sync_task import _mark_missing_as_deleted

    seen = await _add(db)
    missing = await _add(db)
    never_synced = await _add(db, last_sync_id=None)
    already_deleted = await _add(db, is_deleted=True, deleted_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    await _mark_missing_as_deleted(db, {seen.sicar_uuid})

    assert (await _reload(db, seen)).is_deleted is False
    missing_row = await _reload(db, missing)
    assert missing_row.is_deleted is True and missing_row.deleted_at is not None
    assert (await _reload(db, never_synced)).is_deleted is False
    assert (await _reload(db, already_deleted)).deleted_at == datetime(2026, 1, 1, tzinfo=timezone.utc)


def _mock_sicar(monkeypatch, pages: list[list[dict]], hidden: set[str] = frozenset(), hidden_status: int = 200) -> list[str]:
    """Sustituye la API de Sicar X: /product/list devuelve `pages` en orden y luego 204;
    la consulta GraphQL de `hidden` responde con `hidden_status` y marca ocultos los uuids de
    `hidden`. Devuelve la lista (que se va llenando) de cuerpos enviados a GraphQL."""
    from app.worker import sync_task

    remaining = list(pages)
    graphql_bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/product/list"):
            return httpx.Response(200, json=remaining.pop(0)) if remaining else httpx.Response(204)
        body = request.content.decode()
        graphql_bodies.append(body)
        if hidden_status != 200:
            return httpx.Response(hidden_status)
        return httpx.Response(200, json={"data": {"products": [{"uuid": u, "hidden": True} for u in hidden if u in body]}})

    real_client = httpx.AsyncClient

    async def fake_token():
        return "token"

    monkeypatch.setattr(sync_task.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(sync_task.sicar_auth, "get_token", fake_token)
    return graphql_bodies


def _sicar_item(product: Product, **overrides) -> dict:
    from app.worker.sync_task import PRICE_LIST_ID

    item = {
        "uuid": product.sicar_uuid,
        "sku": product.sku,
        "description": product.name,
        "imageUrl": product.image_url,
        "departmentUuid": product.department_uuid,
        "categoryUuid": product.category_uuid,
        "bulk": False,
        "prices": {f"N{PRICE_LIST_ID.split('-')[-1]}": float(product.price)},
        "stock": float(product.stock),
    }
    item.update(overrides)
    return item


async def test_full_pass_only_touches_changes(db, monkeypatch):
    from app.worker.sync_task import sync_sicar_catalog

    same = await _add(db)
    restocked = await _add(db, stock=Decimal("10"))
    gone = await _add(db)
    same_ctid = await _ctid(db, same)

    _mock_sicar(monkeypatch, [[_sicar_item(same), _sicar_item(restocked, stock=25.0)]])
    processed, deactivated, completed = await sync_sicar_catalog(db)

    assert (processed, completed) == (2, True)
    assert deactivated >= 1  # `gone` y cualquier otro producto sincronizado que ya exista en la BD
    assert await _ctid(db, same) == same_ctid
    assert (await _reload(db, restocked)).stock == Decimal("25")
    assert (await _reload(db, gone)).is_deleted is True


async def test_regular_pass_checks_hidden_only_for_new_products(db, monkeypatch):
    """Fuera de la revision completa horaria solo se consulta `hidden` de productos nuevos
    (egress) y los ya guardados conservan su is_active."""
    from app.worker.sync_task import sync_sicar_catalog

    hidden_stored = await _add(db, is_active=False)
    visible_stored = await _add(db)
    new = make_product(is_bulk=False)

    bodies = _mock_sicar(
        monkeypatch,
        [[_sicar_item(hidden_stored), _sicar_item(visible_stored), _sicar_item(new)]],
        hidden={new.sicar_uuid, visible_stored.sicar_uuid},
    )
    await sync_sicar_catalog(db, refresh_hidden=False)

    assert len(bodies) == 1
    assert new.sicar_uuid in bodies[0]
    assert hidden_stored.sicar_uuid not in bodies[0] and visible_stored.sicar_uuid not in bodies[0]
    assert (await _reload(db, hidden_stored)).is_active is False
    assert (await _reload(db, visible_stored)).is_active is True
    assert await db.scalar(select(Product.is_active).where(Product.sicar_uuid == new.sicar_uuid)) is False


async def test_full_refresh_updates_hidden_status(db, monkeypatch):
    from app.worker import sync_task

    now_hidden = await _add(db)
    now_visible = await _add(db, is_active=False)

    _mock_sicar(monkeypatch, [[_sicar_item(now_hidden), _sicar_item(now_visible)]], hidden={now_hidden.sicar_uuid})
    await sync_task.sync_sicar_catalog(db)

    assert (await _reload(db, now_hidden)).is_active is False
    assert (await _reload(db, now_visible)).is_active is True
    # Una revision completa exitosa reinicia el intervalo: la siguiente pasada ya no la repite.
    assert sync_task._full_hidden_refresh_due(datetime.now(timezone.utc)) is False


async def test_failed_hidden_check_keeps_stored_status(db, monkeypatch):
    """Si la consulta de `hidden` falla no se re-publican productos ocultos, y la revision
    completa queda pendiente para la siguiente pasada."""
    from app.worker import sync_task

    hidden_stored = await _add(db, is_active=False)

    _mock_sicar(monkeypatch, [[_sicar_item(hidden_stored)]], hidden_status=500)
    await sync_task.sync_sicar_catalog(db)

    assert (await _reload(db, hidden_stored)).is_active is False
    assert sync_task._full_hidden_refresh_due(datetime.now(timezone.utc)) is True


async def test_empty_catalog_does_not_delete_everything(db, monkeypatch):
    from app.worker.sync_task import sync_sicar_catalog

    product = await _add(db)
    _mock_sicar(monkeypatch, [])

    processed, deactivated, completed = await sync_sicar_catalog(db)

    assert (processed, deactivated, completed) == (0, 0, True)
    assert (await _reload(db, product)).is_deleted is False
