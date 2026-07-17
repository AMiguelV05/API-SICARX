import uuid
from sqlalchemy import Column, Integer, String, Numeric, JSON, DateTime, ForeignKey, func
from sqlalchemy.orm import relationship
from app.core.database import Base

class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String, unique=True, index=True, nullable=False, default=lambda: str(uuid.uuid4()))

    # Sin ondelete=CASCADE a proposito (a diferencia de ClientAddress): una orden es un
    # registro financiero que no queremos borrar en cascada si algun dia se agrega
    # eliminacion de cuentas (no existe esa funcionalidad hoy).
    client_account_id = Column(
        Integer, ForeignKey("client_accounts.id"), nullable=False, index=True
    )

    # Identificadores de Sicar X
    sicar_order_id = Column(String, unique=True, index=True, nullable=False)  # "id" de la respuesta de pago/dispatch
    serie_folio = Column(String, nullable=True)
    sicar_date = Column(DateTime(timezone=True), nullable=True)

    status = Column(String, nullable=False, default="PAID")

    # Estado de cumplimiento/entrega segun Sicar X (dispatchStatus en document-graph/v1/graph-v2:
    # PENDING_ACCEPTANCE, PENDING, PREPARING, COMPLETE, DISPATCHED - confirmado en vivo contra
    # una orden real, ver CLAUDE.md). Distinto de `status` (arriba), que es nuestro propio
    # tracking de pago/cancelacion - dos dimensiones separadas del mismo documento en Sicar.
    dispatch_status = Column(String, nullable=True)
    dispatch_history = Column(JSON, nullable=True)

    branch_id = Column(Integer, nullable=True)
    total = Column(Numeric(10, 2), nullable=False)
    total_quantity = Column(Numeric(10, 2), nullable=False)

    # Snapshot al momento de la orden (misma estructura que produce build_order_payload,
    # no se consulta dentro de estos campos, asi que JSON simple basta - igual que
    # Product.additional_skus/additional_images).
    delivery_info = Column(JSON, nullable=False)
    items = Column(JSON, nullable=False)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=func.now())

    client_account = relationship("ClientAccount", back_populates="orders")
