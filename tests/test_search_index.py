"""Unidades puras de app/services/search_index.py - normalizacion de texto y documentos. Los
ejemplos son nombres reales del catalogo usados en la Fase 0 (ver la spec, "Phase 0 results")."""
from decimal import Decimal

import pytest

from app.services.search_index import (
    build_schema,
    compact_sku,
    compact_tokens,
    name_first,
    name_head,
    normalize_search_text,
    singular,
    to_document,
)
from app.services.synonym_service import normalize_synonym_word


@pytest.mark.parametrize("raw, expected", [
    # Fracciones mixtas -> un solo token, con guion, espacio o comillas.
    ("Adaptador macho de PVC de 1-1/2', FOSET", "adaptador macho de pvc de 1_1/2', foset"),
    ("TEE PVC 1 1/2\"", "tee pvc 1_1/2\""),
    ("adaptador pvc 1 1/2", "adaptador pvc 1_1/2"),
    # Fraccion simple intacta.
    ("broca 1/4", "broca 1/4"),
    # Unidades pegadas al numero, en ambas grafias.
    ("Dado cuadro 1/2' de impacto 6 puntas de 10 mm", "dado cuadro 1/2' de impacto 6 punta de 10mm"),
    ("dado 10mm", "dado 10mm"),
    ("Taladro 1/2', 20 V, 2 baterías", "taladro 1/2', 20v, 2 bateria"),
    ("Foco LED 10W (75 W)", "foco led 10w (75w)"),
    ("Cinta 19.5 mm", "cinta 19.5mm"),
    # SKUs con letras pegadas no se tocan.
    ("U1222V", "u1222v"),
    ("am3165", "am3165"),
    # Acentos fuera; singular.
    ("Llave española", "llave espanola"),
    ("Silicón", "silicon"),
    ("martillos", "martillo"),
    ("llaves allen", "llave allen"),
    ("pijas", "pija"),
    ("focos led", "foco led"),
    ("luces led", "luz led"),
    ("motores", "motor"),
    ("tees pvc", "tee pvc"),
    ("", ""),
    (None, ""),
])
def test_normalize_search_text(raw, expected):
    assert normalize_search_text(raw) == expected


@pytest.mark.parametrize("word, expected", [
    ("llaves", "llave"), ("pinzas", "pinza"), ("desarmadores", "desarmador"), ("rieles", "riel"),
    ("botones", "boton"), ("paredes", "pared"), ("cruces", "cruz"),
    # Cortas, sin plural reconocible o no alfabeticas: intactas.
    ("gas", "gas"), ("leds", "leds"), ("pvc", "pvc"), ("20v", "20v"),
    # Singular "falso" pero inocuo: se aplica igual al indice y a la consulta, y no
    # colisiona con otra palabra real. Endurecer la regla romperia "tees" -> "tee".
    ("tres", "tre"),
    ("martillo", "martillo"),
])
def test_singular(word, expected):
    assert singular(word) == expected


def test_normalization_is_idempotent():
    for raw in ["Adaptador macho de PVC de 1-1/2', FOSET", "Taladro 1/2', 20 V, 2 baterías", "llaves allen"]:
        once = normalize_search_text(raw)
        assert normalize_search_text(once) == once


def test_compact_tokens():
    assert compact_tokens("AFLOJATODO WD-40 6 oz, FLEXITAPE") == ["wd40"]
    assert compact_tokens("Llave T-10 y X-2000") == ["t10", "x2000"]
    assert compact_tokens("Martillo 16 oz") == []
    assert compact_tokens(None) == []


def test_compact_sku():
    assert compact_sku("UBSD1-1/4") == "ubsd114"
    assert compact_sku("51-0025-001") == "510025001"
    assert compact_sku("UBP1 1/4") == "ubp114"
    assert compact_sku(None) == ""


@pytest.mark.parametrize("name, expected", [
    ("Martillo 16 oz uña curva", "martillo"),
    ("Engrapadora tipo martillo, uso rudo", "engrapadora"),
    ('"9V" Pila alcalina', "9v"),
    ("1/2 Dado largo", "1/2"),
    ("", ""),
])
def test_name_first(name, expected):
    assert name_first(normalize_search_text(name)) == expected


def test_name_head():
    assert name_head("dado largo impacto de 9/16'") == "dado largo impacto"
    assert name_head("martillo") == "martillo"


def test_synonym_words_use_the_same_normalization():
    assert normalize_synonym_word("  Inalámbrico ") == "inalambrico"
    assert normalize_synonym_word("Baterías") == "bateria"
    assert normalize_synonym_word("20 V") == "20v"
    assert normalize_synonym_word("Llave   Allen") == "llave allen"


def _row(**overrides):
    row = dict(
        id=1, sicar_uuid="uuid-1", sku="U1222V", additional_skus=None, name="Llave española 1000 V",
        brand="Urrea", description=None, department_uuid="dep", category_uuid="cat",
        price=Decimal("123.45"), stock=Decimal("5"), reserved=Decimal("0"), sales_count=Decimal("7"),
        category_uuids=None, vehicle_uuids=None,
    )
    row.update(overrides)
    return row


def test_to_document_basic_fields():
    doc = to_document(_row())
    assert doc["id"] == "uuid-1"
    assert doc["name"] == "llave espanola 1000v"
    assert doc["name_head"] == "llave espanola 1000v"
    assert doc["sku_compact"] == "u1222v"
    assert doc["sku_lower"] == "u1222v"
    assert doc["brand"] == "Urrea" and doc["brand_lower"] == "urrea" and doc["has_brand"] is True
    assert doc["price"] == pytest.approx(123.45)
    assert doc["sales_count"] == 7
    assert doc["in_stock"] is True
    assert doc["name_first"] == "llave"
    assert doc["category_uuids"] == [] and doc["vehicle_uuids"] == []
    assert doc["description"] is None
    assert doc["name_sort"] == "llave espanola 1000 v"


def test_to_document_no_brand_sold_out_and_links():
    doc = to_document(_row(
        brand=None, stock=Decimal("3"), reserved=Decimal("3"),
        category_uuids=["c2", "c1"], vehicle_uuids=["v1"], additional_skus=["AB-12", "", 5],
        description="Juego de llaves",
    ))
    assert doc["has_brand"] is False and doc["brand_lower"] is None
    assert doc["in_stock"] is False
    assert doc["category_uuids"] == ["c1", "c2"]
    assert doc["vehicle_uuids"] == ["v1"]
    assert doc["additional_skus"] == ["ab12"]
    assert doc["description"] == "juego de llave"


def test_to_document_null_stock_and_price():
    doc = to_document(_row(stock=None, reserved=None, price=None, sales_count=None))
    assert doc["in_stock"] is False
    assert doc["price"] == 0.0
    assert doc["sales_count"] == 0


def test_schema_fields_match_documents():
    """Todo campo que produce to_document existe en el esquema, y viceversa."""
    schema_fields = {f["name"] for f in build_schema("x")["fields"]}
    doc_fields = set(to_document(_row())) - {"id"}
    assert schema_fields == doc_fields
