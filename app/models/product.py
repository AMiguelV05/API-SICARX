from decimal import Decimal
from sqlalchemy import Column, Integer, String, Numeric, Boolean, Text, JSON, DateTime, Index
from sqlalchemy.dialects.postgresql import JSONB
from app.core.database import Base

class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        # jsonb_path_ops: solo se necesita el operador de contencion (@>), da un indice 2-3x mas chico que el default.
        Index("ix_products_tags_gin", "tags", postgresql_using="gin", postgresql_ops={"tags": "jsonb_path_ops"}),
        # Declarados aqui (no solo en la migracion que los creo) para que autogenerate no proponga su drop como falso positivo - ver 806cd48b3b2a.
        Index("ix_products_sku_trgm", "sku", postgresql_using="gin", postgresql_ops={"sku": "gin_trgm_ops"}),
        Index("ix_products_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}),
    )

    id = Column(Integer, primary_key=True, index=True)

    sicar_uuid = Column(String, unique=True, index=True, nullable=False)
    sku = Column(String, nullable=True)
    additional_skus = Column(JSON, nullable=True)

    name = Column(String, nullable=False)
    description_details = Column(Text, nullable=True)
    image_url = Column(String, nullable=True)

    tags = Column(JSONB, nullable=True)  # JSONB (no JSON): permite filtrar por contencion con indice GIN
    additional_images = Column(JSON, nullable=True)  # Listado de URLs de listImages
    sales_unit_uuid = Column(String, nullable=True)  # Para saber si se vende por PZA, MTR, KGS
    unit_short_name = Column(String, nullable=True)  # Nombre corto resuelto (p.ej. "PZA"/"MTR") via content.units - ver fetch_full_details_from_sicar

    department_uuid = Column(String, index=True, nullable=True)
    category_uuid = Column(String, index=True, nullable=True)

    price = Column(Numeric(10, 2), nullable=False)
    # Numeric (no Float): evita error de representacion binaria en la aritmetica de stock. 3 decimales para productos por peso (is_bulk).
    stock = Column(Numeric(12, 3), default=Decimal("0"))
    is_bulk = Column(Boolean, default=False)

    is_active = Column(Boolean, default=True)
    is_deleted = Column(Boolean, default=False)  # Para marcar productos que ya no existen en Sicar
    last_sync_id = Column(String, index=True, nullable=True) # Columna para detectar productos a eliminar
    details_updated_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class SyncStatus(Base):
    """Fila unica (id=1) con el estado de la corrida mas reciente de sync_task.py - antes
    no habia forma de consultar el ultimo sync exitoso sin leer sync.log directamente."""
    __tablename__ = "sync_status"

    id = Column(Integer, primary_key=True)
    last_run_started_at = Column(DateTime(timezone=True), nullable=True)
    last_run_finished_at = Column(DateTime(timezone=True), nullable=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    products_processed = Column(Integer, nullable=True)
    products_deactivated = Column(Integer, nullable=True)
    last_error = Column(String, nullable=True)
