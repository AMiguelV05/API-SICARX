import logging
from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.admin_user import AdminUser
from app.schemas.admin_auth import AdminLoginRequest, AdminUserCreate, AdminUserUpdate
from app.core.security import hash_password, verify_password, _hash_password_sync

logger = logging.getLogger(__name__)

VALID_ROLES = ("super_admin", "staff")

# Mismo patron anti-enumeracion que client_service._DUMMY_HASH.
_DUMMY_HASH = _hash_password_sync("timing-attack-mitigation-dummy-password")


async def authenticate_admin(db: AsyncSession, data: AdminLoginRequest) -> AdminUser:
    email = data.email.lower()
    admin = await db.scalar(select(AdminUser).where(AdminUser.email == email))

    password_ok = await verify_password(data.password, admin.hashed_password if admin else _DUMMY_HASH)
    if not admin or not password_ok:
        logger.info(f"Intento de login admin fallido para: {email}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Correo o contraseña incorrectos.")

    if not admin.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Esta cuenta de administrador está desactivada.")

    admin.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(admin)

    logger.info(f"Login admin exitoso: {admin.email} (role={admin.role}).")
    return admin


async def create_admin_user(db: AsyncSession, data: AdminUserCreate) -> AdminUser:
    if data.role not in VALID_ROLES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"role debe ser uno de: {', '.join(VALID_ROLES)}.")

    email = data.email.lower()
    admin = AdminUser(
        email=email,
        name=data.name,
        hashed_password=await hash_password(data.password),
        role=data.role,
    )
    db.add(admin)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya existe un administrador con ese correo.")
    await db.refresh(admin)
    logger.info(f"AdminUser creado: {admin.email} (role={admin.role}).")
    return admin


async def list_admin_users(db: AsyncSession, limit: int, offset: int) -> tuple[int, list[AdminUser]]:
    total = await db.scalar(select(func.count()).select_from(AdminUser))
    result = await db.execute(select(AdminUser).order_by(AdminUser.created_at.desc()).limit(limit).offset(offset))
    return total or 0, list(result.scalars().all())


async def update_admin_user(db: AsyncSession, admin_uuid: str, data: AdminUserUpdate) -> AdminUser:
    admin = await db.scalar(select(AdminUser).where(AdminUser.uuid == admin_uuid))
    if not admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Administrador no encontrado.")

    if data.role is not None:
        if data.role not in VALID_ROLES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"role debe ser uno de: {', '.join(VALID_ROLES)}.")
        admin.role = data.role
    if data.name is not None:
        admin.name = data.name
    if data.is_active is not None:
        admin.is_active = data.is_active
    if data.new_password is not None:
        admin.hashed_password = await hash_password(data.new_password)

    await db.commit()
    await db.refresh(admin)
    logger.info(f"AdminUser {admin.email} actualizado por otro super_admin.")
    return admin
