import uuid
from sqlalchemy import Column, Integer, String, JSON, DateTime, ForeignKey, func
from app.core.database import Base

class Cart(Base):
    __tablename__ = "carts"

    id = Column(Integer, primary_key=True, index=True)
    # Doble uso: identificador publico Y token portador para acceso anonimo (un carrito no
    # contiene datos sensibles, mismo modelo de confianza que ClientAddress.uuid/Order.uuid).
    uuid = Column(String, unique=True, index=True, nullable=False, default=lambda: str(uuid.uuid4()))
    # Nullable + unique: Postgres permite multiples NULL bajo un constraint unique, asi que esto
    # garantiza "maximo un carrito por cuenta" permitiendo a la vez muchos carritos anonimos.
    client_account_id = Column(
        Integer, ForeignKey("client_accounts.id", ondelete="CASCADE"), nullable=True, unique=True, index=True
    )
    # Solo referencias: [{"uuid": str, "quantity": float}, ...] -- nunca snapshot de precio/nombre.
    # JSON simple (no JSONB): nada consulta dentro de esto, mismo razonamiento que Order.items.
    items = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=func.now())
