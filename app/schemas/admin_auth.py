from datetime import datetime
from typing import Optional
from pydantic import EmailStr, Field
from app.schemas.base import CamelModel


class AdminLoginRequest(CamelModel):
    email: EmailStr
    password: str = Field(min_length=1)


class AdminUserPublic(CamelModel):
    uuid: str
    email: str
    name: str
    role: str
    is_active: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime


class AdminAuthResponse(CamelModel):
    token: str
    admin: AdminUserPublic


class AdminUserCreate(CamelModel):
    email: EmailStr
    name: str = Field(min_length=1)
    password: str = Field(min_length=8, description="Contraseña en texto plano, mínimo 8 caracteres")
    role: str = Field(description="\"super_admin\" o \"staff\"")


class AdminUserUpdate(CamelModel):
    name: Optional[str] = Field(default=None, min_length=1)
    role: Optional[str] = Field(default=None, description="\"super_admin\" o \"staff\"")
    is_active: Optional[bool] = None
    new_password: Optional[str] = Field(default=None, min_length=8, description="Restablece la contraseña de este admin directamente - sin flujo de correo, ver CLAUDE.md")


class AdminUserListResponse(CamelModel):
    total: int
    docs: list[AdminUserPublic]
