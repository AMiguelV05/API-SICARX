from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Table, Index
from app.core.database import Base

# N:M producto<->vehiculo (compatibilidad, PIM propio) - mismo patron que product_categories; empieza vacia.
product_vehicles = Table(
    "product_vehicles",
    Base.metadata,
    Column("vehicle_uuid", String, ForeignKey("vehicles.uuid"), primary_key=True),
    Column("product_id", Integer, ForeignKey("products.id"), primary_key=True),
    Index("ix_product_vehicles_product_id", "product_id"),
)

class Vehicle(Base):
    """Fitment plano (make/model/year-range/engine), no arbol como Category. Administrado
    por humanos via /v1/admin/vehicles/*, sin sincronizacion automatica."""
    __tablename__ = "vehicles"
    __table_args__ = (
        Index("ix_vehicles_type_make_model", "vehicle_type", "make", "model"),
    )

    uuid = Column(String, primary_key=True)
    vehicle_type = Column(String, nullable=False)  # "AUTOMOTIVE" | "MOTORCYCLE"
    make = Column(String, nullable=False)
    model = Column(String, nullable=False)
    year_start = Column(Integer, nullable=False)
    year_end = Column(Integer, nullable=True)  # None = todavia en produccion
    engine = Column(String, nullable=True)  # texto libre, p. ej. "L4 1.6L"
    updated_at = Column(DateTime(timezone=True), nullable=False)
