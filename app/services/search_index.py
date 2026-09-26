"""Forma del indice de productos en Typesense: normalizacion de texto, esquema de la coleccion
y construccion de documentos desde Postgres. Ver CLAUDE.md, "Busqueda con Typesense", y
docs/superpowers/specs/2026-09-26-typesense-search-design.md (seccion 4 y "Phase 0 results").

`normalize_search_text` se aplica IDENTICO a los nombres indexados, a la consulta y a las
palabras de los sinonimos - es el equivalente de search_normalize() de Postgres. Reemplaza al
`stem` propio de Typesense, descartado en la Fase 0: con locale "es" no hace nada, y con el
locale por defecto rompe la tolerancia a typos y los sinonimos en palabras en plural."""
import re
import time
import unicodedata
from typing import Any, Iterable, Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Alias estable al que apuntan api y worker; la coleccion fisica detras cambia en cada
# reconstruccion (products_v{SCHEMA_VERSION}_{timestamp}). Se lee en tiempo de llamada
# (no como default de argumento) para que las pruebas de integracion puedan cambiarlo.
ALIAS = "products"
# Subirlo cuando cambie build_schema o to_document: el worker detecta al arrancar que el
# alias apunta a una version vieja y reconstruye el indice completo.
SCHEMA_VERSION = 1

STOPWORDS_ID = "es"
STOPWORDS = ["de", "del", "la", "el", "los", "las", "para", "con", "y", "en"]

_UNITS = r"(mm|cm|m|v|w|ah|oz|lb|kg|g|hp)"
# Solo numeros "sueltos" (no pegados a letras, ni parte de otra fraccion): asi un SKU como
# u1222v queda intacto.
_MEASURE_RE = re.compile(r"(?<![\w/])(\d+(?:[.,]\d+)?)\s*" + _UNITS + r"\b")
_MIXED_FRACTION_RE = re.compile(r"(?<![\w/])(\d+)[\s-]+(\d+/\d+)")
_WORD_RE = re.compile(r"[a-z]+")
_HYPHEN_JOIN_RE = re.compile(r"\b([a-z]+\d*|\d+[a-z]+)-(\w+)\b")
_SKU_STRIP_RE = re.compile(r"[-\s/._]")
# Consonantes tras las que un plural en -es pierde "es" completo (motores -> motor).
_ES_PLURAL_CONSONANTS = set("rlndzj")


def collection_prefix() -> str:
    return f"{ALIAS}_v{SCHEMA_VERSION}_"


def new_collection_name() -> str:
    # Milisegundos, no segundos: dos reconstrucciones en el mismo segundo (arranque + una
    # manual) chocarian con "collection already exists".
    return f"{collection_prefix()}{int(time.time() * 1000)}"


