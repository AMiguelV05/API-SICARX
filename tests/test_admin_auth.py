"""Cubre AdminUser: login (anti-enumeracion, igual patron que client_service), y el 409
de creacion duplicada. Ver CLAUDE.md, "Admin RBAC y auditoria"."""
import pytest
from fastapi import HTTPException

from app.schemas.admin_auth import AdminLoginRequest, AdminUserCreate
from app.services import admin_auth_service


async def _make_admin(db, *, email="admin@example.com", password="correcta123", role="super_admin"):
    return await admin_auth_service.create_admin_user(
        db, AdminUserCreate(email=email, name="Admin de prueba", password=password, role=role)
    )


async def test_login_succeeds_with_correct_credentials(db):
    await _make_admin(db, email="ok@example.com", password="correcta123")

    admin = await admin_auth_service.authenticate_admin(
        db, AdminLoginRequest(email="ok@example.com", password="correcta123")
    )
    assert admin.email == "ok@example.com"
    assert admin.last_login_at is not None


async def test_login_fails_with_wrong_password(db):
    await _make_admin(db, email="ok2@example.com", password="correcta123")

    with pytest.raises(HTTPException) as exc_info:
        await admin_auth_service.authenticate_admin(
            db, AdminLoginRequest(email="ok2@example.com", password="incorrecta")
        )
    assert exc_info.value.status_code == 401


async def test_login_fails_for_nonexistent_email_same_error_as_wrong_password(db):
    """Mismo mensaje/status que una password incorrecta - no debe revelar si el correo existe."""
    with pytest.raises(HTTPException) as exc_info:
        await admin_auth_service.authenticate_admin(
            db, AdminLoginRequest(email="no-existe@example.com", password="lo-que-sea")
        )
    assert exc_info.value.status_code == 401


async def test_login_fails_for_deactivated_admin(db):
    admin = await _make_admin(db, email="inactivo@example.com", password="correcta123")
    admin.is_active = False
    await db.flush()

    with pytest.raises(HTTPException) as exc_info:
        await admin_auth_service.authenticate_admin(
            db, AdminLoginRequest(email="inactivo@example.com", password="correcta123")
        )
    assert exc_info.value.status_code == 403


async def test_create_admin_user_duplicate_email_raises_409(db):
    await _make_admin(db, email="dup@example.com")

    with pytest.raises(HTTPException) as exc_info:
        await _make_admin(db, email="dup@example.com")
    assert exc_info.value.status_code == 409
