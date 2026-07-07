import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
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
        logger.debug(f"Aplicando filtro por categoría: {filters['category_uuid']}")
        stmt = stmt.where(Product.category_uuid == filters["category_uuid"])


    # Contar el total de resultados
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total_items = await db.scalar(count_stmt)

    # Aplicar paginación
    stmt = stmt.limit(filters.get("limit", 60)).offset(filters.get("offset", 0))
    
    # Ejecutar la consulta
    result = await db.execute(stmt)
    products = result.scalars().all()

    logger.info(f"Consulta de catálogo exitosa. Filtros: {filters}. Total encontrados: {total_items}")

    return {
        "total": total_items,
        "docs": products
    }