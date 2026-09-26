"""app/services/search_service.py sin Typesense real: constructores puros, hydrate, pin de
SKU y el fallback a Postgres (typesense_client monkeypatcheado en la frontera del servicio,
mismo patron que test_refunds.py con Mercado Pago)."""
from decimal import Decimal

import pytest

from app.core.config import settings
from app.schemas.search import SearchFilter
from app.services import search_service, typesense_client
from app.services.search_service import build_filter_by, build_search_params
from tests.conftest import make_product


# --- Constructores puros ------------------------------------------------------------------

def test_filter_by_empty():
    assert build_filter_by() == ""


def test_filter_by_all_filters_combined():
    fb = build_filter_by(
        department_uuid="dep", category_uuid="cat", category_uuids=["t1", "t2"], vehicle_uuid="veh",
        brand="TRUPER", has_brand=True, in_stock=True,
    )
    assert fb == (
        "department_uuid:=`dep` && category_uuid:=`cat` && category_uuids:=[`t1`,`t2`] && "
        "vehicle_uuids:=`veh` && brand_lower:=`truper` && has_brand:=true && in_stock:=true"
    )


def test_filter_by_has_brand_false_and_backtick_stripping():
    assert build_filter_by(has_brand=False) == "has_brand:=false"
    assert build_filter_by(brand="Mar`ca") == "brand_lower:=`marca`"


RELEVANCE_MARTILLO = "_text_match:desc,_eval([(name_first:=[`martillo`]):3, (in_stock:=true):1]):desc,sales_count:desc"


def test_relevance_sort_boosts_query_words_as_product_type():
    params = build_search_params("truper martillo de bola", filter_by="", sort_by=None, limit=1, offset=0)
    assert params["sort_by"] == (
        "_text_match:desc,_eval([(name_first:=[`truper`,`martillo`,`bola`]):3, (in_stock:=true):1]):desc,sales_count:desc"
    )
    only_stopwords = build_search_params("de la", filter_by="", sort_by=None, limit=1, offset=0)
    assert only_stopwords["sort_by"] == "_text_match:desc,_eval(in_stock:=true):desc,sales_count:desc"


@pytest.mark.parametrize("sort_by, expected", [
    (None, RELEVANCE_MARTILLO),
    ("relevance", RELEVANCE_MARTILLO),
    ("price_asc", "price:asc,_text_match:desc"),
    ("price_desc", "price:desc,_text_match:desc"),
    ("name_asc", "name_sort:asc"),
])
def test_sort_mapping(sort_by, expected):
    params = build_search_params("martillo", filter_by="", sort_by=sort_by, limit=10, offset=0)
    assert params["sort_by"] == expected


def test_prefix_only_for_last_word_of_three_or_more_chars():
    assert build_search_params("martillo", filter_by="", sort_by=None, limit=1, offset=0)["prefix"] == "true"
    assert build_search_params("20v", filter_by="", sort_by=None, limit=1, offset=0)["prefix"] == "true"
    assert build_search_params("foco 10", filter_by="", sort_by=None, limit=1, offset=0)["prefix"] == "false"
    assert build_search_params("te", filter_by="", sort_by=None, limit=1, offset=0)["prefix"] == "false"


def test_search_params_include_filter_only_when_present():
    assert "filter_by" not in build_search_params("x", filter_by="", sort_by=None, limit=1, offset=0)
    params = build_search_params("x", filter_by="in_stock:=true", sort_by=None, limit=5, offset=10)
    assert params["filter_by"] == "in_stock:=true"
    assert (params["limit"], params["offset"]) == (5, 10)
    assert params["include_fields"] == "id"


# --- Hydrate y flujo completo con Typesense simulado ---------------------------------------

@pytest.fixture
def typesense_on(monkeypatch):
    monkeypatch.setattr(settings, "TYPESENSE_URL", "http://typesense.test:8108")
    monkeypatch.setattr(settings, "TYPESENSE_API_KEY", "k")
    typesense_client.reset_breaker()
    yield
    typesense_client.reset_breaker()


def _hits(*uuids, found=None):
    return {"found": len(uuids) if found is None else found, "hits": [{"document": {"id": u}} for u in uuids]}


async def _products(db, *specs):
    products = [make_product(**spec) for spec in specs]
    db.add_all(products)
    await db.flush()
    return products


async def test_hydrate_keeps_typesense_order_and_drops_inactive(db):
    a, b, gone = await _products(db, {"name": "A"}, {"name": "B"}, {"name": "C", "is_active": False})
    result = await search_service.hydrate(db, [b.sicar_uuid, "no-existe", gone.sicar_uuid, a.sicar_uuid])
    assert [p.sicar_uuid for p in result] == [b.sicar_uuid, a.sicar_uuid]


