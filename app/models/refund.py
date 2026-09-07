from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, Index, func
from app.core.database import Base


class Refund(Base):
    """Un evento de reembolso (parcial o total) sobre una orden pagada. Una orden puede
    tener varias filas (varios reembolsos parciales); el total reembolsado es
    SUM(amount) WHERE order_id=... - no hay columna de acumulado aparte, para no tener
    una segunda fuente de verdad que mantener sincronizada. Solo afecta dinero: no
    restaura Product.stock/reserved (ver CLAUDE.md, "Reembolsos parciales") - un retorno
    fisico que necesite reponer inventario es un flujo aparte, no construido aqui."""
    __tablename__ = "refunds"
    __table_args__ = (
        Index("ix_refunds_order_id_created_at", "order_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    amount = Column(Numeric(10, 2), nullable=False)
    reason = Column(String, nullable=False)
    mp_refund_id = Column(String, nullable=True)  # id de Mercado Pago para este reembolso

    # NULL para el reembolso automatico de una cancelacion iniciada por el propio
    # cliente/invitado (POST /orders/{id}/cancel) - ahi no hay un AdminUser real
    # involucrado. Poblado para un reembolso parcial emitido via /admin (require_super_admin).
    # index=True: unica FK del esquema que se habia quedado sin indexar (todo el resto -
    # admin_audit_log.admin_user_id, coupon_redemptions.client_account_id, etc. - ya lo tiene).
    issued_by_admin_id = Column(Integer, ForeignKey("admin_users.id"), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
