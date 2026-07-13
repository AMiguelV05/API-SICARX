from pydantic import BaseModel, Field
from typing import Optional

class SessionResponse(BaseModel):
    token: str = Field(..., description="JWT de sesión del cliente; se reenvía como Authorization en /orders")
    priceListUuid: Optional[str] = None
    branchId: Optional[int] = None
    deliveryCost: Optional[float] = None
    contentId: Optional[str] = Field(default=None, description="Extraído del claim `jti` del JWT")
