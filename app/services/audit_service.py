from datetime import datetime
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.admin_user import AdminUser
from app.models.audit_log import AdminAuditLog


async def log_action(
    db: AsyncSession,
    admin: AdminUser,
    action: str,
    resource_type: str,
    resource_id: str,
    detail: dict | None = None,
) -> None:
    """Inserta la fila en la transaccion del llamador - sin commit propio, mismo patron
    que los inserts de SicarSyncOutbox. Llamar justo donde ocurre la mutacion, antes del
    commit de esa ruta/servicio."""
    db.add(AdminAuditLog(
        admin_user_id=admin.id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id),
        detail=detail,
    ))


async def list_audit_log(
    db: AsyncSession,
    *,
    admin_user_uuid: str | None,
    action: str | None,
    resource_type: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    limit: int,
    offset: int,
) -> tuple[int, list[tuple[AdminAuditLog, AdminUser]]]:
    """Devuelve pares (log, admin) - AdminAuditLog no tiene una relationship ORM hacia
    AdminUser (join explicito aqui en vez de un N+1 via awaitable_attrs), y el schema
    publico necesita el uuid/nombre del admin, no su id interno."""
    query = select(AdminAuditLog, AdminUser).join(AdminUser, AdminAuditLog.admin_user_id == AdminUser.id)
    count_query = select(func.count()).select_from(AdminAuditLog).join(AdminUser, AdminAuditLog.admin_user_id == AdminUser.id)

    if admin_user_uuid:
        query = query.where(AdminUser.uuid == admin_user_uuid)
        count_query = count_query.where(AdminUser.uuid == admin_user_uuid)
    if action:
        query = query.where(AdminAuditLog.action == action)
        count_query = count_query.where(AdminAuditLog.action == action)
    if resource_type:
        query = query.where(AdminAuditLog.resource_type == resource_type)
        count_query = count_query.where(AdminAuditLog.resource_type == resource_type)
    if date_from:
        query = query.where(AdminAuditLog.created_at >= date_from)
        count_query = count_query.where(AdminAuditLog.created_at >= date_from)
    if date_to:
        query = query.where(AdminAuditLog.created_at <= date_to)
        count_query = count_query.where(AdminAuditLog.created_at <= date_to)

    total = await db.scalar(count_query)
    result = await db.execute(query.order_by(AdminAuditLog.created_at.desc()).limit(limit).offset(offset))
    return total or 0, [(log, admin) for log, admin in result.all()]
