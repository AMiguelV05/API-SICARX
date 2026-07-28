import logging
from decimal import Decimal
from sqlalchemy import update, bindparam
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.product import Product

logger = logging.getLogger(__name__)

async def apply_stock_deltas(db: AsyncSession, deltas: list[tuple[str, Decimal]]) -> None:
    """Aplica varios ajustes de stock (positivos o negativos, uno por sicar_uuid) en una
    sola sentencia ejecutada en lote (executemany), en vez de un UPDATE awaited por linea
    como se hacia antes en create_order_in_sicar/process_order_cancellation. No hace
    commit — el llamador es responsable de eso, igual que con cualquier otro db.execute."""
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