def strip_accents(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", value) if unicodedata.category(c) != "Mn")


def singular(word: str) -> str:
    """Singular ingenuo del espanol para una palabra ya en minusculas y sin acentos.
    Deterministico y aplicado igual en ambos lados, asi que un singular "incorrecto" solo
    importa si llega a fusionar dos palabras realmente distintas."""
    if len(word) <= 3 or not word.isalpha():
        return word
    if word.endswith("ces") and len(word) > 4:  # luces -> luz, cruces -> cruz
        return word[:-3] + "z"
    if word.endswith("es") and word[-3] in _ES_PLURAL_CONSONANTS and len(word) - 2 >= 3:
        return word[:-2]  # motores -> motor, rieles -> riel
    if word.endswith("s") and word[-2] in "aeiou":
        return word[:-1]  # llaves -> llave, pijas -> pija
    return word


def normalize_search_text(value: str | None) -> str:
    """Minusculas + sin acentos -> fraccion mixta como un solo token (1-1/2, 1 1/2 -> 1_1/2)
    -> numero y unidad pegados (10 mm -> 10mm) -> singular por palabra. Ver la seccion 4 de la
    spec para el porque de cada paso."""
    t = strip_accents(value or "").lower()
    t = _MIXED_FRACTION_RE.sub(r"\1_\2", t)
    t = _MEASURE_RE.sub(r"\1\2", t)
    return _WORD_RE.sub(lambda m: singular(m.group(0)), t)


def compact_tokens(name: str | None) -> list[str]:
    """Tokens unidos a traves de un guion cuando un lado tiene letras: 'WD-40' -> 'wd40'.
    Van a name_compact para que la consulta 'wd40' los encuentre sin typos."""
    t = strip_accents(name or "").lower()
    return sorted({a + b for a, b in _HYPHEN_JOIN_RE.findall(t)})


def compact_sku(sku: str | None) -> str:
    return _SKU_STRIP_RE.sub("", (sku or "").lower())


def name_head(normalized_name: str) -> str:
    """Primeras 3 palabras del nombre ya normalizado: la senal de "tipo de producto"."""
    return " ".join(normalized_name.split()[:3])


def build_schema(collection_name: str) -> dict[str, Any]:
    """Esquema de la coleccion. `name`/`name_head`/`description` guardan texto normalizado,
    no el de despliegue - el nombre que ve el cliente siempre sale del hydrate de Postgres."""
    optional_string = {"type": "string", "optional": True}
    return {
        "name": collection_name,
        "token_separators": ["-"],
        "symbols_to_index": ["/", "_"],
        "default_sorting_field": "sales_count",
        "fields": [
            {"name": "name", "type": "string"},
            {"name": "name_head", "type": "string"},
            {"name": "name_compact", "type": "string[]"},
            # Un solo token (sin separar por -, espacio, /...): "broca 1/4" ya no matchea SKUs
            # como UBSD1-1/4 por su parte "1/4".
            {"name": "sku_compact", "type": "string", "symbols_to_index": ["-", "/", "_", "."]},
            # Solo para el pin de SKU exacto (filter_by sku_lower:=...), nunca en query_by.
            {"name": "sku_lower", "type": "string"},
            {"name": "additional_skus", "type": "string[]", "symbols_to_index": ["-", "/", "_", "."]},
            {"name": "brand", "type": "string", "optional": True, "facet": True},
            {"name": "brand_lower", **optional_string},
            {"name": "has_brand", "type": "bool"},
            {"name": "description", **optional_string},
            {"name": "department_uuid", **optional_string},
            {"name": "category_uuid", **optional_string},
            {"name": "category_uuids", "type": "string[]"},
            {"name": "vehicle_uuids", "type": "string[]"},
            {"name": "in_stock", "type": "bool"},
            {"name": "in_stock_rank", "type": "int32"},
            {"name": "price", "type": "float"},
            {"name": "name_sort", "type": "string", "sort": True},
            {"name": "sales_count", "type": "int64"},
        ],
    }


def to_document(row: Mapping[str, Any]) -> dict[str, Any]:
    """Fila de DOC_COLUMNS -> documento de Typesense. Funcion pura."""
    name = row["name"] or ""
    normalized = normalize_search_text(name)
    stock = row["stock"] or 0
    reserved = row["reserved"] or 0
    in_stock = (stock - reserved) > 0
    brand = row["brand"]
    return {
        "id": row["sicar_uuid"],
        "name": normalized,
        "name_head": name_head(normalized),
        "name_compact": compact_tokens(name),
        "sku_compact": compact_sku(row["sku"]),
        "sku_lower": (row["sku"] or "").strip().lower(),
        "additional_skus": [compact_sku(s) for s in (row["additional_skus"] or []) if isinstance(s, str) and s.strip()],
        "brand": brand,
        "brand_lower": brand.lower() if brand else None,
        "has_brand": brand is not None,
        "description": normalize_search_text(row["description"]) if row["description"] else None,
        "department_uuid": row["department_uuid"],
        "category_uuid": row["category_uuid"],
        "category_uuids": sorted(row["category_uuids"] or []),
        "vehicle_uuids": sorted(row["vehicle_uuids"] or []),
        "in_stock": in_stock,
        "in_stock_rank": 1 if in_stock else 0,
        "price": float(row["price"] or 0),
        "name_sort": strip_accents(name).lower(),
        "sales_count": int(row["sales_count"] or 0),
    }


# Una sola consulta por lote (sin N+1): los ids de categorias/vehiculos se agregan con
# subconsultas correlacionadas, cubiertas por los indices de product_id de cada tabla.
_DOC_SELECT = """
    SELECT p.id, p.sicar_uuid, p.sku, p.additional_skus, p.name, p.brand, p.description,
           p.department_uuid, p.category_uuid, p.price, p.stock, p.reserved, p.sales_count,
           (SELECT array_agg(pc.category_uuid) FROM product_categories pc WHERE pc.product_id = p.id) AS category_uuids,
           (SELECT array_agg(pv.vehicle_uuid) FROM product_vehicles pv WHERE pv.product_id = p.id) AS vehicle_uuids
    FROM products p
"""


async def fetch_documents_by_ids(session: AsyncSession, product_ids: Iterable[int]) -> list[dict[str, Any]]:
    """Documentos para los productos dados que sigan activos y no eliminados."""
    ids = list(product_ids)
    if not ids:
        return []
    result = await session.execute(
        text(_DOC_SELECT + " WHERE p.id = ANY(CAST(:ids AS integer[])) AND p.is_deleted = false AND p.is_active = true"),
        {"ids": ids},
    )
    return [to_document(row) for row in result.mappings()]


async def fetch_document_page(session: AsyncSession, after_id: int, limit: int) -> tuple[list[dict[str, Any]], int | None]:
    """Paginacion por keyset (id > after_id) sobre todo el catalogo activo, para la
    reconstruccion completa. Devuelve (documentos, ultimo id visto o None si ya no hay mas)."""
    result = await session.execute(
        text(_DOC_SELECT + " WHERE p.id > :after_id AND p.is_deleted = false AND p.is_active = true ORDER BY p.id LIMIT :limit"),
        {"after_id": after_id, "limit": limit},
    )
    rows = list(result.mappings())
    if not rows:
        return [], None
    return [to_document(row) for row in rows], rows[-1]["id"]
