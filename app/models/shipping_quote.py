import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB

from app.core.database import Base


class ShippingQuote(Base):
    """Cotizacion de envio hecha en el checkout (POST /shipping/quotes), guardada en servidor
    para que POST /orders cobre el precio cotizado sin confiar en el cliente. Ver
    shipping_quote_service.py. Se borran a los 30 dias (worker)."""
    __tablename__ = "shipping_quotes"
    __table_args__ = (
        # Reuso de una cotizacion reciente para el mismo carrito y destino (cache).
        Index("ix_shipping_quotes_lookup", "items_fingerprint", "destination_key", "created_at"),
        # Barrido diario de las viejas.
        Index("ix_shipping_quotes_created_at", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    uuid = Column(String, unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    # NULL = invitado. Una cotizacion de cuenta solo la puede usar esa cuenta.
    client_account_id = Column(Integer, ForeignKey("client_accounts.id", ondelete="CASCADE"), nullable=True)

    # sha256 de "uuid:cantidad" ordenados - el pedido debe traer exactamente el mismo carrito.
    items_fingerprint = Column(String(64), nullable=False)
    # "zip|ESTADO|CIUDAD" normalizados - el pedido debe ir a la misma direccion.
    destination_key = Column(String, nullable=False)

    # QUOTED (hay opciones) | NOT_QUOTABLE (envio sin costo, el admin cotiza a mano).
    status = Column(String, nullable=False)
    # Solo NOT_QUOTABLE: MISSING_DIMENSIONS | NO_RATES | UPSTREAM_ERROR.
    reason = Column(String, nullable=True)
    # Cajas consolidadas (shipping_packing.Box.as_dict) - null si no se llego a empacar.
    packages = Column(JSONB, nullable=True)
    # [{optionId, carrier, service, serviceDescription, deliveryEstimate, carrierPrice, price, currency}]
    options = Column(JSONB, nullable=False, default=list)
    # uuids de productos sin peso/medidas (solo MISSING_DIMENSIONS).
    missing_dimensions = Column(JSONB, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
