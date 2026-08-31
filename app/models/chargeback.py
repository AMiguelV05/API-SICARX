from sqlalchemy import Column, Integer, String, Numeric, Boolean, DateTime, ForeignKey, Index, func
from app.core.database import Base


class Chargeback(Base):
    """Un contracargo ("Compra no reconocida") sobre una orden ya PAID. Una orden puede
    tener varias filas a lo largo del tiempo (poco comun, pero no se asume unicidad).
    mp_chargeback_id es NULL hasta que llega la notificacion enriquecida del topic
    "chargebacks" de Mercado Pago (GET /v1/chargebacks/{id}) - el camino de deteccion via
    el topic "payment" (payment.status == "charged_back") puede crear/tocar esta fila
    antes de conocer ese id real, ver chargeback_service.py. Solo afecta dinero/registro:
    no toca Order.status ni Product.stock/reserved - mismo criterio que Refund (ver
    CLAUDE.md, "Contracargos de Mercado Pago")."""
    __tablename__ = "chargebacks"
    __table_args__ = (
        Index("ix_chargebacks_order_id_created_at", "order_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    mp_chargeback_id = Column(String, unique=True, index=True, nullable=True)
    mp_payment_id = Column(String, index=True, nullable=True)
    amount = Column(Numeric(10, 2), nullable=True)
    reason = Column(String, nullable=True)
    # IN_PROCESS/WON/LOST - mapeado de coverage_applied de Mercado Pago (None/true/false).
    status = Column(String, nullable=False, default="IN_PROCESS")
    coverage_eligible = Column(Boolean, nullable=True)
    documentation_required = Column(Boolean, nullable=True)
    documentation_deadline = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    resolved_at = Column(DateTime(timezone=True), nullable=True)
