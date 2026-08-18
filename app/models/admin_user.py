import uuid
from sqlalchemy import Column, Integer, String, Boolean, DateTime, func
from app.core.database import Base


class AdminUser(Base):
    """Cuenta de staff interno para /v1/admin/* - reemplaza el antiguo X-Admin-Key
    compartido (ver security.py). Nunca se borra fisicamente, solo se desactiva
    (is_active=False) - admin_audit_log.admin_user_id y Refund.issued_by_admin_id
    referencian esta tabla y necesitan que la fila siga existiendo para el historial."""
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String, unique=True, index=True, nullable=False, default=lambda: str(uuid.uuid4()))

    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    name = Column(String, nullable=False)

    # "super_admin" | "staff" - string plano, vocabulario restringido en la capa de
    # aplicacion (mismo patron que Order.status/ClientAccount.auth_provider).
    role = Column(String, nullable=False, default="staff")

    is_active = Column(Boolean, nullable=False, default=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=func.now())
