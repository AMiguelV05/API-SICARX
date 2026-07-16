import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, case
from app.models.product import Product

logger = logging.getLogger(__name__)

async def get_local_catalog(db: AsyncSession, filters: dict):
    # Consulta base: solo productos activos y no eliminados
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

    if filters.get("in_stock"):
        stmt = stmt.where(Product.stock > 0)

    # Contar el total de resultados
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total_items = await db.scalar(count_stmt)

    # Orden de los resultados
    sort_by = filters.get("sort_by")
    if sort_by == "price_asc":
        stmt = stmt.order_by(Product.price.asc())
    elif sort_by == "price_desc":
        stmt = stmt.order_by(Product.price.desc())
    elif sort_by == "name_asc":
        stmt = stmt.order_by(Product.name.asc())

    # Aplicar paginación
    stmt = stmt.limit(filters.get("limit", 60)).offset(filters.get("offset", 0))
    
    # Ejecutar la consulta
    result = await db.execute(stmt)
    products = result.scalars().all()

    logger.info(f"Consulta de catalogo exitosa. Filtros: {filters}. Total encontrados: {total_items}")

    return {
        "total": total_items,
        "docs": products
    }

async def search_products(db: AsyncSession, q: str, limit: int, offset: int, department_uuid: str = None, category_uuid: str = None, in_stock: bool = False):
    """Busqueda por substring (case-insensitive) en sku o name, acelerada por los
    indices GIN de pg_trgm. Los resultados donde sku o name empiezan con `q` se
    ordenan primero, antes que las coincidencias que solo contienen `q` en medio."""
    pattern = f"%{q}%"
    prefix_pattern = f"{q}%"
    starts_with = or_(Product.sku.ilike(prefix_pattern), Product.name.ilike(prefix_pattern))

    stmt = select(Product).where(
        Product.is_deleted == False,
        Product.is_active == True,
        or_(Product.sku.ilike(pattern), Product.name.ilike(pattern))
    )

    if department_uuid:
        stmt = stmt.where(Product.department_uuid == department_uuid)

    if category_uuid:
        stmt = stmt.where(Product.category_uuid == category_uuid)

    if in_stock:
        stmt = stmt.where(Product.stock > 0)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total_items = await db.scalar(count_stmt)

    priority = case((starts_with, 0), else_=1)
    stmt = stmt.order_by(priority, Product.name).limit(limit).offset(offset)

    result = await db.execute(stmt)
    products = result.scalars().all()

    logger.info(f"Busqueda '{q}' exitosa. Total encontrados: {total_items}")

    return {
        "total": total_items,
        "docs": products
    }