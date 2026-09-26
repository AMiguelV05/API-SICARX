"""Relevancia de punta a punta contra un Typesense REAL: un catalogo chico que reproduce cada
problema encontrado en el sitio en vivo (F1-F10 en la spec, "Live-site findings") y los
casos que ya funcionaban y no deben empeorar. Mismos requisitos y limpieza que
test_search_index_integration.py (se salta sin TYPESENSE_URL/TYPESENSE_API_KEY)."""
import os
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import delete

from app.core import database
from app.core.config import settings
from app.models.product import Product
from app.schemas.search import SearchFilter
from app.services import search_index, search_service, typesense_client
from app.worker import search_index_worker
from tests.conftest import make_product

pytestmark = pytest.mark.skipif(
    not (os.environ.get("TYPESENSE_URL") and os.environ.get("TYPESENSE_API_KEY")),
    reason="Typesense no configurado (TYPESENSE_URL/TYPESENSE_API_KEY)",
)

# (clave, overrides de make_product)
CATALOG = [
    ("pvc_macho", dict(name='ADAPTADOR MACHO PVC 1 1/2"', sku="PADAPM38")),
    ("cpvc_macho", dict(name='ADAPTADOR MACHO CPVC 1"', sku="CADAPM25")),
    ("adaptador_dado", dict(name='Adaptador con balín para dado de impacto cuadro de 1/2" hembra', sku="ADB12")),
    ("dado_12410", dict(name="Dado cuadro 1/2' de impacto 6 puntas de 10 mm, TRUPER", sku="12410", brand="Truper")),
    ("dado_9mm", dict(name='Dado de impacto cuadro de 1/2", 6 puntas, métrico, 9 mm', sku="U7409M", brand="Urrea")),
    ("rotomartillo", dict(name='Adaptador SDS para rotomartillo 1/2"', sku="SDS12")),
    ("martillo", dict(name="Martillo 16 oz uña curva, TRUPER", sku="16704", brand="Truper")),
    ("mango", dict(name="Mango de fibra 54' para azadones y martillo, TRUPER", sku="15921", brand="Truper")),
    # Empata en texto con "martillo" (name_head la contiene); el desempate por tipo de
    # producto (name_first) debe ponerla despues del martillo real.
    ("engrapadora", dict(name="Engrapadora tipo martillo, uso rudo, TRUPER", sku="100334", brand="Truper")),
    ("cable_yamaha", dict(name="CABLE VELOCIMETRO YAMAHA YBR 125", sku="F13012410")),
    ("casco", dict(name="CASCO GHIRA KIDS NEGRO AMARILLO", sku="GH-KIDS")),
    ("hexagonal", dict(name='Juego de llaves hexagonales tipo "L", métricas, 7 piezas', sku="UALLF7M", brand="Urrea")),
    ("llave_10", dict(name="Llave combinada 10 mm x 140 mm de largo, PRETUL", sku="21904", brand="Pretul")),
    ("candado", dict(name="Candado de cable con llave, 10 mm X 1.0 m, HERMEX", sku="43922", brand="Hermex")),
    ("taladro", dict(name="Taladro 1/2', 20 V, 2 baterías 2 Ah, TRUPER", sku="101452", brand="Truper")),
    ("wd40", dict(name="LUBRICANTE AFLOJATODO WD-40 11 OZ.", sku="0032-0016")),
    ("desarmador", dict(name="Desarmador de caja 1/4' mango de acetato, TRUPER", sku="14122", brand="Truper")),
    ("broca_14", dict(name="Broca HSS 1/4' Trublack para metal, TRUPER", sku="15097", brand="Truper")),
    ("broca_114", dict(name='Broca de acero de alta velocidad zanco reducido 1-1/4"', sku="UBSD1-1/4", brand="Urrea")),
    ("foco", dict(name="Foco LED 10W (75W) A19, 6500K", sku="28063")),
    ("focos_pack", dict(name="Pack 4 focos LED 6 W (40 W) A19", sku="28004")),
    ("pinza", dict(name='Pinza de presión 10", TRUPER', sku="17448", brand="Truper")),
    ("tee_agotado", dict(name='TEE PVC 1"', sku="PTEE25", stock=Decimal("0"))),
    ("tee_disponible", dict(name='TEE PVC 3/4"', sku="PTEE19", stock=Decimal("5"))),
    ("llave_espanola", dict(name="Llave española 16 x 17 x 198 mm de largo, TRUPER", sku="15714", brand="Truper")),
    ("cinta", dict(name="Cinta de aislar de 18 m x 19 mm, gris, TRUPER", sku="12507", brand="Truper")),
]


