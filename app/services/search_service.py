"""Busqueda de productos con Typesense, con Postgres como respaldo. Ver CLAUDE.md, "Busqueda
con Typesense", y la spec (seccion 6 y "Phase 0 results").

Typesense solo decide QUE productos y en QUE orden (ids + total); precio, stock y el resto
de lo que ve el cliente siempre sale de Postgres (hydrate), mismo principio de "nunca
confiar en una copia del precio/stock" que el checkout. Si Typesense no esta configurado,
esta caido (cortacircuitos abierto) o falla, se usa catalog_service.search_products tal
cual - la busqueda nunca se cae por Typesense."""
import logging
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_tracking import capture_exception
from app.models.product import Product
from app.schemas.search import SearchFilter
from app.services import catalog_service, search_index, typesense_client
from app.services.taxonomy_service import get_descendant_uuids

logger = logging.getLogger(__name__)

# Pesos y typos por campo, en el mismo orden que QUERY_BY (ajustados en la Fase 0 y en la
# Fase 3, comparando variantes sobre el catalogo completo): campos de SKU sin typos (un typo
# en un codigo es otro producto; ademas evita que "wd40" matchee UD408); nombre con typos;
# descripcion al final. name_first (tipo de producto) va con peso BAJO a proposito: basta
# para que un typo como "matillo" ponga los martillos antes que "Engrapadora tipo martillo",
# y con peso alto aplastaba la cercania entre palabras ("llave allen" traia llaves de
# artilleria con "boca hexagonal" antes que las llaves hexagonales).
QUERY_BY = "sku_compact,name_compact,additional_skus,name_first,name_head,name,brand,description"
QUERY_BY_WEIGHTS = "7,7,7,3,5,4,3,1"
NUM_TYPOS = "0,0,0,2,2,2,1,1"

# "relevance" se arma por consulta en _relevance_sort (depende de las palabras buscadas).
SORTS = {
    "price_asc": "price:asc,_text_match:desc",
    "price_desc": "price:desc,_text_match:desc",
    "name_asc": "name_sort:asc",
}

SUGGEST_BRANDS = 4


def _quote(value: str) -> str:
    """Valor de filter_by entre backticks. Typesense no permite escapar un backtick dentro,
    asi que se quita (uuids y marcas reales nunca lo traen)."""
    return "`" + value.replace("`", "") + "`"


def build_filter_by(*, department_uuid: Optional[str] = None, category_uuid: Optional[str] = None,
                    category_uuids: Optional[list[str]] = None, vehicle_uuid: Optional[str] = None,
                    brand: Optional[str] = None, has_brand: Optional[bool] = None,
                    in_stock: Optional[bool] = False) -> str:
    """Mismos filtros y misma semantica que catalog_service._apply_search_filters.
    `category_uuids` ya viene expandido con descendientes (taxonomyUuid)."""
    clauses = []
    if department_uuid:
        clauses.append(f"department_uuid:={_quote(department_uuid)}")
    if category_uuid:
        clauses.append(f"category_uuid:={_quote(category_uuid)}")
    if category_uuids:
        clauses.append("category_uuids:=[" + ",".join(_quote(c) for c in category_uuids) + "]")
    if vehicle_uuid:
        clauses.append(f"vehicle_uuids:={_quote(vehicle_uuid)}")
    if brand:
        clauses.append(f"brand_lower:={_quote(brand.lower())}")
    if has_brand is True:
        clauses.append("has_brand:=true")
    elif has_brand is False:
        clauses.append("has_brand:=false")
    if in_stock:
        clauses.append("in_stock:=true")
    return " && ".join(clauses)


def _relevance_sort(words: list[str]) -> str:
    """A igual coincidencia de texto (empates frecuentes: casi todo el catalogo tiene
    sales_count 0), desempata por "tipo de producto" y disponibilidad en un solo campo
    _eval ponderado: nombre que EMPIEZA con una palabra de la consulta (peso 3) + en
    existencia (peso 1); luego popularidad. Asi "martillo" pone los martillos antes que
    "Engrapadora tipo martillo", y "llave 10mm" las llaves antes que los candados."""
    content = [w for w in words if w not in search_index.STOPWORDS]
    if not content:
        return "_text_match:desc,_eval(in_stock:=true):desc,sales_count:desc"
    first_words = ",".join(_quote(w) for w in dict.fromkeys(content))
    return (f"_text_match:desc,_eval([(name_first:=[{first_words}]):3, (in_stock:=true):1]):desc,"
            "sales_count:desc")


def build_search_params(normalized_q: str, *, filter_by: str, sort_by: Optional[str],
                        limit: int, offset: int) -> dict[str, Any]:
    words = normalized_q.split()
    last = words[-1] if words else ""
    params: dict[str, Any] = {
        "q": normalized_q,
        "query_by": QUERY_BY,
        "query_by_weights": QUERY_BY_WEIGHTS,
        "num_typos": NUM_TYPOS,
        "text_match_type": "max_weight",
        "prioritize_exact_match": "true",
        # Prefijo solo si la ultima palabra tiene >= 3 caracteres: "20v" o una "v" suelta
        # no deben matchear por prefijo todo lo que empiece igual.
        "prefix": "true" if len(last) >= 3 else "false",
        "split_join_tokens": "fallback",
        "stopwords": search_index.STOPWORDS_ID,
        "sort_by": SORTS.get(sort_by) or _relevance_sort(words),
        "limit": limit,
        "offset": offset,
        "include_fields": "id",
    }
    if filter_by:
        params["filter_by"] = filter_by
    return params


