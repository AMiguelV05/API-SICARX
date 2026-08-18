from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, Query
from app.core.database import DbDep
from app.core.security import SuperAdminDep
from app.schemas.audit_log import AdminAuditLogPublic, AdminAuditLogListResponse
from app.services import audit_service

router = APIRouter(prefix="/admin/audit-log", tags=["Admin - Audit Log"], dependencies=[Depends(SuperAdminDep)])


@router.get("", response_model=AdminAuditLogListResponse, summary="Historial de acciones admin")
async def list_audit_log(
    db: DbDep,
    admin_user_uuid: Optional[str] = Query(default=None, alias="adminUserUuid"),
    action: Optional[str] = Query(default=None),
    resource_type: Optional[str] = Query(default=None, alias="resourceType"),
    date_from: Optional[datetime] = Query(default=None, alias="dateFrom"),
    date_to: Optional[datetime] = Query(default=None, alias="dateTo"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Solo super_admin. Cubre las mutaciones registradas explicitamente via
    `audit_service.log_action` (accept/cancel/refund de ordenes, gestion de
    AdminUser, cupones, borrado de categoria/vehiculo, moderacion de resenas) - no
    todas las rutas /v1/admin/* todavia, ver CLAUDE.md."""
    total, rows = await audit_service.list_audit_log(
        db,
        admin_user_uuid=admin_user_uuid,
        action=action,
        resource_type=resource_type,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    docs = [
        AdminAuditLogPublic(
            id=log.id,
            admin_user_uuid=admin.uuid,
            admin_user_name=admin.name,
            action=log.action,
            resource_type=log.resource_type,
            resource_id=log.resource_id,
            detail=log.detail,
            created_at=log.created_at,
        )
        for log, admin in rows
    ]
    return AdminAuditLogListResponse(total=total, docs=docs)
