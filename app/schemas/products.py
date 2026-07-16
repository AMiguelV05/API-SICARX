from pydantic import BaseModel, Field
from typing import List, Literal, Optional
from datetime import datetime

# Filtros de entrada
class LocalCatalogFilter(BaseModel):
    limit: int = Field(default=60, description="Cantidad de productos por página")
    offset: int = Field(default=0, description="Paginación (inicio)")
    department_uuid: Optional[str] = None
    category_uuid: Optional[str] = None
    tag: Optional[str] = None
    in_stock: Optional[bool] = Field(default=False, description="Si es true, solo muestra productos con stock > 0")
    sort_by: Optional[Literal["price_asc", "price_desc", "name_asc"]] = Field(
        default=None, description="Orden de los resultados: price_asc, price_desc o name_asc"
    )

# Modelo de salida
class ProductBasic(BaseModel):
    sicar_uuid: str 
    sku: str
    name: str
    description_details: Optional[str]
    image_url: Optional[str]
    price: float
    stock: float

    class Config:
        from_attributes = True

# Respuesta completa con paginación
class LocalCatalogResponse(BaseModel):
    total: int
    docs: List[ProductBasic]

# Detalle completo de producto (GET /products/{uuid})
class ProductDetail(BaseModel):
    id: int
    sicar_uuid: str
    sku: Optional[str]
    additional_skus: Optional[list]
    name: str
    description_details: Optional[str]
    image_url: Optional[str]
    tags: Optional[list]
    additional_images: Optional[list]
    sales_unit_uuid: Optional[str]
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

    class Config:
        from_attributes = True