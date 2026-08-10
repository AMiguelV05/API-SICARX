from typing import List, Literal, Optional
from datetime import datetime
from pydantic import EmailStr, Field, field_validator, model_validator
from app.schemas.base import CamelModel
from app.schemas.orders import validate_coupon_code_format

DiscountType = Literal["PERCENTAGE", "FIXED_AMOUNT"]
CouponScopeType = Literal["ORDER", "CATEGORY", "PRODUCT"]
RedemptionStatus = Literal["PENDING", "CONFIRMED", "RELEASED"]


class CouponAdminPublic(CamelModel):
    uuid: str
    code: str
    discount_type: DiscountType
    discount_value: float
    max_discount_amount: Optional[float] = None
    scope_type: CouponScopeType
    min_order_amount: Optional[float] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    is_active: bool
    max_total_uses: Optional[int] = None
    max_uses_per_client: Optional[int] = None
    first_purchase_only: bool
    created_at: datetime
    updated_at: Optional[datetime] = None


class CouponCreateRequest(CamelModel):
    code: str = Field(min_length=1, max_length=40)
    discount_type: DiscountType
    discount_value: float = Field(gt=0)
    max_discount_amount: Optional[float] = Field(default=None, gt=0, description="Tope de monto descontado - solo tiene sentido con discountType=PERCENTAGE.")
    scope_type: CouponScopeType = "ORDER"
    min_order_amount: Optional[float] = Field(default=None, ge=0)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    is_active: bool = True
    max_total_uses: Optional[int] = Field(default=None, gt=0)
    max_uses_per_client: Optional[int] = Field(default=None, gt=0)
    first_purchase_only: bool = False

    @field_validator("code")
    @classmethod
    def validate_code(cls, v):
        return validate_coupon_code_format(v)

    @model_validator(mode="after")
    def validate_discount_shape(self):
        if self.discount_type == "PERCENTAGE" and self.discount_value > 100:
            raise ValueError("discountValue no puede exceder 100 para PERCENTAGE.")
        if self.discount_type == "FIXED_AMOUNT" and self.max_discount_amount is not None:
            raise ValueError("maxDiscountAmount solo aplica a discountType=PERCENTAGE.")
        if self.starts_at and self.ends_at and self.starts_at >= self.ends_at:
            raise ValueError("startsAt debe ser anterior a endsAt.")
        return self


class CouponUpdateRequest(CamelModel):
    """Actualizacion parcial (exclude_unset=True) - mismo patron que CategoryUpdateRequest."""
    code: Optional[str] = Field(default=None, min_length=1, max_length=40)
    discount_type: Optional[DiscountType] = None
    discount_value: Optional[float] = Field(default=None, gt=0)
    max_discount_amount: Optional[float] = Field(default=None, gt=0)
    scope_type: Optional[CouponScopeType] = None
    min_order_amount: Optional[float] = Field(default=None, ge=0)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    is_active: Optional[bool] = None
    max_total_uses: Optional[int] = Field(default=None, gt=0)
    max_uses_per_client: Optional[int] = Field(default=None, gt=0)
    first_purchase_only: Optional[bool] = None

    @field_validator("code")
    @classmethod
    def validate_code(cls, v):
        return validate_coupon_code_format(v)


class CouponListResponse(CamelModel):
    total: int
    docs: List[CouponAdminPublic]


class ReplaceCouponCategoriesRequest(CamelModel):
    category_uuids: List[str] = Field(default_factory=list, max_length=5000)


class ReplaceCouponCategoriesResponse(CamelModel):
    coupon_uuid: str
    category_uuids: List[str]


class ReplaceCouponProductsRequest(CamelModel):
    product_uuids: List[str] = Field(default_factory=list, max_length=5000, description="sicar_uuid de cada producto - reemplaza el conjunto completo del alcance del cupón.")


class ReplaceCouponProductsResponse(CamelModel):
    coupon_uuid: str
    product_uuids: List[str]


class ReplaceCouponClientsRequest(CamelModel):
    client_emails: List[EmailStr] = Field(default_factory=list, description="Vacío = cupón público (cualquier cliente puede intentarlo, sujeto a las demás reglas).")


class ReplaceCouponClientsResponse(CamelModel):
    coupon_uuid: str
    client_emails: List[str]


class CouponRedemptionAdminPublic(CamelModel):
    id: int
    client_account_id: int
    client_email: str
    order_uuid: str
    status: RedemptionStatus
    created_at: datetime
    updated_at: Optional[datetime] = None


class CouponRedemptionListResponse(CamelModel):
    total: int
    docs: List[CouponRedemptionAdminPublic]
