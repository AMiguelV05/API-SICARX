from typing import List, Optional
from pydantic import Field
from app.schemas.base import CamelModel
from app.schemas.products import ProductBasic

class SearchFilter(CamelModel):
    q: str = Field(min_length=1, description="Texto a buscar en sku o nombre del producto")
    limit: int = Field(default=60, ge=1, le=200, description="Cantidad de productos por página (1-200)")
    offset: int = Field(default=0, ge=0, description="Paginación (inicio)")
    department_uuid: Optional[str] = None
    category_uuid: Optional[str] = None
    taxonomy_uuid: Optional[str] = Field(default=None, description="UUID de un nodo del arbol de categorias propio (PIM, GET /taxonomy) - distinto de category_uuid (clasificacion cruda de Sicar X). Incluye productos etiquetados en descendientes del nodo.")
    vehicle_uuid: Optional[str] = Field(default=None, description="UUID de un fitment de vehiculo (GET /v1/vehicles) - filtra a productos compatibles con ese vehiculo.")
    in_stock: Optional[bool] = Field(default=False, description="Si es true, solo muestra productos con stock > 0")

class SearchResponse(CamelModel):
    total: int
    docs: List[ProductBasic]
