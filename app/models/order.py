import uuid
from sqlalchemy import Column, Integer, String, Text, Numeric, JSON, DateTime, ForeignKey, Index, UniqueConstraint, func, text
from sqlalchemy.orm import relationship
from app.core.database import Base

class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String, unique=True, index=True, nullable=False, default=lambda: str(uuid.uuid4()))

    # Sin ondelete=CASCADE (a diferencia de ClientAddress): orden es registro financiero, no debe borrarse en cascada.
    client_account_id = Column(
        Integer, ForeignKey("client_accounts.id"), nullable=False, index=True
    )

    # Identificador publico, generado localmente (uuid4()) - nombre historico de cuando venia de Sicar X; se conserva por el contrato de URL con el frontend.
    sicar_order_id = Column(String, unique=True, index=True, nullable=False)
    # Sin uso real (ya no hay documento Sicar X del que salga un folio) - se conserva por seguir expuesta en el contrato publico.
    serie_folio = Column(String, nullable=True)

    # PAID: pagado; TO_PAY: creada, pago pendiente/en proceso; CANCELLED: rechazado o cancelado manualmente.
    status = Column(String, nullable=False, default="TO_PAY")

    # Estado de cumplimiento segun Sicar X (dispatchStatus) - dimension separada de `status` (pago/cancelacion local).
    dispatch_status = Column(String, nullable=True)

    branch_id = Column(Integer, nullable=True)
    total = Column(Numeric(10, 2), nullable=False)
    total_quantity = Column(Numeric(10, 2), nullable=False)

    # Placeholder sin usar hoy: no se calcula ni se suma a Mercado Pago ni a Sicar X.
    delivery_cost = Column(Numeric(10, 2), nullable=True)

    # Snapshot al crear la orden (mismo shape de build_order_payload); items agrega imageUrl por linea, no enviado a Sicar X.
    delivery_info = Column(JSON, nullable=False)
    items = Column(JSON, nullable=False)

    # Foto fija (ClientAddressPublic) de la direccion al crear la orden, solo DELIVERYMAN - evita usar una direccion editada despues.
    delivery_address_snapshot = Column(JSON, nullable=True)

    # Guia de envio via envia.com; None hasta generarse, sin regeneracion (409 si ya existe).
    shipping_label = Column(JSON, nullable=True)

    # Auditoria de POST /admin/orders/{uuid}/shipping/cancel - mismo patron que cancellation_reason
    # (texto libre, no hay modelo de usuarios admin). NULL si nunca se cancelo una guia en esta orden;
    # no se sobrescriben si se genera y cancela una guia mas de una vez, solo reflejan la ultima vez.
    shipping_cancellation_reason = Column(Text, nullable=True)
    shipping_label_cancelled_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=func.now())

    # Soft-delete: el worker necesita que la fila siga existiendo para reintentar la cancelacion en Sicar X; se filtra en consultas de cliente.
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    # Datos del pago con Mercado Pago; nulos hasta el primer intento de cobro.
    mp_payment_id = Column(String, unique=True, index=True, nullable=True)
    mp_status = Column(String, nullable=True)  # approved/pending/in_process/rejected/cancelled/refunded
    mp_status_detail = Column(String, nullable=True)
    mp_payment_method_id = Column(String, nullable=True)  # visa/oxxo/account_money/...
    mp_ticket_url = Column(String, nullable=True)  # external_resource_url (p. ej. ficha OXXO)

    # Persistido para reconstruir OrderResponse en un reintento idempotente sin volver a llamar a Mercado Pago.
    mp_preference_id = Column(String, nullable=True)

    # Aceptacion admin; accepted_by es texto libre (no hay modelo de usuarios admin). Independiente de dispatch_status.
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    accepted_by = Column(String, nullable=True)

    # Metadato local de mensajeria, texto libre - sin llamada a ninguna API de paqueteria.
    delivery_company = Column(String, nullable=True)
    delivery_assigned_at = Column(DateTime(timezone=True), nullable=True)

    # Motivo de cancelacion cuando un admin cancela la orden (POST /admin/orders/{uuid}/cancel).
    # NULL para cancelaciones del cliente (POST /orders/{id}/cancel, DELETE, pago rechazado).
    cancellation_reason = Column(Text, nullable=True)

    # Cupon aplicado, si hubo uno - ver CouponRedemption para el ciclo de vida del uso.
    # coupon_code es una foto fija del texto usado (el Coupon puede editarse/borrarse despues).
    # subtotal es el total ANTES del descuento; NULL en ordenes historicas (tratar como == total).
    coupon_id = Column(Integer, ForeignKey("coupons.id"), nullable=True)
    coupon_code = Column(String, nullable=True)
    discount_amount = Column(Numeric(10, 2), nullable=True)
    subtotal = Column(Numeric(10, 2), nullable=True)

    client_account = relationship("ClientAccount", back_populates="orders")


class OrderIdempotencyKey(Base):
    """Soporte de idempotencia para POST /orders: evita duplicar la orden en un reintento/doble-submit."""
    __tablename__ = "order_idempotency_keys"
    __table_args__ = (
        UniqueConstraint("client_account_id", "idempotency_key", name="ux_order_idempotency_client_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    client_account_id = Column(Integer, ForeignKey("client_accounts.id"), nullable=False, index=True)
    idempotency_key = Column(String, nullable=False)
    # NULL mientras la orden se sigue creando o si un intento previo se abandono a medio camino.
    order_uuid = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SicarSyncOutbox(Base):
    """Cola de sincronizacion asincrona hacia Sicar X (drenada con reintentos por
    sicar_sync_worker.py) para que un Sicar X caido nunca bloquee una cancelacion/aceptacion
    local. `status` incluye IN_PROGRESS para soltar el lock antes de la llamada HTTP; una
    fila IN_PROGRESS vieja (worker caido a medio proceso) vuelve a ser reclamable."""
    __tablename__ = "sicar_sync_outbox"
    __table_args__ = (
        Index("ix_sicar_sync_outbox_order_id", "order_id"),
        Index("ix_sicar_sync_outbox_status_next_attempt_at", "status", "next_attempt_at"),
        Index(
            "ix_sicar_sync_outbox_one_pending_per_order_action",
            "order_id", "action",
            unique=True,
            postgresql_where=text("status in ('PENDING', 'IN_PROGRESS')"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    action = Column(String, nullable=False)  # "CANCEL"/"ACCEPT" hoy
    cash_register_uuid = Column(String, nullable=False)
    status = Column(String, nullable=False, default="PENDING")  # PENDING/IN_PROGRESS/SUCCEEDED/FAILED
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(String, nullable=True)
    next_attempt_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=func.now())
