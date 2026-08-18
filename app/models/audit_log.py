from sqlalchemy import Column, Integer, String, JSON, DateTime, ForeignKey, Index, func
from app.core.database import Base


class AdminAuditLog(Base):
    """Registro de las mutaciones admin de mas alto valor (ver CLAUDE.md, "Admin RBAC y
    auditoria") - no cubre todavia las ~30 rutas /v1/admin/* completas, solo las
    llamadas explicitamente desde audit_service.log_action en esta primera pasada.
    Nunca se edita/borra despues de escrita."""
    __tablename__ = "admin_audit_log"
    __table_args__ = (
        Index("ix_admin_audit_log_admin_user_id_created_at", "admin_user_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    # Sin ondelete=CASCADE: un AdminUser nunca se borra fisicamente (solo is_active=False),
    # asi que esta FK siempre resuelve.
    admin_user_id = Column(Integer, ForeignKey("admin_users.id"), nullable=False, index=True)

    action = Column(String, nullable=False)  # p.ej. "order.accept", "coupon.delete"
    resource_type = Column(String, nullable=False)  # p.ej. "order", "coupon"
    # String, no Integer: el id vario entre uuid (Order/Coupon) e int segun el recurso.
    resource_id = Column(String, nullable=False)
    detail = Column(JSON, nullable=True)  # contexto extra opcional (p.ej. valores antes/despues)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
