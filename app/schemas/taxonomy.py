from __future__ import annotations
from typing import List, Optional
from datetime import datetime
from pydantic import Field
from app.schemas.base import CamelModel
from app.schemas.products import ProductBasic

class CategoryNode(CamelModel):
    """Nodo recursivo del arbol de categorias; reemplaza Department/Category (ya no hay distincion) - cambio de forma respecto a la version anterior, requiere actualizar el frontend."""
    uuid: str
    name: str
    slug: str
    children: List["CategoryNode"] = []

class TaxonomyResponse(CamelModel):
    categories: List[CategoryNode]

# Admin (/v1/admin/categories/*): CRUD del arbol y asignacion de productos, distinto de CategoryNode/TaxonomyResponse (solo lectura, GET /taxonomy).

class CategoryAdminPublic(CamelModel):
    uuid: str
    name: str
    slug: str
    parent_uuid: Optional[str] = None
    updated_at: datetime

class CategoryCreateRequest(CamelModel):
    name: str = Field(min_length=1)
    parent_uuid: Optional[str] = Field(default=None, description="Nodo padre - omitir o null para crear un nodo raiz")

class CategoryUpdateRequest(CamelModel):
    """Actualizacion parcial (exclude_unset=True): {"parentUuid": null} explicito mueve el nodo a raiz."""
    name: Optional[str] = Field(default=None, min_length=1)
    parent_uuid: Optional[str] = Field(default=None, description="Nuevo padre; null mueve el nodo a raiz")

class ReplaceCategoryProductsRequest(CamelModel):
    product_uuids: List[str] = Field(default_factory=list, max_length=5000, description="sicar_uuid de cada producto - reemplaza el conjunto completo asignado a la categoria")

class ReplaceCategoryProductsResponse(CamelModel):
    category_uuid: str
    product_uuids: List[str]

class CategoryProductsResponse(CamelModel):
    total: int
    docs: List[ProductBasic]
