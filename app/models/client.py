import uuid
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Index, func, text
from sqlalchemy.orm import relationship
from app.core.database import Base

class ClientAccount(Base):
    __tablename__ = "client_accounts"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String, unique=True, index=True, nullable=False, default=lambda: str(uuid.uuid4()))

    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    phone = Column(String, nullable=True)
    hashed_password = Column(String, nullable=False)

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=func.now())

    # lazy="select" (default) a proposito: no todas las rutas que resuelven un ClientAccount
    # necesitan las direcciones (p. ej. POST/PATCH/DELETE de una direccion individual). Donde
    # sí se necesitan (respuestas ClientPublic), se cargan explícitamente con
    # `await client.awaitable_attrs.addresses` (AsyncAttrs, ver app/core/database.py).
    addresses = relationship(
        "ClientAddress", back_populates="client_account", cascade="all, delete-orphan"
    )
    # Sin cascade de borrado (a diferencia de addresses): las ordenes son registros
    # financieros que deben sobrevivir aunque la cuenta cambie.
    orders = relationship("Order", back_populates="client_account")

class ClientAddress(Base):
    __tablename__ = "client_addresses"
    __table_args__ = (
        # Garantiza a nivel de BD que como maximo una direccion por cliente sea la default,
        # en vez de confiar solo en la logica de la app (evita condiciones de carrera).
        Index(
            "ix_client_addresses_one_default",
            "client_account_id",
            unique=True,
            postgresql_where=text("is_default = true"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String, unique=True, index=True, nullable=False, default=lambda: str(uuid.uuid4()))
    client_account_id = Column(
        Integer, ForeignKey("client_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )

    label = Column(String, nullable=True)  # "Casa", "Oficina", etc.
    street = Column(String, nullable=False)
    ext_number = Column(String, nullable=True)
    int_number = Column(String, nullable=True)
    neighborhood = Column(String, nullable=True)
    city = Column(String, nullable=True)
    county = Column(String, nullable=True)  # Municipio - distinto de city, lo exige el "county" de Sicar X en deliveryInfo.contactInfo.address
    state = Column(String, nullable=True)
    country = Column(String, nullable=True)
    zip_code = Column(String, nullable=True)
    references = Column(String, nullable=True)  # Referencias para ubicar el domicilio
    is_default = Column(Boolean, default=False)

    client_account = relationship("ClientAccount", back_populates="addresses")
