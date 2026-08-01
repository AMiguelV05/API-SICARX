from __future__ import annotations
from typing import List
from app.schemas.base import CamelModel

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