def _exact_sku_search(raw_q: str, filter_by: str) -> dict[str, Any]:
    """Pin de SKU exacto: cubre cualquier formato de SKU (con espacios, fracciones, guiones)
    que la busqueda de texto no garantiza en primer lugar - ver "Phase 0 results"."""
    sku_filter = f"sku_lower:={_quote(raw_q.strip().lower())}"
    return {
        "q": "*",
        "filter_by": f"{sku_filter} && {filter_by}" if filter_by else sku_filter,
        "per_page": 1,
        "include_fields": "id",
    }


async def hydrate(db: AsyncSession, ids: list[str]) -> list[Product]:
    """Productos de Postgres en el orden dado por Typesense. Un id que ya no este activo (se
    desactivo en los ~30s antes de reindexarse) simplemente se omite."""
    if not ids:
        return []
    result = await db.execute(
        select(Product).where(Product.sicar_uuid.in_(ids), Product.is_deleted == False, Product.is_active == True)  # noqa: E712
    )
    by_uuid = {p.sicar_uuid: p for p in result.scalars().all()}
    return [by_uuid[i] for i in ids if i in by_uuid]


async def _postgres_search(db: AsyncSession, f: SearchFilter) -> dict[str, Any]:
    return await catalog_service.search_products(
        db, f.q, f.limit, f.offset,
        department_uuid=f.department_uuid, category_uuid=f.category_uuid,
        taxonomy_uuid=f.taxonomy_uuid, vehicle_uuid=f.vehicle_uuid,
        brand=f.brand, has_brand=f.has_brand, in_stock=f.in_stock, sort_by=f.sort_by,
    )


def _report(e: Exception, operation: str, q: str) -> None:
    logger.warning(f"Typesense fallo en {operation} ('{q}'), usando Postgres: {e}")
    capture_exception(e, search_operation=operation, q=q)


async def search(db: AsyncSession, f: SearchFilter) -> dict[str, Any]:
    """POST /search. Mismo contrato de respuesta que catalog_service.search_products."""
    if not typesense_client.is_available():
        return await _postgres_search(db, f)

    category_uuids = None
    if f.taxonomy_uuid:
        category_uuids = await get_descendant_uuids(db, f.taxonomy_uuid)
        if not category_uuids:
            # Nodo inexistente: Postgres devuelve vacio (IN de una lista vacia) - igual aqui.
            return {"total": 0, "docs": []}

    filter_by = build_filter_by(
        department_uuid=f.department_uuid, category_uuid=f.category_uuid,
        category_uuids=category_uuids, vehicle_uuid=f.vehicle_uuid,
        brand=f.brand, has_brand=f.has_brand, in_stock=f.in_stock,
    )
    normalized_q = search_index.normalize_search_text(f.q).strip()
    if not normalized_q:
        return {"total": 0, "docs": []}

    main = {"collection": search_index.ALIAS,
            **build_search_params(normalized_q, filter_by=filter_by, sort_by=f.sort_by, limit=f.limit, offset=f.offset)}
    searches = [main]
    pin = f.offset == 0
    if pin:
        searches.append({"collection": search_index.ALIAS, **_exact_sku_search(f.q, filter_by)})

    try:
        results = await typesense_client.multi_search(searches)
    except typesense_client.TypesenseError as e:
        _report(e, "search", f.q)
        return await _postgres_search(db, f)

    ids = [hit["document"]["id"] for hit in results[0].get("hits", [])]
    total = results[0].get("found", 0)
    if pin:
        pinned = [hit["document"]["id"] for hit in results[1].get("hits", [])]
        if pinned:
            if pinned[0] in ids:
                ids.remove(pinned[0])
            else:
                total += 1
            # El pin ocupa un lugar de la pagina 1; limitacion aceptada: si el producto
            # fijado no era resultado de texto, el ultimo hit de esta pagina no aparece en
            # la siguiente (solo pasa en busquedas por SKU, que rara vez paginan).
            ids = (pinned[:1] + ids)[:f.limit]

    return {"total": total, "docs": await hydrate(db, ids)}


async def suggest(db: AsyncSession, q: str, limit: int) -> dict[str, Any]:
    """GET /search/suggest: productos + marcas que empiezan con lo escrito, en una sola ida
    y vuelta. Sin Typesense: productos de la busqueda de Postgres y sin marcas."""
    normalized_q = search_index.normalize_search_text(q).strip()
    if typesense_client.is_available() and normalized_q:
        searches = [
            {
                "collection": search_index.ALIAS,
                **build_search_params(normalized_q, filter_by="", sort_by="relevance", limit=limit, offset=0),
                # Autocompletado: prefijo siempre, aunque la ultima palabra sea corta ("tr" -> Truper).
                "prefix": "true",
            },
            {
                "collection": search_index.ALIAS,
                "q": "*",
                "per_page": 0,
                "facet_by": "brand",
                "facet_query": f"brand:{q.strip()}",
                "max_facet_values": SUGGEST_BRANDS,
            },
        ]
        try:
            products_result, brands_result = await typesense_client.multi_search(searches)
            ids = [hit["document"]["id"] for hit in products_result.get("hits", [])]
            facet = next(iter(brands_result.get("facet_counts", [])), {})
            brands = [{"name": c["value"], "count": c["count"]} for c in facet.get("counts", [])]
            return {"products": await hydrate(db, ids), "brands": brands}
        except typesense_client.TypesenseError as e:
            _report(e, "suggest", q)

    result = await catalog_service.search_products(db, q, limit, 0)
    return {"products": result["docs"], "brands": []}
