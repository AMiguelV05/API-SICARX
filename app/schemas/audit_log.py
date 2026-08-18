from datetime import datetime
from typing import Optional
from app.schemas.base import CamelModel


class AdminAuditLogPublic(CamelModel):
    id: int
    admin_user_uuid: str
    admin_user_name: str
    action: str
    resource_type: str
    resource_id: str
    detail: Optional[dict] = None
    created_at: datetime


class AdminAuditLogListResponse(CamelModel):
    total: int
    docs: list[AdminAuditLogPublic]
