import uuid
from sqlalchemy import Column, Integer, String, JSON, DateTime, Index, func, text
from app.core.database import Base


class WebhookOutbox(Base):
    """Cola persistente de webhooks salientes hacia el frontend (drenada con reintentos por
    webhook_outbox_worker.py). Solo la usan los webhooks que nacen en el worker
    (product-back-in-stock, wishlist-price-drop, cart-abandoned): se encolan en la MISMA
    transaccion que marca la suscripcion/carrito como notificado, asi que el correo nunca se
    pierde ni se duplica por un fallo entre ambos pasos. Los webhooks anteriores
    (order-confirmed, verification-requested, ...) siguen enviandose directo, sin outbox.

    `uuid` viaja como encabezado X-Webhook-Id, igual en cada reintento, para que el frontend
    descarte duplicados. Mismo ciclo de status que SicarSyncOutbox (IN_PROGRESS suelta el
    lock antes del HTTP; una fila IN_PROGRESS vieja vuelve a ser reclamable)."""
    __tablename__ = "webhook_outbox"
    __table_args__ = (
        Index(
            "ix_webhook_outbox_claimable", "next_attempt_at",
            postgresql_where=text("status IN ('PENDING', 'IN_PROGRESS')"),
        ),
    )

    id = Column(Integer, primary_key=True)
    uuid = Column(String, unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    event = Column(String, nullable=False)  # p. ej. "product-back-in-stock"
    path = Column(String, nullable=False)  # relativo a FRONTEND_BASE_URL
    payload = Column(JSON, nullable=False)
    status = Column(String, nullable=False, default="PENDING")  # PENDING/IN_PROGRESS/SUCCEEDED/FAILED
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_error = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
