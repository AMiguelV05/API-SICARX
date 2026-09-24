import logging
from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.product import Product

logger = logging.getLogger(__name__)

# Administracion de Product.brand a nivel "marca" (no producto por producto) - ver
# /v1/admin/brands y PATCH /v1/admin/products/brand. Todo el matching es case-insensitive via
# lower(brand), igual que el filtro publico `brand` de catalog_service, y opera sobre productos
# no eliminados (activos o no), igual que attribute_service.update_product_info. Ninguna
# funcion aqui hace commit: la ruta registra el audit log y luego hace commit.


def normalize_brand(value: Optional[str]) -> Optional[str]:
    """Recorta espacios y trata "" como null - unica normalizacion aplicada a brand en
    escritura (no se toca el casing: "SURTEK" vs "Surtek" se resuelve via rename_brand)."""
    if value is None:
        return None
    value = value.strip()
    return value or None


async def list_brands(db: AsyncSession) -> tuple[list[dict], int]:
    """Un grupo por lower(brand). `name` = MIN(brand) (determinista, mismo criterio que
    catalog_service.get_distinct_brands); `variants` = todas las grafias crudas del grupo -
    mas de una significa duplicados por casing que conviene fusionar via rename_brand."""
    group_key = func.lower(Product.brand)
    name = func.min(Product.brand)
    stmt = (
        select(name, func.count(), func.array_agg(Product.brand.distinct()))
        .where(Product.is_deleted == False, Product.brand.isnot(None))
        .group_by(group_key)
        .order_by(func.lower(name))
    )
    result = await db.execute(stmt)
    docs = [
        {"name": row[0], "product_count": row[1], "variants": sorted(row[2])}
        for row in result.all()
    ]

    unbranded_count = await db.scalar(
        select(func.count()).select_from(Product).where(Product.is_deleted == False, Product.brand.is_(None))
    )
    return docs, unbranded_count or 0


async def set_brand_for_products(db: AsyncSession, product_uuids: list[str], brand: Optional[str]) -> tuple[int, list[str]]:
    """Asigna (o borra, con brand=None) la marca de varios productos a la vez. Exito parcial:
    uuids que no resuelven a un producto no eliminado se reportan en not_found (orden de
    entrada, sin duplicados) en vez de rechazar toda la solicitud."""
    unique_uuids = list(dict.fromkeys(product_uuids))
    brand = normalize_brand(brand)

    found = set((await db.execute(
        select(Product.sicar_uuid).where(Product.sicar_uuid.in_(unique_uuids), Product.is_deleted == False)
    )).scalars().all())
    not_found = [u for u in unique_uuids if u not in found]

    updated = 0
    if found:
        result = await db.execute(
            update(Product)
            .where(Product.sicar_uuid.in_(found), Product.is_deleted == False)
            .values(brand=brand)
            .execution_options(synchronize_session=False)
        )
        updated = result.rowcount

    logger.info(f"Marca '{brand}' asignada a {updated} productos via /admin ({len(not_found)} uuids no encontrados).")
    return updated, not_found


async def rename_brand(db: AsyncSession, from_brand: str, to_brand: str) -> int:
    """Todo producto cuya marca coincida con from_brand (case-insensitive) pasa a to_brand.
    Si to_brand ya existe como marca, esto fusiona ambas. Tambien sirve para corregir solo el
    casing ("SURTEK" -> "Surtek"). `404` si ningun producto coincide con from_brand."""
    result = await db.execute(
        update(Product)
        .where(func.lower(Product.brand) == from_brand.lower(), Product.is_deleted == False)
        .values(brand=to_brand)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No hay productos con la marca '{from_brand}'.")
    logger.info(f"Marca '{from_brand}' renombrada a '{to_brand}' en {result.rowcount} productos via /admin.")
    return result.rowcount


async def clear_brand(db: AsyncSession, name: str) -> int:
    """Pone brand=null en todo producto con esa marca (case-insensitive). Idempotente: 0 si
    ninguno coincide, no es un error."""
    result = await db.execute(
        update(Product)
        .where(func.lower(Product.brand) == name.lower(), Product.is_deleted == False)
        .values(brand=None)
        .execution_options(synchronize_session=False)
    )
    logger.info(f"Marca '{name}' eliminada de {result.rowcount} productos via /admin.")
    return result.rowcount
