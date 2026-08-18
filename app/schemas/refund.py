from datetime import datetime
from typing import Optional
from pydantic import Field
from app.schemas.base import CamelModel


class RefundCreateRequest(CamelModel):
    amount: float = Field(gt=0, description="Monto a reembolsar, en la misma moneda de la orden (MXN)")
    reason: str = Field(min_length=1)


class RefundPublic(CamelModel):
    id: int
    order_id: int
    amount: float
    reason: str
    mp_refund_id: Optional[str] = None
    issued_by_admin_id: Optional[int] = None
    created_at: datetime


class RefundListResponse(CamelModel):
    total: int
    docs: list[RefundPublic]
