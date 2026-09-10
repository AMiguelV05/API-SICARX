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

async def _apply_search_filters(db: AsyncSession, stmt, department_uuid, category_uuid, taxonomy_uuid, vehicle_uuid, in_stock):
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

    if filters.get("in_stock"):
        stmt = stmt.where(Product.available_stock > 0)

    if filters.get("tag"):
        stmt = stmt.where(Product.tags.contains([filters["tag"]]))

    sort_by = filters.get("sort_by")
    if sort_by == "price_asc":
        stmt = stmt.order_by(Product.price.asc())
    elif sort_by == "price_desc":
        stmt = stmt.order_by(Product.price.desc())
    elif sort_by == "name_asc":
        stmt = stmt.order_by(Product.name.asc())
    elif sort_by == "relevance":
        # Sin texto de busqueda aqui, "relevance" = popularidad (sales_count) - el proxy estandar de e-commerce para el orden por defecto de una categoria.
        stmt = stmt.order_by(Product.sales_count.desc(), Product.name.asc())

    offset = filters.get("offset", 0)

    # count(*) OVER() en la MISMA consulta que trae la pagina, en vez de un SELECT COUNT(*)
    # aparte contra una subconsulta identica - una sola ida a Postgres por llamada en el
    # endpoint mas usado de la API, en vez de dos. El total sigue siendo sobre el conjunto
    # filtrado COMPLETO (antes de LIMIT/OFFSET) - la ventana no lleva PARTITION BY/ORDER BY,
    # asi que cuenta todas las filas que matchean el WHERE sin importar el orden/pagina.
    paged_stmt = stmt.add_columns(func.count().over().label("total_count")).limit(filters.get("limit", 60)).offset(offset)

    result = await db.execute(paged_stmt)
    rows = result.all()
    products = [row[0] for row in rows]

    if rows:
        total_items = rows[0].total_count
    elif offset == 0:
        # Sin filas y sin offset: el filtro genuinamente no matchea nada - total es 0, sin
        # necesidad de una segunda consulta.
        total_items = 0
    else:
        # offset mas alla del conjunto filtrado (se pidio una pagina que ya no existe) - la
        # ventana count()OVER() no aparece en ninguna fila si esta pagina viene vacia, asi
        # que la unica forma de que `total` siga siendo correcto (cuantas paginas hay EN
        # TOTAL, no solo si esta pagina esta vacia) es un COUNT(*) aparte aqui - el precio de
        # colapsar a una sola consulta lo paga solo este caso, no el camino comun.
        total_items = await db.scalar(select(func.count()).select_from(stmt.subquery()))

    logger.info(f"Consulta de catalogo exitosa. Filtros: {filters}. Total encontrados: {total_items}")

    return {
        "total": total_items,
        "docs": products
    }

