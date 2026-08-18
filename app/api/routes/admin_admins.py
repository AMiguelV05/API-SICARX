from fastapi import APIRouter, Body, Depends, Query
from app.core.database import DbDep
from app.core.security import SuperAdminDep
from app.schemas.admin_auth import AdminUserCreate, AdminUserUpdate, AdminUserPublic, AdminUserListResponse
from app.services import admin_auth_service, audit_service

router = APIRouter(prefix="/admin/admins", tags=["Admin - Admin Users"], dependencies=[Depends(SuperAdminDep)])


@router.post("", response_model=AdminUserPublic, summary="Crear una cuenta de administrador")
async def create_admin(db: DbDep, current: SuperAdminDep, data: AdminUserCreate = Body()):
    """Solo super_admin. `409` en correo duplicado."""
    admin = await admin_auth_service.create_admin_user(db, data)
    await audit_service.log_action(db, current, "admin_user.create", "admin_user", admin.uuid, {"email": admin.email, "role": admin.role})
    await db.commit()
    return AdminUserPublic.model_validate(admin)


@router.get("", response_model=AdminUserListResponse, summary="Listar administradores")
async def list_admins(db: DbDep, limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0)):
    total, admins = await admin_auth_service.list_admin_users(db, limit, offset)
    return AdminUserListResponse(total=total, docs=[AdminUserPublic.model_validate(a) for a in admins])


@router.patch("/{admin_uuid}", response_model=AdminUserPublic, summary="Actualizar una cuenta de administrador")
async def update_admin(admin_uuid: str, db: DbDep, current: SuperAdminDep, data: AdminUserUpdate = Body()):
    """Solo super_admin. Actualizacion parcial (`exclude_unset` a nivel de request,
    ver `AdminUserUpdate`) - incluye restablecer la contraseña de otro admin
    directamente, sin flujo de correo (ver CLAUDE.md)."""
    admin = await admin_auth_service.update_admin_user(db, admin_uuid, data)
    detail = data.model_dump(exclude_unset=True, exclude={"new_password"})
    if data.new_password is not None:
        detail["password_reset"] = True
    await audit_service.log_action(db, current, "admin_user.update", "admin_user", admin.uuid, detail)
    await db.commit()
    return AdminUserPublic.model_validate(admin)
