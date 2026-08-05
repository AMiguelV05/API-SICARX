import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Optional
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.order import Order
from app.schemas.dashboard import (
    DashboardDailyPoint,
    DashboardSummaryResponse,
    DashboardTopProduct,
    DashboardCategorySales,
)

logger = logging.getLogger(__name__)

DEFAULT_RANGE_DAYS = 30

_SORT_COLUMNS = {"revenue": "revenue", "quantity": "units_sold"}


def _resolve_date_range(start_date: Optional[date], end_date: Optional[date]) -> tuple[datetime, datetime, date, date]:
    """Default a los ultimos 30 dias (UTC) cuando no se especifica. Devuelve un rango
    medio-abierto [start, end + 1 dia) para comparar contra created_at (timestamptz) sin
    off-by-one en el dia final, y resuelve los defaults en Python antes de bindear - evita
    el problema de asyncpg con parametros NULL (ver vehicle_service.py)."""
    resolved_end = end_date or datetime.now(timezone.utc).date()
    resolved_start = start_date or (resolved_end - timedelta(days=DEFAULT_RANGE_DAYS - 1))

    start_dt = datetime.combine(resolved_start, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(resolved_end + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return start_dt, end_dt, resolved_start, resolved_end


async def get_summary(db: AsyncSession, start_date: Optional[date], end_date: Optional[date]) -> DashboardSummaryResponse:
    """KPIs y serie diaria agregados directamente sobre `Order.total`/`Order.total_quantity`
    - a diferencia de top-products/top-categories, no hace falta desempacar `items` porque
    estas columnas ya tienen el total por orden."""
    start_dt, end_dt, resolved_start, resolved_end = _resolve_date_range(start_date, end_date)
    filters = (Order.status == "PAID", Order.deleted_at.is_(None), Order.created_at >= start_dt, Order.created_at < end_dt)

    order_count, total_revenue, total_units_sold = (await db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(Order.total), 0),
            func.coalesce(func.sum(Order.total_quantity), 0),
        ).where(*filters)
    )).one()

    daily_rows = (await db.execute(
        select(
            func.date_trunc("day", Order.created_at).label("day"),
            func.count().label("order_count"),
            func.sum(Order.total).label("revenue"),
            func.sum(Order.total_quantity).label("units_sold"),
        ).where(*filters).group_by(text("day")).order_by(text("day"))
    )).all()

    average_order_value = float(total_revenue) / order_count if order_count else 0.0

    return DashboardSummaryResponse(
        start_date=resolved_start,
        end_date=resolved_end,
        total_revenue=float(total_revenue),
        order_count=order_count,
        average_order_value=average_order_value,
        total_units_sold=float(total_units_sold),
        daily=[
            DashboardDailyPoint(
                date=row.day.date(),
                revenue=float(row.revenue),
                order_count=row.order_count,
                units_sold=float(row.units_sold),
            )
            for row in daily_rows
        ],
    )


async def get_top_products(
    db: AsyncSession,
    start_date: Optional[date],
    end_date: Optional[date],
    sort_by: Literal["revenue", "quantity"],
    limit: int,
    offset: int,
) -> tuple[int, list[DashboardTopProduct]]:
    """Agregado directamente desde el JSON de `Order.items` (snapshot al momento de la
    venta) via jsonb_array_elements - deliberadamente NO se une contra el `Product` actual,
    asi el nombre/sku/imagen mostrados son historicamente correctos aunque el producto haya
    sido renombrado, re-precificado o marcado is_deleted despues."""
    start_dt, end_dt, _, _ = _resolve_date_range(start_date, end_date)
    order_col = _SORT_COLUMNS[sort_by]

    grouped_sql = """
        SELECT item->>'uuid' AS product_uuid,
               MAX(item->>'description') AS name,
               MAX(item->>'sku') AS sku,
               MAX(item->>'imageUrl') AS image_url,
               SUM((item->>'quantity')::numeric) AS units_sold,
               SUM((item->>'amountTax')::numeric) AS revenue
        FROM orders o, jsonb_array_elements(o.items::jsonb) AS item
        WHERE o.status = 'PAID' AND o.deleted_at IS NULL
          AND o.created_at >= :start_dt AND o.created_at < :end_dt
        GROUP BY item->>'uuid'
    """
    params = {"start_dt": start_dt, "end_dt": end_dt}

    total = (await db.execute(text(f"SELECT COUNT(*) FROM ({grouped_sql}) AS sub"), params)).scalar_one()

    rows = (await db.execute(
        text(f"{grouped_sql} ORDER BY {order_col} DESC LIMIT :limit OFFSET :offset"),
        {**params, "limit": limit, "offset": offset},
    )).all()

    return total or 0, [
        DashboardTopProduct(
            product_uuid=row.product_uuid,
            sku=row.sku,
            name=row.name,
            image_url=row.image_url,
            units_sold=float(row.units_sold),
            revenue=float(row.revenue),
        )
        for row in rows
    ]


async def get_top_categories(
    db: AsyncSession,
    start_date: Optional[date],
    end_date: Optional[date],
    sort_by: Literal["revenue", "quantity"],
    limit: int,
    offset: int,
) -> tuple[int, list[DashboardCategorySales]]:
    """Agrupa ventas por categoria PIM (`product_categories`/`categories`), uniendo el
    `uuid` de cada linea de `Order.items` contra el `Product` actual por `sicar_uuid` -
    deliberadamente SIN filtrar is_deleted/is_active (la categoria asignada a un producto no
    cambia porque se haya descontinuado, y la venta historica debe seguir atribuida a ella).
    Dos comportamientos deliberados de v1:
    - Un producto en varias categorias suma su revenue/unidades completas a CADA una - es un
      desglose por faceta, no una particion (los totales no van a sumar el total general).
    - Productos sin ninguna categoria asignada quedan excluidos (inner joins, sin bucket
      "Sin categoria")."""
    start_dt, end_dt, _, _ = _resolve_date_range(start_date, end_date)
    order_col = _SORT_COLUMNS[sort_by]

    grouped_sql = """
        WITH order_items AS (
            SELECT item->>'uuid' AS product_uuid,
                   (item->>'quantity')::numeric AS quantity,
                   (item->>'amountTax')::numeric AS revenue
            FROM orders o, jsonb_array_elements(o.items::jsonb) AS item
            WHERE o.status = 'PAID' AND o.deleted_at IS NULL
              AND o.created_at >= :start_dt AND o.created_at < :end_dt
        )
        SELECT c.uuid AS category_uuid, c.name AS category_name,
               SUM(oi.quantity) AS units_sold, SUM(oi.revenue) AS revenue
        FROM order_items oi
        JOIN products p ON p.sicar_uuid = oi.product_uuid
        JOIN product_categories pc ON pc.product_id = p.id
        JOIN categories c ON c.uuid = pc.category_uuid
        GROUP BY c.uuid, c.name
    """
    params = {"start_dt": start_dt, "end_dt": end_dt}

    total = (await db.execute(text(f"SELECT COUNT(*) FROM ({grouped_sql}) AS sub"), params)).scalar_one()

    rows = (await db.execute(
        text(f"{grouped_sql} ORDER BY {order_col} DESC LIMIT :limit OFFSET :offset"),
        {**params, "limit": limit, "offset": offset},
    )).all()

    return total or 0, [
        DashboardCategorySales(
            category_uuid=row.category_uuid,
            category_name=row.category_name,
            units_sold=float(row.units_sold),
            revenue=float(row.revenue),
        )
        for row in rows
    ]
