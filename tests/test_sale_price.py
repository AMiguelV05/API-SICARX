"""Precio de oferta (Product.compare_at_price/sale_ends_at): vigencia, exposicion en las
respuestas, filtro onSale y rutas admin - ver CLAUDE.md, "Precio de oferta"."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.audit_log import AdminAuditLog
from app.models.product import Product
from app.services import catalog_service, search_index, typesense_client
from app.services.order_service import build_order_payload
from tests.conftest import admin_headers, make_product

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def typesense_off(monkeypatch):
    monkeypatch.setattr(settings, "TYPESENSE_URL", None)
    typesense_client.reset_breaker()


def test_is_on_sale_rules():
    p = make_product(price=Decimal("100.00"))
    assert p.is_on_sale is False and p.effective_compare_at_price is None

    p.compare_at_price = Decimal("150.00")
    assert p.is_on_sale is True and p.effective_compare_at_price == Decimal("150.00")

    p.sale_ends_at = NOW - timedelta(minutes=1)  # vencida: se apaga sola
    assert p.is_on_sale is False and p.effective_sale_ends_at is None

    p.sale_ends_at = NOW + timedelta(days=1)
    p.price = Decimal("150.00")  # Sicar X subio el precio hasta igualarla
    assert p.is_on_sale is False


async def _seed(db):
    on_sale = make_product(name="Taladro en oferta", price=Decimal("120.00"), compare_at_price=Decimal("150.00"),
                           sale_ends_at=NOW + timedelta(days=3))
    expired = make_product(name="Taladro vencido", price=Decimal("120.00"), compare_at_price=Decimal("150.00"),
                           sale_ends_at=NOW - timedelta(days=1))
    no_end = make_product(name="Taladro sin fin", price=Decimal("80.00"), compare_at_price=Decimal("100.00"))
    plain = make_product(name="Taladro normal", price=Decimal("90.00"))
    for p in (on_sale, expired, no_end, plain):
        p.details_updated_at = NOW  # GET /products/{uuid} no intenta refrescar de Sicar X
    db.add_all([on_sale, expired, no_end, plain])
    await db.flush()
    return on_sale, expired, no_end, plain


async def test_on_sale_filter_catalog_and_postgres_search(db):
    on_sale, expired, no_end, plain = await _seed(db)
    ids = {on_sale.sicar_uuid, expired.sicar_uuid, no_end.sicar_uuid, plain.sicar_uuid}

    catalog = await catalog_service.get_local_catalog(db, {"on_sale": True, "limit": 200, "offset": 0})
    found = {p.sicar_uuid for p in catalog["docs"]} & ids
    assert found == {on_sale.sicar_uuid, no_end.sicar_uuid}

    search = await catalog_service.search_products(db, "taladro", 50, 0, on_sale=True)
    assert {p.sicar_uuid for p in search["docs"]} & ids == {on_sale.sicar_uuid, no_end.sicar_uuid}


async def test_catalog_response_exposes_compare_at_price(api, db):
    on_sale, expired, _, plain = await _seed(db)
    response = await api.post("/v1/products", json={"onSale": True, "limit": 200})
    assert response.status_code == 200, response.text
    docs = {d["sicarUuid"]: d for d in response.json()["docs"]}
    assert docs[on_sale.sicar_uuid]["compareAtPrice"] == 150.0
    assert docs[on_sale.sicar_uuid]["saleEndsAt"] is not None
    assert expired.sicar_uuid not in docs

    detail = await api.get(f"/v1/products/{on_sale.sicar_uuid}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["compareAtPrice"] == 150.0

    plain_detail = (await api.get(f"/v1/products/{plain.sicar_uuid}")).json()
    assert plain_detail["compareAtPrice"] is None and plain_detail["saleEndsAt"] is None


async def test_cart_line_exposes_sale(api, db):
    on_sale, *_ = await _seed(db)
    response = await api.put("/v1/cart", json={"items": [{"uuid": on_sale.sicar_uuid, "quantity": 1}]})
    assert response.status_code == 200, response.text
    line = response.json()["items"][0]
    assert line["compareAtPrice"] == 150.0 and line["price"] == 120.0


def test_typesense_sale_until():
    assert search_index.sale_until(None, Decimal("10"), None) == 0
    assert search_index.sale_until(Decimal("10"), Decimal("10"), None) == 0
    assert search_index.sale_until(Decimal("12"), Decimal("10"), None) == search_index.SALE_NO_END
    ends = NOW + timedelta(days=1)
    assert search_index.sale_until(Decimal("12"), Decimal("10"), ends) == int(ends.timestamp())


def test_checkout_still_charges_price():
    product = make_product(price=Decimal("120.00"), compare_at_price=Decimal("150.00"))
    payload = build_order_payload({product.sicar_uuid: product}, {product.sicar_uuid: 2}, {"deliveryType": "PICKUP"}, 1, "lista", "contenido")
    line = payload["ecOrderDto"]["products"][0]
    assert line["priceTax"] == "120.00" and line["amountTax"] == "240.00"


async def test_admin_set_and_clear_sale(api, db):
    product = make_product(price=Decimal("100.00"))
    db.add(product)
    await db.flush()
    headers = await admin_headers(db, "staff")
    url = f"/v1/admin/products/{product.sicar_uuid}/sale"

    bad = await api.put(url, json={"compareAtPrice": 100}, headers=headers)
    assert bad.status_code == 422

    past = await api.put(url, json={"compareAtPrice": 150, "saleEndsAt": (NOW - timedelta(hours=1)).isoformat()}, headers=headers)
    assert past.status_code == 422

    ok = await api.put(url, json={"compareAtPrice": 150, "saleEndsAt": (NOW + timedelta(days=2)).isoformat()}, headers=headers)
    assert ok.status_code == 200, ok.text
    assert ok.json()["isOnSale"] is True and ok.json()["compareAtPrice"] == 150.0

    assert (await api.delete(url, headers=headers)).status_code == 204
    assert (await api.delete(url, headers=headers)).status_code == 204  # idempotente
    await db.refresh(product)
    assert product.compare_at_price is None and product.sale_ends_at is None

    assert (await api.put("/v1/admin/products/no-existe/sale", json={"compareAtPrice": 1}, headers=headers)).status_code == 404

    actions = (await db.execute(select(AdminAuditLog.action).where(AdminAuditLog.resource_id == product.sicar_uuid))).scalars().all()
    assert sorted(actions) == ["product.sale_clear", "product.sale_clear", "product.sale_set"]


async def test_admin_bulk_sale_by_percent(api, db):
    a = make_product(price=Decimal("80.00"))
    b = make_product(price=Decimal("0.00"))
    db.add_all([a, b])
    await db.flush()
    headers = await admin_headers(db, "staff")

    response = await api.put("/v1/admin/products/sale", headers=headers,
                             json={"productUuids": [a.sicar_uuid, b.sicar_uuid, "no-existe"], "compareAtPercent": 20})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"updated": 1, "notFound": ["no-existe"], "skipped": [b.sicar_uuid]}
    await db.refresh(a)
    assert a.compare_at_price == Decimal("100.00")  # 80 / (1 - 0.20)

    both = await api.put("/v1/admin/products/sale", headers=headers, json={"productUuids": [a.sicar_uuid], "compareAtPrice": 90, "compareAtPercent": 10})
    assert both.status_code == 422

    cleared = await api.request("DELETE", "/v1/admin/products/sale", headers=headers, json={"productUuids": [a.sicar_uuid]})
    assert cleared.status_code == 200 and cleared.json()["updated"] == 1
    on_sale = (await db.execute(select(Product.compare_at_price).where(Product.id == a.id))).scalar_one()
    assert on_sale is None


async def test_sale_change_marks_product_dirty_for_search(db):
    product = make_product(price=Decimal("100.00"))
    db.add(product)
    await db.flush()
    await db.execute(Product.__table__.update().where(Product.id == product.id).values(search_dirty_at=None))
    await db.execute(Product.__table__.update().where(Product.id == product.id).values(compare_at_price=Decimal("150.00")))
    dirty = (await db.execute(select(Product.search_dirty_at).where(Product.id == product.id))).scalar_one()
    assert dirty is not None
