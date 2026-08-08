import logging
from decimal import Decimal
from sqlalchemy import update, bindparam, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.product import Product

logger = logging.getLogger(__name__)

async def apply_stock_deltas(db: AsyncSession, deltas: list[tuple[str, Decimal]]) -> None:
    """Ajustes de stock en lote (executemany) por sicar_uuid; no hace commit, es responsabilidad del llamador."""
    params = [{"p_uuid": uuid, "p_delta": delta} for uuid, delta in deltas if uuid and delta != 0]
    if not params:
        return

    stmt = (
        update(Product)
        .where(Product.sicar_uuid == bindparam("p_uuid"))
        .values(stock=Product.stock + bindparam("p_delta"))
        .execution_options(dml_strategy="core_only")
    )
    await db.execute(stmt, params)

async def apply_reserved_deltas(db: AsyncSession, deltas: list[tuple[str, Decimal]]) -> None:
    """Ajustes de reserved en lote (executemany) por sicar_uuid; no hace commit, es responsabilidad del llamador.

    Clampeado con GREATEST(..., 0): una orden creada antes de que existiera esta reserva
    (pre-migracion 380517fdd109) nunca incremento `reserved` al hacer checkout, pero su
    cancelacion igual intenta liberarla - sin el clamp, eso lleva `reserved` a negativo y
    viola `ck_products_reserved_non_negative`. Inofensivo para el camino normal de reservar
    (delta positivo nunca activa el clamp)."""
    params = [{"p_uuid": uuid, "p_delta": delta} for uuid, delta in deltas if uuid and delta != 0]
    if not params:
        return

    stmt = (
        update(Product)
        .where(Product.sicar_uuid == bindparam("p_uuid"))
        .values(reserved=func.greatest(Product.reserved + bindparam("p_delta"), 0))
        .execution_options(dml_strategy="core_only")
    )
    await db.execute(stmt, params)

async def apply_sales_count_deltas(db: AsyncSession, deltas: list[tuple[str, Decimal]]) -> None:
    """Ajustes de sales_count en lote (executemany) por sicar_uuid; no hace commit, es responsabilidad del llamador."""
    params = [{"p_uuid": uuid, "p_delta": delta} for uuid, delta in deltas if uuid and delta != 0]
    if not params:
        return

    stmt = (
        update(Product)
        .where(Product.sicar_uuid == bindparam("p_uuid"))
        .values(sales_count=Product.sales_count + bindparam("p_delta"))
        .execution_options(dml_strategy="core_only")
    )
    await db.execute(stmt, params)
