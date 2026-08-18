from fastapi import APIRouter, Body, Depends, Request
from app.core.database import DbDep
from app.core.rate_limit import limiter
from app.core.security import create_admin_token, CurrentAdminDep
from app.schemas.admin_auth import AdminLoginRequest, AdminAuthResponse, AdminUserPublic
from app.services import admin_auth_service

router = APIRouter(prefix="/admin/auth", tags=["Admin - Auth"])


@router.post("/login", response_model=AdminAuthResponse, summary="Login de administrador")
@limiter.limit("5/minute")
async def admin_login(request: Request, db: DbDep, data: AdminLoginRequest = Body()):
    """Login de staff interno (`AdminUser`) - reemplaza el antiguo `X-Admin-Key`
    compartido. Mismo patron anti-enumeracion (comparacion en tiempo constante) que
    `POST /auth/login` de cuentas de cliente."""
    admin = await admin_auth_service.authenticate_admin(db, data)
    token = create_admin_token(admin.uuid)
    return AdminAuthResponse(token=token, admin=AdminUserPublic.model_validate(admin))


@router.get("/me", response_model=AdminUserPublic, summary="Datos del administrador autenticado")
async def admin_me(admin: CurrentAdminDep):
    return AdminUserPublic.model_validate(admin)
