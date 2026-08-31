from datetime import datetime
from typing import Optional
from app.schemas.base import CamelModel


class ChargebackPublic(CamelModel):
    id: int
    order_id: int
    mp_chargeback_id: Optional[str] = None
    mp_payment_id: Optional[str] = None
    amount: Optional[float] = None
    reason: Optional[str] = None
    status: str
    coverage_eligible: Optional[bool] = None
    documentation_required: Optional[bool] = None
    documentation_deadline: Optional[datetime] = None
    created_at: datetime
    resolved_at: Optional[datetime] = None


class ChargebackListResponse(CamelModel):
    total: int
    docs: list[ChargebackPublic]
