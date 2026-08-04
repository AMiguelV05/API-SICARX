import uuid
from sqlalchemy import Column, Integer, String, JSON, DateTime, ForeignKey, func
from app.core.database import Base

class Cart(Base):
    __tablename__ = "carts"

    id = Column(Integer, primary_key=True, index=True)
    # Doble uso: identificador publico Y token portador de acceso anonimo (carrito sin datos sensibles).
    uuid = Column(String, unique=True, index=True, nullable=False, default=lambda: str(uuid.uuid4()))
    # Nullable+unique: Postgres permite varios NULL, asi se limita a un carrito por cuenta sin bloquear anonimos.
    client_account_id = Column(
        Integer, ForeignKey("client_accounts.id", ondelete="CASCADE"), nullable=True, unique=True, index=True
    )
    # Solo referencias [{"uuid","quantity"}], nunca snapshot de precio/nombre; JSON simple, nada consulta dentro.
    items = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=func.now())
