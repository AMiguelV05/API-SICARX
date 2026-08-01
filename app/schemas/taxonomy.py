from __future__ import annotations
from typing import List, Optional
from datetime import datetime
from pydantic import Field
from app.schemas.base import CamelModel
from app.schemas.products import ProductBasic

class CategoryNode(CamelModel):
    """Nodo del arbol de categorias - recursivo (`children` es una lista del
    mismo tipo). Reemplaza a DepartmentWithCategories/CategoryBasic: ya no hay
    una distincion especial "departamento" vs "categoria", solo nodos con
    profundidad arbitraria (ver CLAUDE.md, "Taxonomia"). Cambio de forma de
    respuesta respecto a la version anterior - requiere actualizar el frontend."""
    uuid: str
    name: str
    slug: str
    children: List["CategoryNode"] = []

class TaxonomyResponse(CamelModel):
    categories: List[CategoryNode]

# Admin (/v1/admin/categories/*, ver CLAUDE.md "Admin API") - CRUD del arbol de
# categorias y asignacion de productos. Distinto de CategoryNode/TaxonomyResponse
# arriba (esas son de solo lectura para GET /taxonomy, publicas).

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
    """Actualizacion parcial - el servicio distingue "campo omitido" de "campo mandado
    en null" via `exclude_unset=True` (mismo patron que ClientAddressUpdate en
    address_service.py), asi que `{"parentUuid": null}` explicito mueve el nodo a raiz
    sin necesidad de un valor centinela aparte."""
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
