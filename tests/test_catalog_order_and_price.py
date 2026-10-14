"""Orden por defecto (con existencia y con foto primero) y filtro/rango de precio en
POST /products y en la busqueda de Postgres (respaldo de POST /search)."""
import uuid as uuid_lib
from decimal import Decimal

import pytest

from app.core.config import settings
from app.services import catalog_service
from tests.conftest import make_product

IMG = "https://img.example/p.jpg"


@pytest.fixture
def brand():
    """Marca unica por test: aisla el conjunto filtrado de cualquier otro producto en la BD."""
    return f"Marca-{uuid_lib.uuid4().hex[:8]}"


async def _add(db, brand, **spec):
    product = make_product(brand=brand, **spec)
    db.add(product)
    await db.flush()
    return product


async def test_relevance_puts_in_stock_and_with_photo_first(api, db, brand):
    sold_out_photo = await _add(db, brand, name="D agotado con foto", stock=Decimal("0"), image_url=IMG, sales_count=Decimal("90"))
    in_stock_no_photo = await _add(db, brand, name="C con existencia sin foto", image_url=None, sales_count=Decimal("80"))
    sold_out_no_photo = await _add(db, brand, name="E agotado sin foto", stock=Decimal("0"), image_url="", sales_count=Decimal("99"))
    best = await _add(db, brand, name="B con existencia con foto", image_url=IMG, sales_count=Decimal("5"))
    tie = await _add(db, brand, name="A con existencia con foto", image_url=IMG, sales_count=Decimal("5"))
    # Reservado por completo: cuenta como agotado (available_stock), no por el stock fisico.
    reserved_out = await _add(db, brand, name="F reservado", stock=Decimal("2"), reserved=Decimal("2"), image_url=IMG)

    response = await api.post("/v1/products", json={"brand": brand, "sortBy": "relevance"})
    assert response.status_code == 200, response.text
    order = [d["sicarUuid"] for d in response.json()["docs"]]
    assert order == [
        tie.sicar_uuid, best.sicar_uuid,                # con existencia + foto (sales_count, name)
        in_stock_no_photo.sicar_uuid,                   # con existencia, sin foto
        sold_out_photo.sicar_uuid, reserved_out.sicar_uuid,  # agotados con foto (sales_count desc)
        sold_out_no_photo.sicar_uuid,                   # agotado sin foto, aunque sea el mas vendido
    ]


async def test_explicit_sorts_ignore_stock_and_photo(api, db, brand):
    cheap_sold_out = await _add(db, brand, price=Decimal("10.00"), stock=Decimal("0"), image_url=None)
    pricey = await _add(db, brand, price=Decimal("90.00"), image_url=IMG)
    response = await api.post("/v1/products", json={"brand": brand, "sortBy": "price_asc"})
    assert [d["sicarUuid"] for d in response.json()["docs"]] == [cheap_sold_out.sicar_uuid, pricey.sicar_uuid]


async def test_pagination_is_stable_across_ties(api, db, brand):
    products = [await _add(db, brand, name="Mismo nombre", image_url=IMG) for _ in range(7)]
    seen = []
    for offset in range(0, 7, 3):
        page = await api.post("/v1/products", json={"brand": brand, "sortBy": "relevance", "limit": 3, "offset": offset})
        assert page.json()["total"] == 7
        seen += [d["sicarUuid"] for d in page.json()["docs"]]
    assert sorted(seen) == sorted(p.sicar_uuid for p in products)


