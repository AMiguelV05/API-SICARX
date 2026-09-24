"""Cubre brand_service.py (GET/POST/DELETE /admin/brands, PATCH /admin/products/brand) y el
filtro publico hasBrand de catalog_service. Matching case-insensitive via lower(brand);
normalize_brand recorta y trata "" como null."""
import uuid

import pytest
from fastapi import HTTPException

from app.services import brand_service, catalog_service
from tests.conftest import make_product


def _unique_brand() -> str:
    # Marca unica por test - la BD de pruebas puede tener otros productos con marca.
    return f"Marca{uuid.uuid4().hex[:8]}"


def test_normalize_brand():
    assert brand_service.normalize_brand("  Surtek ") == "Surtek"
    assert brand_service.normalize_brand("   ") is None
    assert brand_service.normalize_brand("") is None
    assert brand_service.normalize_brand(None) is None


async def test_list_brands_groups_case_variants(db):
    brand = _unique_brand()
    db.add_all([
        make_product(brand=brand),
        make_product(brand=brand.upper()),
        make_product(brand=brand.upper()),
        make_product(brand=brand, is_deleted=True),  # no cuenta
        make_product(brand=brand, is_active=False),  # si cuenta - vista admin
    ])
    await db.flush()

    docs, _ = await brand_service.list_brands(db)
    group = next(d for d in docs if d["name"].lower() == brand.lower())
    assert group["product_count"] == 4
    assert group["variants"] == sorted([brand, brand.upper()])
    # name = MIN(brand) segun la collation de Postgres (en_US: minusculas antes que
    # mayusculas), no el orden por codepoint de Python - solo se garantiza que sea una de
    # las variantes del grupo.
    assert group["name"] in group["variants"]


async def test_list_brands_unbranded_count(db):
    _, before = await brand_service.list_brands(db)
    db.add_all([make_product(brand=None), make_product(brand=None), make_product(brand=None, is_deleted=True)])
    await db.flush()
    _, after = await brand_service.list_brands(db)
    assert after - before == 2


async def test_set_brand_for_products_trims_and_reports_not_found(db):
    p1, p2 = make_product(), make_product()
    deleted = make_product(is_deleted=True)
    db.add_all([p1, p2, deleted])
    await db.flush()
    missing = str(uuid.uuid4())

    updated, not_found = await brand_service.set_brand_for_products(
        db, [p1.sicar_uuid, p2.sicar_uuid, p1.sicar_uuid, missing, deleted.sicar_uuid], "  Truper  "
    )
    assert updated == 2
    assert not_found == [missing, deleted.sicar_uuid]

    await db.refresh(p1)
    await db.refresh(p2)
    assert p1.brand == "Truper" and p2.brand == "Truper"


async def test_set_brand_for_products_empty_string_clears(db):
    product = make_product(brand="Truper")
    db.add(product)
    await db.flush()

    updated, _ = await brand_service.set_brand_for_products(db, [product.sicar_uuid], "")
    assert updated == 1
    await db.refresh(product)
    assert product.brand is None


async def test_rename_brand_merges_case_insensitively(db):
    brand = _unique_brand()
    target = brand + "X"
    a = make_product(brand=brand.upper())
    b = make_product(brand=brand.lower())
    c = make_product(brand=target)
    db.add_all([a, b, c])
    await db.flush()

    updated = await brand_service.rename_brand(db, brand, target)
    assert updated == 2
    for p in (a, b, c):
        await db.refresh(p)
        assert p.brand == target


async def test_rename_brand_404_when_nothing_matches(db):
    with pytest.raises(HTTPException) as exc:
        await brand_service.rename_brand(db, _unique_brand(), "Otra")
    assert exc.value.status_code == 404


async def test_clear_brand(db):
    brand = _unique_brand()
    a, b = make_product(brand=brand), make_product(brand=brand.upper())
    db.add_all([a, b])
    await db.flush()

    assert await brand_service.clear_brand(db, brand) == 2
    await db.refresh(a)
    assert a.brand is None
    assert await brand_service.clear_brand(db, brand) == 0


async def test_catalog_has_brand_filter(db):
    tag = f"tag-{uuid.uuid4().hex[:8]}"  # aisla los productos de este test
    brand = _unique_brand()
    branded = make_product(brand=brand, tags=[tag])
    unbranded = make_product(brand=None, tags=[tag])
    db.add_all([branded, unbranded])
    await db.flush()

    async def uuids(**filters):
        result = await catalog_service.get_local_catalog(db, {"tag": tag, **filters})
        return {p.sicar_uuid for p in result["docs"]}

    assert await uuids(has_brand=False) == {unbranded.sicar_uuid}
    assert await uuids(has_brand=True) == {branded.sicar_uuid}
    assert await uuids() == {branded.sicar_uuid, unbranded.sicar_uuid}
    assert await uuids(has_brand=False, brand=brand) == set()


async def test_search_has_brand_filter(db):
    word = f"zq{uuid.uuid4().hex[:8]}"
    branded = make_product(name=f"Martillo {word}", brand=_unique_brand())
    unbranded = make_product(name=f"Pinza {word}", brand=None)
    db.add_all([branded, unbranded])
    await db.flush()

    result = await catalog_service.search_products(db, word, 60, 0, has_brand=False)
    assert {p.sicar_uuid for p in result["docs"]} == {unbranded.sicar_uuid}
    result = await catalog_service.search_products(db, word, 60, 0, has_brand=True)
    assert {p.sicar_uuid for p in result["docs"]} == {branded.sicar_uuid}
