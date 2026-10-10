import logging
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.product import Product
from app.models.taxonomy import product_categories
from app.services.taxonomy_service import get_descendant_uuids

logger = logging.getLogger(__name__)

# Precio de oferta (PIM propio): solo se escriben Product.compare_at_price/sale_ends_at - el
# precio cobrado (price) sigue siendo de Sicar X. Ninguna funcion hace commit: la ruta
# registra el audit log y luego hace commit (mismo patron que brand_service).

PRICE_QUANTUM = Decimal("0.01")


def _to_decimal(value) -> Decimal:
    return Decimal(str(value)).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)


async def _get_product(db: AsyncSession, product_uuid: str) -> Product:
    product = await db.scalar(select(Product).where(Product.sicar_uuid == product_uuid, Product.is_deleted == False))  # noqa: E712
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado")
    return product


async def set_sale(db: AsyncSession, product_uuid: str, compare_at_price: float, sale_ends_at: Optional[datetime]) -> Product:
    product = await _get_product(db, product_uuid)
    compare = _to_decimal(compare_at_price)
    if compare <= product.price:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"El precio anterior ({compare}) debe ser mayor que el precio actual ({product.price}).",
        )
    product.compare_at_price = compare
    product.sale_ends_at = sale_ends_at
    await db.flush()
    return product


async def clear_sale(db: AsyncSession, product_uuid: str) -> Product:
    product = await _get_product(db, product_uuid)
    product.compare_at_price = None
    product.sale_ends_at = None
    await db.flush()
    return product


async def _resolve_targets(db: AsyncSession, product_uuids: Optional[list[str]], category_uuid: Optional[str]) -> tuple[list[Product], list[str]]:
    """Productos no eliminados del objetivo de una operacion masiva, mas lo que no resolvio."""
    if category_uuid is not None:
        descendants = await get_descendant_uuids(db, category_uuid)
        if not descendants:
            return [], [category_uuid]
        stmt = select(Product).where(
            Product.is_deleted == False,  # noqa: E712
            Product.id.in_(select(product_categories.c.product_id).where(product_categories.c.category_uuid.in_(descendants))),
        )
        return list((await db.execute(stmt)).scalars().all()), []

    unique = list(dict.fromkeys(product_uuids or []))
    found = (await db.execute(
        select(Product).where(Product.sicar_uuid.in_(unique), Product.is_deleted == False)  # noqa: E712
    )).scalars().all()
    by_uuid = {p.sicar_uuid: p for p in found}
    return [by_uuid[u] for u in unique if u in by_uuid], [u for u in unique if u not in by_uuid]


async def bulk_set_sale(
    db: AsyncSession, *, product_uuids: Optional[list[str]], category_uuid: Optional[str],
    compare_at_price: Optional[float], compare_at_percent: Optional[float], sale_ends_at: Optional[datetime],
) -> tuple[int, list[str], list[str]]:
    """Exito parcial: uuids inexistentes vuelven en not_found; productos cuyo precio anterior
    no quedaria mayor que su price actual (o con price <= 0) vuelven en skipped."""
    products, not_found = await _resolve_targets(db, product_uuids, category_uuid)
    fixed = _to_decimal(compare_at_price) if compare_at_price is not None else None
    factor = (Decimal("1") - Decimal(str(compare_at_percent)) / Decimal("100")) if compare_at_percent is not None else None

    skipped = []
    updated = 0
    for product in products:
        price = product.price
        if price is None or price <= 0:
            skipped.append(product.sicar_uuid)
            continue
        compare = fixed if fixed is not None else _to_decimal(price / factor)
        if compare <= price:
            skipped.append(product.sicar_uuid)
            continue
        product.compare_at_price = compare
        product.sale_ends_at = sale_ends_at
        updated += 1
    await db.flush()
    logger.info(f"Oferta capturada en {updated} productos via /admin ({len(skipped)} omitidos, {len(not_found)} no encontrados).")
    return updated, not_found, skipped


async def bulk_clear_sale(db: AsyncSession, *, product_uuids: Optional[list[str]], category_uuid: Optional[str]) -> tuple[int, list[str]]:
    products, not_found = await _resolve_targets(db, product_uuids, category_uuid)
    ids = [p.id for p in products if p.compare_at_price is not None or p.sale_ends_at is not None]
    if ids:
        await db.execute(
            update(Product).where(Product.id.in_(ids))
            .values(compare_at_price=None, sale_ends_at=None)
            .execution_options(synchronize_session=False)
        )
    logger.info(f"Oferta quitada de {len(ids)} productos via /admin ({len(not_found)} no encontrados).")
    return len(ids), not_found