async def test_price_filter_inclusive_and_price_range_ignores_it(api, db, brand):
    zero = await _add(db, brand, price=Decimal("0.00"))  # sin precio: no se puede comprar, no cuenta para el rango
    low = await _add(db, brand, price=Decimal("12.00"))
    mid = await _add(db, brand, price=Decimal("100.00"))
    top = await _add(db, brand, price=Decimal("500.00"))
    await _add(db, brand, price=Decimal("8450.00"))

    unfiltered = (await api.post("/v1/products", json={"brand": brand})).json()
    assert unfiltered["priceRange"] == {"min": 12.0, "max": 8450.0}

    body = (await api.post("/v1/products", json={"brand": brand, "minPrice": 100, "maxPrice": 500, "sortBy": "price_asc"})).json()
    assert body["total"] == 2
    assert [d["sicarUuid"] for d in body["docs"]] == [mid.sicar_uuid, top.sicar_uuid]
    assert body["priceRange"] == {"min": 12.0, "max": 8450.0}

    # El filtro es sobre price tal cual: el de 0.00 tambien cae bajo maxPrice (como ya aparece hoy en el catalogo).
    only_max = (await api.post("/v1/products", json={"brand": brand, "maxPrice": 12, "sortBy": "price_asc"})).json()
    assert [d["sicarUuid"] for d in only_max["docs"]] == [zero.sicar_uuid, low.sicar_uuid]

    empty = (await api.post("/v1/products", json={"brand": brand, "minPrice": 9000})).json()
    assert empty["total"] == 0 and empty["docs"] == []
    assert empty["priceRange"] == {"min": 12.0, "max": 8450.0}


async def test_price_range_null_for_empty_set(api, db, brand):
    body = (await api.post("/v1/products", json={"brand": brand})).json()
    assert body == {"total": 0, "docs": [], "priceRange": None}


async def test_price_filter_uses_charged_price_not_compare_at(api, db, brand):
    on_sale = await _add(db, brand, price=Decimal("80.00"), compare_at_price=Decimal("150.00"))
    body = (await api.post("/v1/products", json={"brand": brand, "maxPrice": 100})).json()
    assert [d["sicarUuid"] for d in body["docs"]] == [on_sale.sicar_uuid]


@pytest.mark.parametrize("payload", [{"minPrice": -1}, {"maxPrice": -0.01}, {"minPrice": 500, "maxPrice": 100}])
async def test_invalid_price_bounds_are_422(api, payload):
    assert (await api.post("/v1/products", json=payload)).status_code == 422
    assert (await api.post("/v1/search", json={"q": "x", **payload})).status_code == 422


# --- Busqueda de Postgres (respaldo de /search) ----------------------------------------------

async def test_postgres_search_text_match_first_then_stock_and_photo(db, brand):
    exact_sold_out = await _add(db, brand, name="Pinza zorbex", stock=Decimal("0"), image_url=None)
    other_in_stock = await _add(db, brand, name="Juego con pinza zorbex", image_url=IMG)
    contains_no_photo = await _add(db, brand, name="Juego zorbex con pinza", image_url=None)
    result = await catalog_service.search_products(db, "pinza zorbex", 10, 0, brand=brand)
    # Frase completa (los dos primeros) manda sobre existencia: el agotado que la contiene
    # queda arriba del que tiene existencia pero solo trae las palabras sueltas. Dentro de la
    # frase completa, existencia desempata.
    assert [p.sicar_uuid for p in result["docs"]] == [
        other_in_stock.sicar_uuid, exact_sold_out.sicar_uuid, contains_no_photo.sicar_uuid,
    ]


async def test_postgres_search_price_filter_and_range(db, brand):
    cheap = await _add(db, brand, name="Taladro qwertz chico", price=Decimal("50.00"))
    await _add(db, brand, name="Taladro qwertz grande", price=Decimal("900.00"))
    result = await catalog_service.search_products(db, "taladro qwertz", 10, 0, brand=brand, max_price=Decimal("100"))
    assert [p.sicar_uuid for p in result["docs"]] == [cheap.sicar_uuid]
    assert result["price_range"] == {"min": 50.0, "max": 900.0}


async def test_postgres_search_no_typo_fallback_when_only_price_excludes(db, brand):
    await _add(db, brand, name="Esmeriladora plmkj", price=Decimal("900.00"))
    result = await catalog_service.search_products(db, "esmeriladora plmkj", 10, 0, brand=brand, max_price=Decimal("10"))
    assert result["total"] == 0 and result["docs"] == []
    assert result["price_range"] == {"min": 900.0, "max": 900.0}


async def test_search_endpoint_returns_price_range_on_postgres(api, db, brand, monkeypatch):
    monkeypatch.setattr(settings, "TYPESENSE_URL", None)
    await _add(db, brand, name="Nivel asdfgh", price=Decimal("33.00"))
    body = (await api.post("/v1/search", json={"q": "nivel asdfgh", "brand": brand})).json()
    assert body["total"] == 1 and body["priceRange"] == {"min": 33.0, "max": 33.0}
