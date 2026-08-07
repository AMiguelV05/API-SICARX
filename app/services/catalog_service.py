import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, case
from app.models.product import Product
from app.models.taxonomy import product_categories
from app.models.vehicle import product_vehicles
from app.services.taxonomy_service import get_descendant_uuids

logger = logging.getLogger(__name__)

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

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total_items = await db.scalar(count_stmt)

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

    stmt = stmt.limit(filters.get("limit", 60)).offset(filters.get("offset", 0))

    result = await db.execute(stmt)
    products = result.scalars().all()

    logger.info(f"Consulta de catalogo exitosa. Filtros: {filters}. Total encontrados: {total_items}")

    return {
        "total": total_items,
        "docs": products
    }

async def search_products(db: AsyncSession, q: str, limit: int, offset: int, department_uuid: str = None, category_uuid: str = None, taxonomy_uuid: str = None, vehicle_uuid: str = None, in_stock: bool = False, sort_by: str = "relevance"):
    """Substring search (ILIKE) en sku/name via GIN pg_trgm; prefix matches ordenan primero. `q` se escapa para que %/_ no se interpreten como comodines ILIKE."""
    escaped_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped_q}%"
    prefix_pattern = f"{escaped_q}%"
    starts_with = or_(Product.sku.ilike(prefix_pattern, escape="\\"), Product.name.ilike(prefix_pattern, escape="\\"))

    stmt = select(Product).where(
        Product.is_deleted == False,
        Product.is_active == True,
        or_(Product.sku.ilike(pattern, escape="\\"), Product.name.ilike(pattern, escape="\\"))
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

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total_items = await db.scalar(count_stmt)

    if sort_by == "price_asc":
        stmt = stmt.order_by(Product.price.asc())
    elif sort_by == "price_desc":
        stmt = stmt.order_by(Product.price.desc())
    elif sort_by == "name_asc":
        stmt = stmt.order_by(Product.name.asc())
    else:
        # "relevance" (default): coincidencia de texto primero (prefix antes que substring), luego popularidad, luego nombre para desempate estable.
        priority = case((starts_with, 0), else_=1)
        stmt = stmt.order_by(priority, Product.sales_count.desc(), Product.name.asc())

    stmt = stmt.limit(limit).offset(offset)

    result = await db.execute(stmt)
    products = result.scalars().all()

    logger.info(f"Busqueda '{q}' exitosa. Total encontrados: {total_items}")

    return {
        "total": total_items,
        "docs": products
    }

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