async def search_products(db: AsyncSession, q: str, limit: int, offset: int, department_uuid: str = None, category_uuid: str = None, taxonomy_uuid: str = None, vehicle_uuid: str = None, in_stock: bool = False, sort_by: str = "relevance"):
    """Busqueda por palabras: cada palabra de `q` debe aparecer (ILIKE, insensible a acentos via
    immutable_unaccent, migracion f1a3c7e9b2d4) en sku O name - sin exigir que la frase completa
    sea una sola subcadena contigua ni que las palabras esten en el mismo orden/campo. Esto es lo
    que arregla la busqueda "demasiado estricta": antes, "tuerca acero" no matcheaba "Tuerca de
    Acero Inoxidable 3/8" porque las palabras no eran adyacentes; ahora cada palabra se valida por
    separado (AND entre palabras, OR entre sku/name por palabra) via los mismos indices GIN
    trigram+unaccent (ix_products_sku_trgm_unaccent/ix_products_name_trgm_unaccent) que ya existian
    - no se necesito migracion nueva. Ranking: frase completa como subcadena (comportamiento previo)
    > name empieza con la primera palabra > el resto de los matches por palabra, luego popularidad.

    Si el match por palabras no encuentra nada (offset 0), se reintenta con un fallback tolerante a
    errores de tipeo via similarity() de pg_trgm (operador `%`, mismo indice) - cubre el caso de una
    sola palabra mal escrita (p. ej. "desarmalador" en vez de "desarmador") que el AND estricto de
    arriba nunca hubiera encontrado."""
    escaped_q = _escape_ilike(q)
    words = [w for w in q.split() if w]
    escaped_words = [_escape_ilike(w) for w in words] or [escaped_q]

    unaccented_sku = func.immutable_unaccent(Product.sku)
    unaccented_name = func.immutable_unaccent(Product.name)

    def word_match(escaped_word):
        pattern = f"%{escaped_word}%"
        return or_(
            unaccented_sku.ilike(func.immutable_unaccent(pattern), escape="\\"),
            unaccented_name.ilike(func.immutable_unaccent(pattern), escape="\\"),
        )

    full_phrase_match = word_match(escaped_q)
    starts_with_first_word = unaccented_name.ilike(func.immutable_unaccent(f"{escaped_words[0]}%"), escape="\\")

    stmt = select(Product).where(
        Product.is_deleted == False,
        Product.is_active == True,
        and_(*[word_match(w) for w in escaped_words]),
    )
    stmt = await _apply_search_filters(db, stmt, department_uuid, category_uuid, taxonomy_uuid, vehicle_uuid, in_stock)

    def apply_sort(stmt, relevance_priority):
        if sort_by == "price_asc":
            return stmt.order_by(Product.price.asc())
        elif sort_by == "price_desc":
            return stmt.order_by(Product.price.desc())
        elif sort_by == "name_asc":
            return stmt.order_by(Product.name.asc())
        else:
            return stmt.order_by(relevance_priority, Product.sales_count.desc(), Product.name.asc())

    priority = case((full_phrase_match, 0), (starts_with_first_word, 1), else_=2)
    stmt = apply_sort(stmt, priority)

    # count(*) OVER() en la misma consulta que la pagina - mismo motivo/tradeoff que
    # get_local_catalog arriba: una sola ida a Postgres en el camino comun, con fallback a un
    # COUNT(*) aparte solo si offset cae mas alla del conjunto filtrado (ninguna fila que
    # traiga la ventana consigo).
    paged_stmt = stmt.add_columns(func.count().over().label("total_count")).limit(limit).offset(offset)

    result = await db.execute(paged_stmt)
    rows = result.all()
    products = [row[0] for row in rows]

    if rows:
        total_items = rows[0].total_count
    elif offset == 0:
        total_items = 0
    else:
        total_items = await db.scalar(select(func.count()).select_from(stmt.subquery()))

    if total_items == 0:
        # Fallback tolerante a errores de tipeo. Ojo: similarity()/`%` (usado aqui antes) compara
        # la CADENA COMPLETA de q contra la CADENA COMPLETA de name - para una palabra corta con
        # typo contra un name largo ("Desarmador Plano Punta Cruz 1/4 x 6 Truper"), los trigramas
        # del resto del nombre diluyen la similitud total muy por debajo del umbral, asi que casi
        # nada pasaba (confirmado: "desalmador" solo devolvia 2 resultados). word_similarity()/
        # `<%`/`%>` es la funcion de pg_trgm hecha para esto - busca la MEJOR subcadena continua
        # dentro del texto largo que se parezca a la palabra corta, en vez de diluir contra todo
        # el campo. Mismo indice GIN (immutable_unaccent(name)/sku, gin_trgm_ops) acelera `%>`
        # ademas de `%`, asi que sigue sin requerir migracion. Se aplica por palabra (AND entre
        # palabras, igual que el match estricto de arriba) para que una query de varias palabras
        # con una sola mal escrita solo tenga que tolerar esa, no la frase completa.
        # `pg_trgm.word_similarity_threshold` (default 0.6) se relaja a 0.4 solo para esta
        # transaccion (SET LOCAL) - sigue siendo index-friendly (el GUC controla el umbral que
        # usan `<%`/`%>` internamente) pero menos estricto que el default para tolerar mejor un
        # typo de una letra en palabras cortas.
        await db.execute(text("SET LOCAL pg_trgm.word_similarity_threshold = 0.5"))

        word_binds = [func.immutable_unaccent(w) for w in words] or [func.immutable_unaccent(q)]

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
        fallback_stmt = await _apply_search_filters(db, fallback_stmt, department_uuid, category_uuid, taxonomy_uuid, vehicle_uuid, in_stock)
        fallback_stmt = apply_sort(fallback_stmt, fuzzy_score.desc())

        paged_fallback_stmt = fallback_stmt.add_columns(func.count().over().label("total_count")).limit(limit).offset(offset)
        result = await db.execute(paged_fallback_stmt)
        rows = result.all()
        products = [row[0] for row in rows]
        total_items = rows[0].total_count if rows else 0

        if products:
            logger.info(f"Busqueda '{q}' sin match exacto por palabras - usando fallback de word_similarity (typo-tolerant). Total encontrados: {total_items}")

    logger.info(f"Busqueda '{q}' exitosa. Total encontrados: {total_items}")

    return {
        "total": total_items,
        "docs": products
    }

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