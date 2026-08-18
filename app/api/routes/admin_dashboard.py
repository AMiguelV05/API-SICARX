import logging
from datetime import date
from typing import Literal, Optional
from fastapi import APIRouter, Depends, Query
from app.core.database import DbDep
from app.core.security import get_current_admin
from app.schemas.dashboard import (
    DashboardSummaryResponse,
    DashboardTopProductsResponse,
    DashboardTopCategoriesResponse,
)
from app.services import dashboard_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/dashboard", tags=["Admin - Dashboard"], dependencies=[Depends(get_current_admin)])


@router.get("/summary", response_model=DashboardSummaryResponse, summary="KPIs de ventas (revenue, ordenes, AOV) y serie diaria para un rango de fechas")
async def admin_dashboard_summary(
    db: DbDep,
    start_date: Optional[date] = Query(default=None, alias="startDate", description="Por defecto, hoy - 29 dias (ultimos 30 dias)"),
    end_date: Optional[date] = Query(default=None, alias="endDate", description="Por defecto, hoy (UTC)"),
):
    """Agregado directamente sobre `Order.total`/`Order.total_quantity` (sin desempacar
    `items`) para ordenes `PAID` no eliminadas en el rango. Incluye una serie diaria
    (`daily`) pensada para graficar."""
    return await dashboard_service.get_summary(db, start_date, end_date)


@router.get("/top-products", response_model=DashboardTopProductsResponse, summary="Productos mas vendidos en un rango de fechas (por revenue o cantidad)")
async def admin_dashboard_top_products(
    db: DbDep,
    start_date: Optional[date] = Query(default=None, alias="startDate"),
    end_date: Optional[date] = Query(default=None, alias="endDate"),
    sort_by: Literal["revenue", "quantity"] = Query(default="revenue", alias="sortBy"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Agregado desde el snapshot de `Order.items` (no desde el `Product` actual) - nombre/
    sku/imagen mostrados son los vigentes al momento de cada venta, correctos incluso si el
    producto fue renombrado o eliminado despues."""
    total, docs = await dashboard_service.get_top_products(db, start_date, end_date, sort_by, limit, offset)
    return DashboardTopProductsResponse(total=total, docs=docs)


@router.get("/top-categories", response_model=DashboardTopCategoriesResponse, summary="Ventas por categoria PIM en un rango de fechas (por revenue o cantidad)")
async def admin_dashboard_top_categories(
    db: DbDep,
    start_date: Optional[date] = Query(default=None, alias="startDate"),
    end_date: Optional[date] = Query(default=None, alias="endDate"),
    sort_by: Literal["revenue", "quantity"] = Query(default="revenue", alias="sortBy"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Agrupa por la taxonomia PIM (`product_categories`/`categories`), no por el
    `category_uuid`/`department_uuid` sincronizado de Sicar X. Un producto en varias
    categorias suma su revenue/unidades completas a cada una (desglose por faceta, no
    particion); productos sin categoria asignada quedan excluidos."""
    total, docs = await dashboard_service.get_top_categories(db, start_date, end_date, sort_by, limit, offset)
    return DashboardTopCategoriesResponse(total=total, docs=docs)
