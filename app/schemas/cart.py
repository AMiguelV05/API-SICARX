from typing import Optional, List
from datetime import datetime
from pydantic import Field, field_validator
from app.schemas.base import CamelModel
from app.schemas.orders import ProductItem, validate_coupon_code_format
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

class CartCouponApply(CamelModel):
    code: str = Field(min_length=1, max_length=40)

    @field_validator("code")
    @classmethod
    def validate_code(cls, v):
        return validate_coupon_code_format(v)

class CartItemDelta(CamelModel):
    productUuid: str
    delta: float

    @field_validator("productUuid")
    @classmethod
    def validate_product_uuid(cls, v):
        if not is_safe_sicar_id(v):
            raise ValueError(f"UUID de producto invalido: {v}")
        return v

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
    couponCode: Optional[str] = None
    couponValid: bool = Field(default=False, description="False si couponCode está seteado pero ya no aplica (expirado, no alcanza el mínimo, etc.) - ver couponInvalidReason. GET /cart siempre responde 200, nunca falla por esto.")
    couponInvalidReason: Optional[str] = Field(default=None, description="Motivo legible si couponValid es false. Null si no hay cupón o si es válido.")
    discountAmount: float = 0.0
    total: float = Field(default=0.0, description="subtotal - discountAmount, nunca negativo.")
