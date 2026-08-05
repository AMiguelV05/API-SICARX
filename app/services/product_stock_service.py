import logging
from decimal import Decimal
from sqlalchemy import update, bindparam
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
