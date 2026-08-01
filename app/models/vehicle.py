from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Table, Index
from app.core.database import Base

# Tabla N:M producto<->vehiculo (compatibilidad de vehiculos, PIM propio - ver CLAUDE.md
# "Compatibilidad de vehiculos"). Mismo patron que product_categories (app/models/taxonomy.py):
# empieza vacia, no hay migracion que la puebla con asignaciones reales - el script de
# import_gonher_vehicles.py solo siembra `vehicles`, nunca esta tabla.
product_vehicles = Table(
    "product_vehicles",
    Base.metadata,
    Column("vehicle_uuid", String, ForeignKey("vehicles.uuid"), primary_key=True),
    Column("product_id", Integer, ForeignKey("products.id"), primary_key=True),
    Index("ix_product_vehicles_product_id", "product_id"),
)

class Vehicle(Base):
    """Una fila = una combinacion make/model/year-range/engine ("fitment"), no un arbol -
    a diferencia de Category (auto-referenciada), aqui make/model/year/engine es plano,
    sin jerarquia real que representar (ver CLAUDE.md, "Compatibilidad de vehiculos").
    `uuid` generado localmente (uuid4()), mismo patron que Order.uuid/Category.uuid.

    Administrada por humanos via /v1/admin/vehicles/* - no hay sincronizacion automatica
    con ninguna fuente externa. Puede sembrarse desde un catalogo de referencia externo
    (ver import_gonher_vehicles.py) pero eso es un import manual de una sola vez, no un
    job recurrente."""
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
