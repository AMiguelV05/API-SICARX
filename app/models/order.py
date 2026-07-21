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

    # PAID: pagado (via Mercado Pago o legado); TO_PAY: orden creada en Sicar pero el
    # pago con Mercado Pago sigue pendiente/en proceso (OXXO, wallet, tarjeta en
    # revision); CANCELLED: pago rechazado/cancelado o cancelacion manual - ver
    # order_history_service.finalize_order_payment.
    status = Column(String, nullable=False, default="TO_PAY")

    # Estado de cumplimiento/entrega segun Sicar X (dispatchStatus en document-graph/v1/graph-v2:
    # PENDING_ACCEPTANCE, PENDING, PREPARING, COMPLETE, DISPATCHED - confirmado en vivo contra
    # una orden real, ver CLAUDE.md). Distinto de `status` (arriba), que es nuestro propio
    # tracking de pago/cancelacion - dos dimensiones separadas del mismo documento en Sicar.
    dispatch_status = Column(String, nullable=True)
    dispatch_history = Column(JSON, nullable=True)

    branch_id = Column(Integer, nullable=True)
    total = Column(Numeric(10, 2), nullable=False)
    total_quantity = Column(Numeric(10, 2), nullable=False)

    # Placeholder para una futura integracion de costo de envio (envia.com, API mexicana
    # de tarifas de paqueteria). Intencionalmente sin usar hoy: no se calcula, no se suma
    # al cobro de Mercado Pago (payment_service.py) ni a ecOrderDto.total enviado a Sicar X
    # - ambos siguen reflejando solo el total de productos, para PICKUP y DELIVERYMAN por igual.
    delivery_cost = Column(Numeric(10, 2), nullable=True)

    # Snapshot al momento de la orden (misma estructura que produce build_order_payload,
    # no se consulta dentro de estos campos, asi que JSON simple basta - igual que
    # Product.additional_skus/additional_images).
    delivery_info = Column(JSON, nullable=False)
    items = Column(JSON, nullable=False)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=func.now())

    # Datos del pago con Mercado Pago (ver app/services/payment_service.py). Nulos
    # mientras no se haya intentado ningun cobro (p. ej. justo despues de crear la
    # orden, antes de que el Payment Brick haga submit).
    mp_payment_id = Column(String, unique=True, index=True, nullable=True)
    mp_status = Column(String, nullable=True)  # approved/pending/in_process/rejected/cancelled/refunded
    mp_status_detail = Column(String, nullable=True)
    mp_payment_method_id = Column(String, nullable=True)  # visa/oxxo/account_money/...
    mp_ticket_url = Column(String, nullable=True)  # external_resource_url (p. ej. ficha OXXO)

    client_account = relationship("ClientAccount", back_populates="orders")
