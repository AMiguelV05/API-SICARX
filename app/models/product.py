from sqlalchemy import Column, Integer, String, Numeric, Float, Boolean, Text, JSON, DateTime, Index
from sqlalchemy.dialects.postgresql import JSONB
from app.core.database import Base

class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        # jsonb_path_ops: solo necesitamos el operador de contencion (@>) para el filtro
        # `tag` de /catalog, y ese opclass da un indice 2-3x mas chico que el default.
        Index("ix_products_tags_gin", "tags", postgresql_using="gin", postgresql_ops={"tags": "jsonb_path_ops"}),
    )

    # ID local
    id = Column(Integer, primary_key=True, index=True)

    # Identificadores
    sicar_uuid = Column(String, unique=True, index=True, nullable=False)
    sku = Column(String, nullable=True)
    additional_skus = Column(JSON, nullable=True)  # Para guardar los SKUs adicionales que pueda tener un producto, como un array de strings

    # Datos básicos
    name = Column(String, nullable=False)
    description_details = Column(Text, nullable=True)
    image_url = Column(String, nullable=True)

    tags = Column(JSONB, nullable=True)               # Listas: ["oferta", "pretul"]; JSONB (no JSON) para poder filtrar por contencion con indice GIN
    additional_images = Column(JSON, nullable=True)  # Listado de URLs de listImages
    sales_unit_uuid = Column(String, nullable=True)  # Para saber si se vende por PZA, MTR, KGS

    # Clasificación
    department_uuid = Column(String, index=True, nullable=True)
    category_uuid = Column(String, index=True, nullable=True)

    # Precios e Inventario
    price = Column(Numeric(10, 2), nullable=False)
    stock = Column(Float, default=0.0)
    is_bulk = Column(Boolean, default=False)
    
    # Estado
    is_active = Column(Boolean, default=True) # Indica si el producto está activo en tienda en linea
    is_deleted = Column(Boolean, default=False)  # Para marcar productos que ya no existen en Sicar
    last_sync_id = Column(String, index=True, nullable=True) # Columna para detectar productos a eliminar
    details_updated_at = Column(DateTime(timezone=True), nullable=True)  # Fecha de actualización de detalles
    deleted_at = Column(DateTime(timezone=True), nullable=True)  # Fecha de eliminación del producto en Sicar
