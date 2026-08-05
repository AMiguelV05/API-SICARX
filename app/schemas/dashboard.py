from datetime import date
from typing import List, Optional
from app.schemas.base import CamelModel


class DashboardDailyPoint(CamelModel):
    date: date
    revenue: float
    order_count: int
    units_sold: float


class DashboardSummaryResponse(CamelModel):
    start_date: date
    end_date: date
    total_revenue: float
    order_count: int
    average_order_value: float
    total_units_sold: float
    daily: List[DashboardDailyPoint]


class DashboardTopProduct(CamelModel):
    product_uuid: str
    sku: Optional[str] = None
    name: Optional[str] = None
    image_url: Optional[str] = None
    units_sold: float
    revenue: float


class DashboardTopProductsResponse(CamelModel):
    total: int
    docs: List[DashboardTopProduct]


class DashboardCategorySales(CamelModel):
    category_uuid: str
    category_name: str
    units_sold: float
    revenue: float


class DashboardTopCategoriesResponse(CamelModel):
    total: int
    docs: List[DashboardCategorySales]
