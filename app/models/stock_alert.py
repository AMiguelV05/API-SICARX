import uuid
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Index, func, text
from sqlalchemy.orm import relationship
from app.core.database import Base


class StockAlert(Base):
    """Suscripcion "Avisame cuando llegue" a un producto agotado (POST /products/{uuid}/stock-alerts).
    Activa = notified_at IS NULL AND unsubscribed_at IS NULL AND expires_at > now(). Solo se
    puede crear con el producto agotado, asi que stock_alert_worker.py trata cualquier
    suscripcion activa cuyo producto ya tenga existencia como "volvio a haber": encola el
    webhook product-back-in-stock y la marca notified_at (una sola vez). Ver CLAUDE.md,
    "Avisos de reabastecimiento"."""
    __tablename__ = "stock_alerts"
    __table_args__ = (
        # Idempotencia de la suscripcion: una sola activa por producto+correo.
        Index(
            "ux_stock_alerts_active_product_email", "product_id", "email", unique=True,
            postgresql_where=text("notified_at IS NULL AND unsubscribed_at IS NULL"),
        ),
        # El worker solo recorre las activas.
        Index(
            "ix_stock_alerts_active_product", "product_id",
            postgresql_where=text("notified_at IS NULL AND unsubscribed_at IS NULL"),
        ),
    )

    id = Column(Integer, primary_key=True)
    uuid = Column(String, unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    # Sin ondelete, mismo motivo que WishlistItem.product_id (los productos nunca se borran).
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    email = Column(String, nullable=False, index=True)  # siempre en minusculas
    # Null para invitados. Con cuenta, `email` es el de la cuenta al suscribirse.
    client_account_id = Column(Integer, ForeignKey("client_accounts.id", ondelete="CASCADE"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    notified_at = Column(DateTime(timezone=True), nullable=True)
    unsubscribed_at = Column(DateTime(timezone=True), nullable=True)

    product = relationship("Product")
    client_account = relationship("ClientAccount")
