import uuid
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, ForeignKey, Index, func, text
from sqlalchemy.orm import relationship
from app.core.database import Base

class ClientAccount(Base):
    __tablename__ = "client_accounts"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String, unique=True, index=True, nullable=False, default=lambda: str(uuid.uuid4()))

    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    phone = Column(String, nullable=True)
    # Nullable: cuentas via Google no tienen password local.
    hashed_password = Column(String, nullable=True)

    # "local" | "google" - string plano, vocabulario restringido en la capa de aplicacion (igual que Order.status).
    auth_provider = Column(String, nullable=False, default="local")
    # sub de Google, solo poblado para auth_provider="google"; unico por cuenta.
    google_sub = Column(String, unique=True, index=True, nullable=True)

    # Verificacion "suave": no bloquea login/checkout, solo se expone en ClientPublic (p. ej. banner). Google entra ya verificado.
    is_verified = Column(Boolean, nullable=False, default=False)
    email_verified_at = Column(DateTime(timezone=True), nullable=True)

    is_active = Column(Boolean, default=True)
    # Momento del ultimo cambio de password (via reset o PATCH /auth/me). NULL = nunca ha
    # cambiado su password. Usado por _resolve_client_from_token para invalidar sesiones
    # (JWTs) emitidas antes de este momento - ver security.py.
    password_changed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=func.now())

    # lazy="select" a proposito (no todas las rutas necesitan direcciones); donde si, se cargan con awaitable_attrs.addresses.
    addresses = relationship(
        "ClientAddress", back_populates="client_account", cascade="all, delete-orphan"
    )
    # Sin cascade de borrado (a diferencia de addresses): una orden es registro financiero, debe sobrevivir.
    orders = relationship("Order", back_populates="client_account")

class ClientAddress(Base):
    __tablename__ = "client_addresses"
    __table_args__ = (
        # Garantiza a nivel de BD un maximo de una direccion default por cliente (evita condiciones de carrera).
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

    label = Column(String, nullable=True)
    street = Column(String, nullable=False)
    ext_number = Column(String, nullable=True)
    int_number = Column(String, nullable=True)
    neighborhood = Column(String, nullable=True)
    city = Column(String, nullable=True)
    county = Column(String, nullable=True)  # Municipio - distinto de city, lo exige el "county" de Sicar X en deliveryInfo.contactInfo.address
    state = Column(String, nullable=True)
    country = Column(String, nullable=True)
    zip_code = Column(String, nullable=True)
    references = Column(String, nullable=True)
    # Coordenadas resueltas por el frontend (geocoder externo) - este backend nunca geocodifica.
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    is_default = Column(Boolean, default=False)

    client_account = relationship("ClientAccount", back_populates="addresses")