@pytest.fixture
async def indexed(monkeypatch):
    alias = f"test_relevance_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(search_index, "ALIAS", alias)
    typesense_client.reset_breaker()
    suffix = uuid.uuid4().hex[:6]
    by_key = {}
    async with database.AsyncSessionLocal() as session:
        for key, overrides in CATALOG:
            # SKU unico por corrida (la columna no es unica, pero asi no choca con otras pruebas);
            # el pin de SKU exacto se prueba aparte con el SKU tal cual.
            product = make_product(**overrides)
            session.add(product)
            by_key[key] = product
        await session.commit()
    try:
        await search_index_worker.full_rebuild("prueba de relevancia")
        yield by_key
    finally:
        collection = await typesense_client.get_alias(alias)
        if collection:
            await typesense_client._request("DELETE", f"/aliases/{alias}", allow_404=True)
            await typesense_client.delete_collection(collection)
        async with database.AsyncSessionLocal() as session:
            await session.execute(delete(Product).where(Product.sicar_uuid.in_([p.sicar_uuid for p in by_key.values()])))
            await session.commit()
        await database.engine.dispose()


async def _search(q: str, **filters) -> list[str]:
    async with database.AsyncSessionLocal() as session:
        result = await search_service.search(session, SearchFilter(q=q, limit=20, **filters))
    return [p.sicar_uuid for p in result["docs"]]


async def test_live_site_findings_are_fixed(indexed):
    p = {k: v.sicar_uuid for k, v in indexed.items()}
    # (consulta, clave esperada en 1er lugar, claves que NO deben aparecer, descripcion)
    cases = [
        ("adaptador pvc macho", "pvc_macho", ["cpvc_macho"], "F1: pvc no matchea CPVC"),
        ("martillo", "martillo", ["rotomartillo"], "F1: sin coincidencias dentro de otra palabra"),
        ("matillo", "martillo", ["casco"], "F1/F7: el typo no matchea 'amarillo'"),
        ("12410", "dado_12410", [], "F2: SKU exacto primero"),
        ("UBSD1-1/4", "broca_114", [], "F2: SKU con fraccion (pin)"),
        ("0032-0016", "wd40", [], "F2: SKU con guiones"),
        ("dado de impacto", None, [], "F3: un dado primero, no el adaptador"),
        ("broca 1/4", "broca_14", [], "F3/F9: 1/4 no es 1-1/4"),
        ("martillos", "martillo", [], "F4: plural"),
        ("dado 10mm", "dado_12410", [], "F5: unidad junta"),
        ("dado 10 mm", "dado_12410", [], "F5: unidad separada"),
        ("llave 10mm", "llave_10", [], "F3/F5: la llave antes que el candado"),
        ("truper martillo", "martillo", [], "F8: orden de palabras"),
        ("martillo truper", "martillo", [], "F8: orden de palabras"),
        ("adaptador pvc 1-1/2", "pvc_macho", [], "F9: fraccion mixta con guion"),
        ("adaptador pvc 1 1/2", "pvc_macho", [], "F9: fraccion mixta con espacio"),
        ("llave allen", "hexagonal", [], "F10: sinonimo allen/hexagonal"),
        ("llaves allen", "hexagonal", [], "F10: sinonimo + plural"),
        ("destornillador", "desarmador", [], "F10: sinonimo desarmador/destornillador"),
        ("taladro inalambrico", "taladro", [], "F10: sinonimo de una direccion inalambrico -> 20v/bateria"),
        ("pinsas", "pinza", [], "F7: typo"),
        ("wd40", "wd40", [], "sin regresion: wd40"),
        ("wd-40", "wd40", [], "sin regresion: wd-40"),
        ("llave española", "llave_espanola", [], "sin regresion: acentos"),
        ("cinta de aislar", "cinta", [], "sin regresion"),
        ("tee pvc", "tee_disponible", [], "F6: disponible antes que agotado"),
    ]
    failures = []
    for q, expected_first, forbidden, why in cases:
        ids = await _search(q)
        if expected_first is None:  # "dado de impacto": cualquiera de los dos dados, no el adaptador
            ok = bool(ids) and ids[0] in (p["dado_12410"], p["dado_9mm"])
        else:
            ok = bool(ids) and ids[0] == p[expected_first]
        bad = [k for k in forbidden if p[k] in ids]
        if not ok or bad:
            names = {v: k for k, v in p.items()}
            failures.append(f"{q!r} ({why}): top={[names.get(i, i) for i in ids[:3]]} prohibidos presentes={bad}")
    assert not failures, "\n".join(failures)


