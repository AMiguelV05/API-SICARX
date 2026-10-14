import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_, case, text
from app.models.product import Product
from app.models.taxonomy import product_categories
from app.models.vehicle import product_vehicles
from app.services.taxonomy_service import get_descendant_uuids

logger = logging.getLogger(__name__)

def _escape_ilike(text: str) -> str:
    """Escapa %/_ (y la propia barra invertida) para que no se interpreten como comodines ILIKE."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

async def _apply_search_filters(db: AsyncSession, stmt, department_uuid, category_uuid, taxonomy_uuid, vehicle_uuid, in_stock, brand=None, has_brand=None, on_sale=None):
    """Filtros compartidos entre la busqueda principal (word-AND) y el fallback tolerante a
    errores de tipeo de mas abajo - misma logica que get_local_catalog, factorizada aqui para
    no duplicarla entre las dos consultas de search_products."""
    if department_uuid:
        stmt = stmt.where(Product.department_uuid == department_uuid)

    if category_uuid:
        stmt = stmt.where(Product.category_uuid == category_uuid)

    if taxonomy_uuid:
        descendant_uuids = await get_descendant_uuids(db, taxonomy_uuid)
        stmt = stmt.where(Product.id.in_(
            select(product_categories.c.product_id).where(product_categories.c.category_uuid.in_(descendant_uuids))
        ))

    if vehicle_uuid:
        stmt = stmt.where(Product.id.in_(
            select(product_vehicles.c.product_id).where(product_vehicles.c.vehicle_uuid == vehicle_uuid)
        ))

    if in_stock:
        stmt = stmt.where(Product.available_stock > 0)

    if brand:
        stmt = stmt.where(func.lower(Product.brand) == brand.lower())

    stmt = _apply_has_brand(stmt, has_brand)

    if on_sale:
        stmt = stmt.where(Product.is_on_sale)

    return stmt

# Orden por defecto ("relevance"): primero lo que se puede comprar y se ve bien - con
# existencia antes que agotado, con foto antes que sin foto - y despues el criterio propio de
# cada endpoint. Product.id al final de todo orden: sales_count/name/price se repiten mucho
# (casi todo el catalogo tiene sales_count 0), y sin un desempate unico Postgres puede
# devolver los empates en distinto orden entre dos consultas, repitiendo o saltando
# productos entre paginas consecutivas.
_IN_STOCK_FIRST = (Product.available_stock > 0).desc()
_WITH_IMAGE_FIRST = and_(Product.image_url.isnot(None), Product.image_url != "").desc()


def _apply_price_range(stmt, min_price, max_price):
    """minPrice/maxPrice inclusivos, sobre el precio que se cobra (price, con IVA) - nunca
    compareAtPrice."""
    if min_price is not None:
        stmt = stmt.where(Product.price >= min_price)
    if max_price is not None:
        stmt = stmt.where(Product.price <= max_price)
    return stmt


# priceRange: precios > 0 nada mas - un producto que la sincronizacion dejo en 0.00 no se
# puede comprar (validate_cart_items lo rechaza) y no debe estirar el limite inferior.
_MIN_PRICE = func.min(Product.price).filter(Product.price > 0)
_MAX_PRICE = func.max(Product.price).filter(Product.price > 0)


def _price_range_dict(min_value, max_value):
    if min_value is None or max_value is None:
        return None
    return {"min": float(min_value), "max": float(max_value)}


async def _fetch_page(db: AsyncSession, stmt, unpriced_stmt, limit: int, offset: int, price_filtered: bool):
    """Pagina + total + priceRange.

    count(*) OVER() en la MISMA consulta que trae la pagina, en vez de un SELECT COUNT(*)
    aparte contra una subconsulta identica - una sola ida a Postgres en el camino comun. El
    total sigue siendo sobre el conjunto filtrado COMPLETO (antes de LIMIT/OFFSET): la ventana
    no lleva PARTITION BY/ORDER BY.

    priceRange es el rango del conjunto filtrado SIN el propio filtro de precio
    (`unpriced_stmt`), para que el control del frontend muestre los limites reales. Sin
    filtro de precio sale de la misma ventana (gratis: la ventana ya recorre todas las
    filas); con filtro de precio hace falta una consulta aparte sobre `unpriced_stmt`."""
    columns = [func.count().over().label("total_count")]
    if not price_filtered:
        columns += [_MIN_PRICE.over().label("min_price"), _MAX_PRICE.over().label("max_price")]
    result = await db.execute(stmt.add_columns(*columns).limit(limit).offset(offset))
    rows = result.all()
    products = [row[0] for row in rows]

    if rows:
        total_items = rows[0].total_count
    elif offset == 0:
        # Sin filas y sin offset: el filtro genuinamente no matchea nada.
        total_items = 0
    else:
        # offset mas alla del conjunto filtrado: la ventana no viene en ninguna fila, asi que
        # el total sale de un COUNT(*) aparte - el precio lo paga solo este caso.
        total_items = await db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))

    if rows and not price_filtered:
        price_range = _price_range_dict(rows[0].min_price, rows[0].max_price)
    elif not price_filtered and total_items == 0:
        price_range = None
    else:
        sub = unpriced_stmt.order_by(None).subquery()
        bounds = (await db.execute(select(
            func.min(sub.c.price).filter(sub.c.price > 0), func.max(sub.c.price).filter(sub.c.price > 0),
        ))).one()
        price_range = _price_range_dict(*bounds)

    return {"total": total_items, "docs": products, "price_range": price_range}


def _apply_has_brand(stmt, has_brand):
    """hasBrand: true = solo con marca, false = solo sin marca ("Sin marca"), None = sin
    filtro. brand nunca se guarda como "" (brand_service.normalize_brand + migracion
    a7c3e1f9d2b4), asi que IS NULL basta."""
    if has_brand is True:
        stmt = stmt.where(Product.brand.isnot(None))
    elif has_brand is False:
        stmt = stmt.where(Product.brand.is_(None))
    return stmt

async def get_local_catalog(db: AsyncSession, filters: dict):
    stmt = select(Product).where(
        Product.is_deleted == False,
        Product.is_active == True
    )

    if filters.get("department_uuid"):
        logger.debug(f"Aplicando filtro por departamento: {filters['department_uuid']}")
        stmt = stmt.where(Product.department_uuid == filters["department_uuid"])

    if filters.get("category_uuid"):
        logger.debug(f"Aplicando filtro por categoria: {filters['category_uuid']}")
        stmt = stmt.where(Product.category_uuid == filters["category_uuid"])

    if filters.get("taxonomy_uuid"):
        # taxonomy_uuid es el arbol PIM propio (N:M via product_categories), distinto de category_uuid (clasificacion cruda de Sicar X); incluye descendientes.
        descendant_uuids = await get_descendant_uuids(db, filters["taxonomy_uuid"])
        stmt = stmt.where(Product.id.in_(
            select(product_categories.c.product_id).where(product_categories.c.category_uuid.in_(descendant_uuids))
        ))

    if filters.get("vehicle_uuid"):
        stmt = stmt.where(Product.id.in_(
            select(product_vehicles.c.product_id).where(product_vehicles.c.vehicle_uuid == filters["vehicle_uuid"])
        ))

    if filters.get("brand"):
        stmt = stmt.where(func.lower(Product.brand) == filters["brand"].lower())

    stmt = _apply_has_brand(stmt, filters.get("has_brand"))

    if filters.get("in_stock"):
        stmt = stmt.where(Product.available_stock > 0)

    if filters.get("tag"):
        stmt = stmt.where(Product.tags.contains([filters["tag"]]))

    if filters.get("on_sale"):
        stmt = stmt.where(Product.is_on_sale)

    unpriced_stmt = stmt
    min_price, max_price = filters.get("min_price"), filters.get("max_price")
    stmt = _apply_price_range(stmt, min_price, max_price)

    sort_by = filters.get("sort_by")
    if sort_by == "price_asc":
        stmt = stmt.order_by(Product.price.asc(), Product.id)
    elif sort_by == "price_desc":
        stmt = stmt.order_by(Product.price.desc(), Product.id)
    elif sort_by == "name_asc":
        stmt = stmt.order_by(Product.name.asc(), Product.id)
    elif sort_by == "relevance":
        # Sin texto de busqueda aqui, "relevance" = popularidad (sales_count) - el proxy
        # estandar de e-commerce para el orden por defecto de una categoria - despues de
        # existencia y foto. Con inStock: true el primer criterio es constante y no estorba.
        stmt = stmt.order_by(_IN_STOCK_FIRST, _WITH_IMAGE_FIRST, Product.sales_count.desc(), Product.name.asc(), Product.id)
    else:
        # Sin sortBy: antes no habia ORDER BY (orden natural, no estable entre paginas).
        stmt = stmt.order_by(Product.id)

    page = await _fetch_page(
        db, stmt, unpriced_stmt, filters.get("limit", 60), filters.get("offset", 0),
        price_filtered=min_price is not None or max_price is not None,
    )

    logger.info(f"Consulta de catalogo exitosa. Filtros: {filters}. Total encontrados: {page['total']}")
    return page

async def search_products(db: AsyncSession, q: str, limit: int, offset: int, department_uuid: str = None, category_uuid: str = None, taxonomy_uuid: str = None, vehicle_uuid: str = None, in_stock: bool = False, sort_by: str = "relevance", brand: str = None, has_brand: bool = None, on_sale: bool = None, min_price=None, max_price=None):
    """Busqueda por palabras: cada palabra de `q` debe aparecer (ILIKE, insensible a acentos via
    immutable_unaccent, migracion f1a3c7e9b2d4) en sku O name - sin exigir que la frase completa
    sea una sola subcadena contigua ni que las palabras esten en el mismo orden/campo. Esto es lo
    que arregla la busqueda "demasiado estricta": antes, "tuerca acero" no matcheaba "Tuerca de
    Acero Inoxidable 3/8" porque las palabras no eran adyacentes; ahora cada palabra se valida por
    separado (AND entre palabras, OR entre sku/name por palabra) via los mismos indices GIN
    trigram (ix_products_sku_trgm_search/ix_products_name_trgm_search). Ranking: frase completa como subcadena (comportamiento previo)
    > name empieza con la primera palabra > el resto de los matches por palabra, luego popularidad.

    Si el match por palabras no encuentra nada (offset 0), se reintenta con un fallback tolerante a
    errores de tipeo via similarity() de pg_trgm (operador `%`, mismo indice) - cubre el caso de una
    sola palabra mal escrita (p. ej. "desarmalador" en vez de "desarmador") que el AND estricto de
    arriba nunca hubiera encontrado.

    Guiones: tanto sku/name (search_normalize(), migracion b8d2f5a1c7e3) como q se comparan sin
    "-", asi que "wd40" encuentra "WD-40" (guion como nulo); "wd 40" ya lo encontraba por el AND
    entre palabras (guion como espacio). La frase sin espacios ("wd40") tambien cuenta como match
    de frase completa para el ranking, para que "wd 40" ponga WD-40 hasta arriba."""
    norm_q = " ".join(q.replace("-", "").split())
    if not norm_q:
        # q era solo guiones/espacios - sin nada que buscar tras normalizar.
        return {"total": 0, "docs": [], "price_range": None}
    escaped_q = _escape_ilike(norm_q)
    words = norm_q.split()
    escaped_words = [_escape_ilike(w) for w in words]

    unaccented_sku = func.search_normalize(Product.sku)
    unaccented_name = func.search_normalize(Product.name)

    def word_match(escaped_word):
        pattern = f"%{escaped_word}%"
        return or_(
            unaccented_sku.ilike(func.search_normalize(pattern), escape="\\"),
            unaccented_name.ilike(func.search_normalize(pattern), escape="\\"),
        )

    full_phrase_match = word_match(escaped_q)
    if len(words) > 1:
        full_phrase_match = or_(full_phrase_match, word_match(_escape_ilike("".join(words))))
    starts_with_first_word = unaccented_name.ilike(func.search_normalize(f"{escaped_words[0]}%"), escape="\\")

    stmt = select(Product).where(
        Product.is_deleted == False,
        Product.is_active == True,
        and_(*[word_match(w) for w in escaped_words]),
    )
    stmt = await _apply_search_filters(db, stmt, department_uuid, category_uuid, taxonomy_uuid, vehicle_uuid, in_stock, brand, has_brand, on_sale)
    price_filtered = min_price is not None or max_price is not None

    def apply_sort(stmt, relevance_priority):
        if sort_by == "price_asc":
            return stmt.order_by(Product.price.asc(), Product.id)
        elif sort_by == "price_desc":
            return stmt.order_by(Product.price.desc(), Product.id)
        elif sort_by == "name_asc":
            return stmt.order_by(Product.name.asc(), Product.id)
        else:
            # La coincidencia de texto sigue mandando; existencia y foto desempatan antes que
            # la popularidad (mismo criterio que get_local_catalog y que Typesense).
            return stmt.order_by(relevance_priority, _IN_STOCK_FIRST, _WITH_IMAGE_FIRST, Product.sales_count.desc(), Product.name.asc(), Product.id)

    priority = case((full_phrase_match, 0), (starts_with_first_word, 1), else_=2)
    page = await _fetch_page(
        db, apply_sort(_apply_price_range(stmt, min_price, max_price), priority), stmt,
        limit, offset, price_filtered,
    )
    total_items = page["total"]

    # Fallback solo si las palabras no matchean nada; si matchean pero ninguna cae en el rango
    # de precio pedido (price_range no nulo), el resultado correcto es 0, no otras palabras.
    if total_items == 0 and page["price_range"] is None:
        # Fallback tolerante a errores de tipeo. Ojo: similarity()/`%` (usado aqui antes) compara
        # la CADENA COMPLETA de q contra la CADENA COMPLETA de name - para una palabra corta con
        # typo contra un name largo ("Desarmador Plano Punta Cruz 1/4 x 6 Truper"), los trigramas
        # del resto del nombre diluyen la similitud total muy por debajo del umbral, asi que casi
        # nada pasaba (confirmado: "desalmador" solo devolvia 2 resultados). word_similarity()/
        # `<%`/`%>` es la funcion de pg_trgm hecha para esto - busca la MEJOR subcadena continua
        # dentro del texto largo que se parezca a la palabra corta, en vez de diluir contra todo
        # el campo. Mismo indice GIN (search_normalize(name)/sku, gin_trgm_ops) acelera `%>`
        # ademas de `%`, asi que sigue sin requerir migracion. Se aplica por palabra (AND entre
        # palabras, igual que el match estricto de arriba) para que una query de varias palabras
        # con una sola mal escrita solo tenga que tolerar esa, no la frase completa.
        # `pg_trgm.word_similarity_threshold` (default 0.6) se relaja a 0.4 solo para esta
        # transaccion (SET LOCAL) - sigue siendo index-friendly (el GUC controla el umbral que
        # usan `<%`/`%>` internamente) pero menos estricto que el default para tolerar mejor un
        # typo de una letra en palabras cortas.
        await db.execute(text("SET LOCAL pg_trgm.word_similarity_threshold = 0.5"))

        word_binds = [func.search_normalize(w) for w in words]

        def word_fuzzy_match(word_bind):
            return or_(
                unaccented_name.op("%>")(word_bind),
                unaccented_sku.op("%>")(word_bind),
            )

        fuzzy_score = func.greatest(
            func.word_similarity(word_binds[0], unaccented_name),
            func.word_similarity(word_binds[0], unaccented_sku),
        )
        for word_bind in word_binds[1:]:
            fuzzy_score = fuzzy_score + func.greatest(
                func.word_similarity(word_bind, unaccented_name),
                func.word_similarity(word_bind, unaccented_sku),
            )

        fallback_stmt = select(Product).where(
            Product.is_deleted == False,
            Product.is_active == True,
            and_(*[word_fuzzy_match(wb) for wb in word_binds]),
        )
        fallback_stmt = await _apply_search_filters(db, fallback_stmt, department_uuid, category_uuid, taxonomy_uuid, vehicle_uuid, in_stock, brand, has_brand, on_sale)
        page = await _fetch_page(
            db, apply_sort(_apply_price_range(fallback_stmt, min_price, max_price), fuzzy_score.desc()), fallback_stmt,
            limit, offset, price_filtered,
        )
        total_items = page["total"]

        if page["docs"]:
            logger.info(f"Busqueda '{q}' sin match exacto por palabras - usando fallback de word_similarity (typo-tolerant). Total encontrados: {total_items}")

    logger.info(f"Busqueda '{q}' exitosa. Total encontrados: {total_items}")
    return page

async def get_available_now_products(db: AsyncSession, limit: int, department_uuid: str = None, category_uuid: str = None, taxonomy_uuid: str = None, vehicle_uuid: str = None, sort_by: str = None):
    """Productos con stock disponible e imagen, para la seccion "Disponible Ahora" de la
    pagina principal. Sin paginacion, igual que get_best_selling_products - un feed acotado
    de top-N, no un browse."""
    stmt = select(Product).where(
        Product.is_deleted == False,
        Product.is_active == True,
        Product.available_stock > 0,
        Product.image_url.isnot(None),
        Product.image_url != "",
    )

    if department_uuid:
        stmt = stmt.where(Product.department_uuid == department_uuid)

    if category_uuid:
        stmt = stmt.where(Product.category_uuid == category_uuid)

    if taxonomy_uuid:
        descendant_uuids = await get_descendant_uuids(db, taxonomy_uuid)
        stmt = stmt.where(Product.id.in_(
            select(product_categories.c.product_id).where(product_categories.c.category_uuid.in_(descendant_uuids))
        ))

    if vehicle_uuid:
        stmt = stmt.where(Product.id.in_(
            select(product_vehicles.c.product_id).where(product_vehicles.c.vehicle_uuid == vehicle_uuid)
        ))

    if sort_by == "price_asc":
        stmt = stmt.order_by(Product.price.asc())
    elif sort_by == "price_desc":
        stmt = stmt.order_by(Product.price.desc())
    elif sort_by == "name_asc":
        stmt = stmt.order_by(Product.name.asc())
    elif sort_by == "relevance":
        stmt = stmt.order_by(Product.sales_count.desc(), Product.name.asc())

    stmt = stmt.limit(limit)

    result = await db.execute(stmt)
    return result.scalars().all()

async def get_best_selling_products(db: AsyncSession, limit: int, department_uuid: str = None, category_uuid: str = None, taxonomy_uuid: str = None, vehicle_uuid: str = None, in_stock: bool = False):
    """Productos mas vendidos (Product.sales_count > 0), para la seccion de la pagina principal - ver order_history_service.py para como se mantiene sales_count al dia."""
    stmt = select(Product).where(
        Product.is_deleted == False,
        Product.is_active == True,
        Product.sales_count > 0,
    )

    if department_uuid:
        stmt = stmt.where(Product.department_uuid == department_uuid)

    if category_uuid:
        stmt = stmt.where(Product.category_uuid == category_uuid)

    if taxonomy_uuid:
        descendant_uuids = await get_descendant_uuids(db, taxonomy_uuid)
        stmt = stmt.where(Product.id.in_(
            select(product_categories.c.product_id).where(product_categories.c.category_uuid.in_(descendant_uuids))
        ))

    if vehicle_uuid:
        stmt = stmt.where(Product.id.in_(
            select(product_vehicles.c.product_id).where(product_vehicles.c.vehicle_uuid == vehicle_uuid)
        ))

    if in_stock:
        stmt = stmt.where(Product.available_stock > 0)

    stmt = stmt.order_by(Product.sales_count.desc()).limit(limit)

    result = await db.execute(stmt)
    return result.scalars().all()

async def get_distinct_brands(db: AsyncSession, taxonomy_uuid: str = None) -> list[str]:
    """Un representante por grupo case-insensitive (GROUP BY lower(brand), MIN(brand) como
    valor mostrado - determinista) - evita que el picklist muestre duplicados por casing
    inconsistente (brand nunca se normaliza en escritura, ver attribute_service.update_product_info/
    bulk_import_service). Cualquiera de estos valores filtra el grupo completo via el filtro
    `brand` de get_local_catalog/_apply_search_filters, que ya comparan por lower(brand).

    `taxonomy_uuid` (opcional) acota las marcas a las de un nodo del arbol PIM (y sus
    descendientes, igual que el filtro homonimo de get_local_catalog) - para que la franja de
    marcas de la UI muestre solo las relevantes a la categoria seleccionada, en vez de siempre
    el catalogo completo."""
    representative = func.min(Product.brand)
    stmt = (
        select(representative)
        .where(
            Product.is_deleted == False,
            Product.is_active == True,
            Product.brand.isnot(None),
            Product.brand != "",
        )
    )

    if taxonomy_uuid:
        descendant_uuids = await get_descendant_uuids(db, taxonomy_uuid)
        stmt = stmt.where(Product.id.in_(
            select(product_categories.c.product_id).where(product_categories.c.category_uuid.in_(descendant_uuids))
        ))

    stmt = stmt.group_by(func.lower(Product.brand)).order_by(representative)

    result = await db.execute(stmt)
    return [row[0] for row in result.all()]