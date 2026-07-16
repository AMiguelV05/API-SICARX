from pydantic import BaseModel, Field
from typing import List, Optional
from app.schemas.products import ProductBasic

class SearchFilter(BaseModel):
    q: str = Field(..., min_length=1, description="Texto a buscar en sku o nombre del producto")
    limit: int = Field(default=60, description="Cantidad de productos por página")
    offset: int = Field(default=0, description="Paginación (inicio)")
    department_uuid: Optional[str] = None
    category_uuid: Optional[str] = None
    in_stock: Optional[bool] = Field(default=False, description="Si es true, solo muestra productos con stock > 0")

class SearchResponse(BaseModel):
    total: int
    docs: List[ProductBasic]
