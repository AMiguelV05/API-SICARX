from pydantic import BaseModel, Field
from typing import List, Optional

# Filtros de entrada
class LocalCatalogFilter(BaseModel):
    limit: int = Field(default=60, description="Cantidad de productos por página")
    offset: int = Field(default=0, description="Paginación (inicio)")
    department_uuid: Optional[str] = None
    category_uuid: Optional[str] = None
    tag: Optional[str] = None

# Modelo de salida
class ProductBasic(BaseModel):
    sicar_uuid: str 
    sku: Optional[str]
    name: str
    description_details: Optional[str]
    image_url: Optional[str]
    price: float

    class Config:
        from_attributes = True

# Respuesta completa con paginación
class LocalCatalogResponse(BaseModel):
    total: int
    docs: List[ProductBasic]