async def test_entrada_synonym_finds_half_inch_impact_sockets(indexed):
    ids = await _search("dado de impacto de entrada 1/2")
    assert indexed["dado_12410"].sicar_uuid in ids[:3]


async def test_filters_in_stock_and_brand(indexed):
    ids = await _search("tee pvc", in_stock=True)
    assert indexed["tee_disponible"].sicar_uuid in ids
    assert indexed["tee_agotado"].sicar_uuid not in ids

    ids = await _search("martillo", brand="TRUPER")
    truper_martillo_like = {indexed[k].sicar_uuid for k in ("martillo", "mango", "engrapadora")}
    assert ids and all(i in truper_martillo_like for i in ids)

    ids = await _search("adaptador", has_brand=False)
    assert indexed["pvc_macho"].sicar_uuid in ids


async def test_suggest_returns_products_and_brands(indexed):
    async with database.AsyncSessionLocal() as session:
        result = await search_service.suggest(session, "tru", 6)
    truper_count = sum(1 for _, o in CATALOG if o.get("brand") == "Truper")
    assert {"name": "Truper", "count": truper_count} in result["brands"]
    assert result["products"], "debio sugerir productos de Truper"


async def test_http_endpoints(indexed):
    from app.main import app

    headers = {"x-api-key": settings.X_API_KEY}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/search", json={"q": "martillos", "limit": 5}, headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["docs"][0]["sicarUuid"] == indexed["martillo"].sicar_uuid
        assert set(body["docs"][0]) >= {"sicarUuid", "sku", "name", "price", "stock"}

        response = await client.get("/v1/search/suggest", params={"q": "tru", "limit": 3}, headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert len(body["products"]) <= 3
        assert set(body["products"][0]) == {"sicarUuid", "sku", "name", "imageUrl", "price"}
        assert body["brands"][0]["name"] == "Truper"

        assert (await client.get("/v1/search/suggest", params={"q": ""}, headers=headers)).status_code == 422
        assert (await client.get("/v1/search/suggest", params={"q": "x"})).status_code in (401, 403)


async def test_product_type_beats_word_elsewhere_in_name(indexed):
    """Con una consulta con typo, el martillo real va antes que la engrapadora "tipo
    martillo"; y "llave allen" (sinonimo) trae las llaves hexagonales, no las que solo
    mencionan "hexagonal" lejos de "llave"."""
    assert (await _search("matillo"))[0] == indexed["martillo"].sicar_uuid
    assert (await _search("llave allen"))[0] == indexed["hexagonal"].sicar_uuid
