from typing import List, Literal, Optional
from datetime import datetime
from pydantic import Field
from app.schemas.base import CamelModel

class LocalCatalogFilter(CamelModel):
    limit: int = Field(default=60, ge=1, le=200, description="Cantidad de productos por página (1-200)")
    offset: int = Field(default=0, ge=0, description="Paginación (inicio)")
    department_uuid: Optional[str] = None
    category_uuid: Optional[str] = None
    taxonomy_uuid: Optional[str] = Field(default=None, description="UUID de un nodo del arbol de categorias propio (PIM, GET /taxonomy) - distinto de category_uuid (clasificacion cruda de Sicar X). Incluye productos etiquetados en descendientes del nodo.")
    vehicle_uuid: Optional[str] = Field(default=None, description="UUID de un fitment de vehiculo (GET /v1/vehicles) - filtra a productos compatibles con ese vehiculo.")
    tag: Optional[str] = None
    in_stock: Optional[bool] = Field(default=False, description="Si es true, solo muestra productos con stock > 0")
    sort_by: Optional[Literal["price_asc", "price_desc", "name_asc"]] = Field(
        default=None, description="Orden de los resultados: price_asc, price_desc o name_asc"
    )

class ProductBasic(CamelModel):
    sicar_uuid: str
    sku: str
    name: str
    description_details: Optional[str]
    image_url: Optional[str]
    price: float
    stock: float

class LocalCatalogResponse(CamelModel):
    total: int
    docs: List[ProductBasic]

class ProductDetail(CamelModel):
    id: int
    sicar_uuid: str
    sku: Optional[str]
    additional_skus: Optional[list[str]]
    name: str
    description_details: Optional[str]
    image_url: Optional[str]
    tags: Optional[list[str]]
    additional_images: Optional[list[str]]
    sales_unit_uuid: Optional[str]
    unit_short_name: Optional[str]
    department_uuid: Optional[str]
    category_uuid: Optional[str]
    price: float
    stock: float
    is_bulk: bool
    is_active: bool
    is_deleted: bool
    last_sync_id: Optional[str]
    details_updated_at: Optional[datetime]
    deleted_at: Optional[datetime]
