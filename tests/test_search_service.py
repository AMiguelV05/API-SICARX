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


def _eval_of(*criteria):
    """Las 2^n - 1 combinaciones, de la mas alta a la mas baja (ver _tiebreak_eval)."""
    n = len(criteria)
    clauses = []
    for mask in range(2**n - 1, 0, -1):
        terms = [c for i, c in enumerate(criteria) if mask & (1 << (n - 1 - i))]
        clauses.append(f"({' && '.join(terms)}):{mask}")
    return f"_eval([{', '.join(clauses)}]):desc"


RELEVANCE_MARTILLO = (
    "_text_match:desc,"
    "_eval([(name_first:=[`martillo`] && in_stock:=true && has_image:=true):7, "
    "(name_first:=[`martillo`] && in_stock:=true):6, (name_first:=[`martillo`] && has_image:=true):5, "
    "(name_first:=[`martillo`]):4, (in_stock:=true && has_image:=true):3, (in_stock:=true):2, (has_image:=true):1]):desc,"
    "sales_count:desc"
)


def test_relevance_sort_boosts_query_words_as_product_type():
    params = build_search_params("truper martillo de bola", filter_by="", sort_by=None, limit=1, offset=0)
    assert params["sort_by"] == (
        "_text_match:desc,"
        + _eval_of("name_first:=[`truper`,`martillo`,`bola`]", "in_stock:=true", "has_image:=true")
        + ",sales_count:desc"
    )
    only_stopwords = build_search_params("de la", filter_by="", sort_by=None, limit=1, offset=0)
    assert only_stopwords["sort_by"] == (
        "_text_match:desc,_eval([(in_stock:=true && has_image:=true):3, (in_stock:=true):2, (has_image:=true):1]):desc,sales_count:desc"
    )


def test_filter_by_price_range():
    assert build_filter_by(min_price=Decimal("100"), max_price=Decimal("500")) == "price:>=100.0 && price:<=500.0"
    assert build_filter_by(in_stock=True, max_price=Decimal("99.5")) == "in_stock:=true && price:<=99.5"


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


def _stats(min_price=None, max_price=None):
    """Respuesta de la busqueda de stats de precio (facet_by price, per_page 0)."""
    if min_price is None:
        return {"found": 0, "hits": [], "facet_counts": []}
    return {"found": 3, "hits": [], "facet_counts": [
        {"field_name": "price", "counts": [], "stats": {"min": min_price, "max": max_price}},
    ]}


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
        return [_hits(b.sicar_uuid, a.sicar_uuid, found=37), _stats(9.5, 870.0), _hits()]

    monkeypatch.setattr(typesense_client, "multi_search", fake_multi_search)
    result = await search_service.search(db, SearchFilter(q="Martillos", limit=10, in_stock=True))

    assert result["total"] == 37
    assert [p.sicar_uuid for p in result["docs"]] == [b.sicar_uuid, a.sicar_uuid]
    assert result["docs"][1].price == Decimal("10.00")  # de Postgres, no del indice
    assert result["price_range"] == {"min": 9.5, "max": 870.0}
    main, stats, pin = captured["searches"]
    assert stats["filter_by"] == "in_stock:=true && price:>0"
    assert (stats["per_page"], stats["facet_by"], stats["q"]) == (0, "price", "martillo")
    assert main["q"] == "martillo"  # normalizado (singular)
    assert main["filter_by"] == "in_stock:=true"
    assert pin["filter_by"] == "sku_lower:=`martillos` && in_stock:=true"


async def test_exact_sku_pin_goes_first_and_counts_once(db, typesense_on, monkeypatch):
    pinned, other = await _products(db, {"sku": "UBP1 1/4"}, {"sku": "X-1"})

    async def not_in_text_results(searches):
        return [_hits(other.sicar_uuid, found=1), _stats(), _hits(pinned.sicar_uuid)]

    monkeypatch.setattr(typesense_client, "multi_search", not_in_text_results)
    result = await search_service.search(db, SearchFilter(q="UBP1 1/4"))
    assert [p.sicar_uuid for p in result["docs"]] == [pinned.sicar_uuid, other.sicar_uuid]
    assert result["total"] == 2

    async def also_in_text_results(searches):
        return [_hits(other.sicar_uuid, pinned.sicar_uuid, found=2), _stats(), _hits(pinned.sicar_uuid)]

    monkeypatch.setattr(typesense_client, "multi_search", also_in_text_results)
    result = await search_service.search(db, SearchFilter(q="UBP1 1/4"))
    assert [p.sicar_uuid for p in result["docs"]] == [pinned.sicar_uuid, other.sicar_uuid]
    assert result["total"] == 2


async def test_no_pin_after_first_page(db, typesense_on, monkeypatch):
    captured = {}

    async def fake(searches):
        captured["n"] = len(searches)
        return [_hits(), _stats()]

    monkeypatch.setattr(typesense_client, "multi_search", fake)
    await search_service.search(db, SearchFilter(q="martillo", offset=60))
    assert captured["n"] == 2  # principal + stats de precio, sin pin


async def test_unknown_taxonomy_node_returns_empty_without_calling_typesense(db, typesense_on, monkeypatch):
    async def must_not_be_called(searches):
        raise AssertionError("no debio llamar a Typesense")

    monkeypatch.setattr(typesense_client, "multi_search", must_not_be_called)
    result = await search_service.search(db, SearchFilter(q="martillo", taxonomy_uuid="no-existe"))
    assert result == {"total": 0, "docs": [], "price_range": None}


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


async def test_price_filter_goes_to_main_and_pin_but_not_to_stats(db, typesense_on, monkeypatch):
    captured = {}

    async def fake(searches):
        captured["searches"] = searches
        return [_hits(), _stats(), _hits()]

    monkeypatch.setattr(typesense_client, "multi_search", fake)
    result = await search_service.search(db, SearchFilter(q="martillo", min_price=100, max_price=500))
    main, stats, pin = captured["searches"]
    assert main["filter_by"] == "price:>=100.0 && price:<=500.0"
    assert pin["filter_by"].endswith("&& price:>=100.0 && price:<=500.0")
    assert stats["filter_by"] == "price:>0"
    assert result["price_range"] is None  # sin resultados


def test_price_bounds_validation():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SearchFilter(q="x", min_price=-1)
    with pytest.raises(ValidationError):
        SearchFilter(q="x", min_price=500, max_price=100)
    assert SearchFilter(q="x", min_price=100, max_price=100).max_price == 100
