from typing import List, Optional
from datetime import datetime
from pydantic import Field
from app.schemas.base import CamelModel
from app.schemas.products import ProductBasic

class WishlistCollectionCreate(CamelModel):
    name: str = Field(min_length=1, description="Nombre de la lista, p. ej. 'Cumpleaños'")

class WishlistCollectionUpdate(CamelModel):
    name: Optional[str] = Field(default=None, min_length=1, description="Nuevo nombre de la lista")

class WishlistCollectionPublic(CamelModel):
    uuid: str
    name: str
    is_default: bool = Field(description="true para la lista 'Favoritos' fija - no se puede eliminar")
    item_count: int
    created_at: datetime

class WishlistItemCreate(CamelModel):
    product_uuid: str = Field(description="sicar_uuid del producto a guardar")

class WishlistItemPublic(CamelModel):
    product_uuid: str
    added_at: datetime
    available: bool = Field(description="false si el producto fue desactivado/eliminado del catalogo desde que se guardo - la fila se conserva, solo se oculta el detalle")
    product: Optional[ProductBasic] = Field(default=None, description="null si available=false")

class WishlistItemListResponse(CamelModel):
    total: int
    docs: List[WishlistItemPublic]
