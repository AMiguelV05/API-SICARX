from typing import Optional, List
from datetime import datetime
from pydantic import field_validator
from app.schemas.base import CamelModel
from app.schemas.orders import ProductItem
from app.core.sicar_validation import is_safe_sicar_id

class CartReplace(CamelModel):
    items: List[ProductItem]

    @field_validator("items")
    @classmethod
    def validate_items(cls, items):
        for item in items:
            if item.quantity <= 0:
                raise ValueError(f"La cantidad debe ser mayor a 0 (producto {item.uuid}).")
            if not is_safe_sicar_id(item.uuid):
                raise ValueError(f"UUID de producto invalido: {item.uuid}")
        return items

class CartMergeRequest(CamelModel):
    cartToken: str

class CartItemPublic(CamelModel):
    productUuid: str
    sku: Optional[str] = None
    name: Optional[str] = None
    imageUrl: Optional[str] = None
    price: Optional[float] = None
    stock: Optional[float] = None
    quantity: float
    lineTotal: Optional[float] = None
    available: bool

class CartResponse(CamelModel):
    items: List[CartItemPublic]
    subtotal: float
    totalQuantity: float
    cartToken: Optional[str] = None
    updatedAt: Optional[datetime] = None