async def test_search_uses_typesense_order_total_and_live_price(db, typesense_on, monkeypatch):
    a, b = await _products(db, {"name": "Martillo A", "price": Decimal("10.00")}, {"name": "Martillo B"})
    captured = {}

    async def fake_multi_search(searches):
        captured["searches"] = searches
        return [_hits(b.sicar_uuid, a.sicar_uuid, found=37), _hits()]

    monkeypatch.setattr(typesense_client, "multi_search", fake_multi_search)
    result = await search_service.search(db, SearchFilter(q="Martillos", limit=10, in_stock=True))

    assert result["total"] == 37
    assert [p.sicar_uuid for p in result["docs"]] == [b.sicar_uuid, a.sicar_uuid]
    assert result["docs"][1].price == Decimal("10.00")  # de Postgres, no del indice
    main, pin = captured["searches"]
    assert main["q"] == "martillo"  # normalizado (singular)
    assert main["filter_by"] == "in_stock:=true"
    assert pin["filter_by"] == "sku_lower:=`martillos` && in_stock:=true"


async def test_exact_sku_pin_goes_first_and_counts_once(db, typesense_on, monkeypatch):
    pinned, other = await _products(db, {"sku": "UBP1 1/4"}, {"sku": "X-1"})

    async def not_in_text_results(searches):
        return [_hits(other.sicar_uuid, found=1), _hits(pinned.sicar_uuid)]

    monkeypatch.setattr(typesense_client, "multi_search", not_in_text_results)
    result = await search_service.search(db, SearchFilter(q="UBP1 1/4"))
    assert [p.sicar_uuid for p in result["docs"]] == [pinned.sicar_uuid, other.sicar_uuid]
    assert result["total"] == 2

    async def also_in_text_results(searches):
        return [_hits(other.sicar_uuid, pinned.sicar_uuid, found=2), _hits(pinned.sicar_uuid)]

    monkeypatch.setattr(typesense_client, "multi_search", also_in_text_results)
    result = await search_service.search(db, SearchFilter(q="UBP1 1/4"))
    assert [p.sicar_uuid for p in result["docs"]] == [pinned.sicar_uuid, other.sicar_uuid]
    assert result["total"] == 2


async def test_no_pin_after_first_page(db, typesense_on, monkeypatch):
    captured = {}

    async def fake(searches):
        captured["n"] = len(searches)
        return [_hits()]

    monkeypatch.setattr(typesense_client, "multi_search", fake)
    await search_service.search(db, SearchFilter(q="martillo", offset=60))
    assert captured["n"] == 1


async def test_unknown_taxonomy_node_returns_empty_without_calling_typesense(db, typesense_on, monkeypatch):
    async def must_not_be_called(searches):
        raise AssertionError("no debio llamar a Typesense")

    monkeypatch.setattr(typesense_client, "multi_search", must_not_be_called)
    result = await search_service.search(db, SearchFilter(q="martillo", taxonomy_uuid="no-existe"))
    assert result == {"total": 0, "docs": []}


# --- Fallback a Postgres --------------------------------------------------------------------

async def test_falls_back_to_postgres_when_typesense_fails(db, typesense_on, monkeypatch):
    (product,) = await _products(db, {"name": "Martillo de bola fallback"})

    async def boom(searches):
        raise typesense_client.TypesenseError("caido", 503)

    monkeypatch.setattr(typesense_client, "multi_search", boom)
    result = await search_service.search(db, SearchFilter(q="martillo de bola fallback"))
    assert [p.sicar_uuid for p in result["docs"]] == [product.sicar_uuid]


async def test_uses_postgres_when_typesense_not_configured(db, monkeypatch):
    monkeypatch.setattr(settings, "TYPESENSE_URL", None)
    (product,) = await _products(db, {"name": "Pinza de presion sin motor"})

    async def must_not_be_called(searches):
        raise AssertionError("no debio llamar a Typesense")

    monkeypatch.setattr(typesense_client, "multi_search", must_not_be_called)
    result = await search_service.search(db, SearchFilter(q="pinza de presion sin motor"))
    assert [p.sicar_uuid for p in result["docs"]] == [product.sicar_uuid]


async def test_suggest_returns_products_and_brands(db, typesense_on, monkeypatch):
    (product,) = await _products(db, {"name": "Martillo Truper", "brand": "Truper"})

    async def fake(searches):
        assert searches[0]["prefix"] == "true"
        assert searches[1]["facet_query"] == "brand:tru"
        return [_hits(product.sicar_uuid), {"facet_counts": [{"counts": [{"value": "Truper", "count": 812}]}]}]

    monkeypatch.setattr(typesense_client, "multi_search", fake)
    result = await search_service.suggest(db, "tru", 6)
    assert [p.sicar_uuid for p in result["products"]] == [product.sicar_uuid]
    assert result["brands"] == [{"name": "Truper", "count": 812}]


async def test_suggest_falls_back_without_brands(db, typesense_on, monkeypatch):
    (product,) = await _products(db, {"name": "Taladro sugerido fallback"})

    async def boom(searches):
        raise typesense_client.TypesenseError("caido")

    monkeypatch.setattr(typesense_client, "multi_search", boom)
    result = await search_service.suggest(db, "taladro sugerido fallback", 6)
    assert [p.sicar_uuid for p in result["products"]] == [product.sicar_uuid]
    assert result["brands"] == []
