from sqlalchemy import Column, String, Integer, Boolean, JSON, DateTime, ForeignKey, Table, Index
from app.core.database import Base

# N:M preset<->atributo (plantilla de conveniencia, NO obligatoria - un producto puede
# tener cualquier atributo sin pertenecer a ningun preset). PK preset-primero: la consulta
# dominante es "atributos de este preset", igual que product_categories/product_vehicles.
attribute_preset_items = Table(
    "attribute_preset_items",
    Base.metadata,
    Column("preset_uuid", String, ForeignKey("attribute_presets.uuid"), primary_key=True),
    Column("attribute_uuid", String, ForeignKey("attributes.uuid"), primary_key=True),
    Column("is_required", Boolean, nullable=False, default=False),  # Solo asesorio para la UI - nunca se valida contra Product.attributes.
    Column("display_order", Integer, nullable=False, default=0),
    Index("ix_attribute_preset_items_attribute_uuid", "attribute_uuid"),
)


class Attribute(Base):
    """Catalogo de definiciones de atributos EAV. Vocabulario de `data_type` restringido en
    capa de aplicacion (Literal en schemas/attribute.py), no en la BD - mismo patron que
    Order.status/ClientAccount.auth_provider. Los VALORES reales viven en Product.attributes
    (JSONB, ver product.py), no en una tabla EAV por fila."""
    __tablename__ = "attributes"

    uuid = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    slug = Column(String, nullable=False, unique=True)
    data_type = Column(String, nullable=False)  # "TEXT" | "NUMBER" | "BOOLEAN" | "ENUM"
    allowed_values = Column(JSON, nullable=True)  # Solo para ENUM - plain JSON (no JSONB), nada consulta adentro, igual que Product.additional_skus.
    unit = Column(String, nullable=True)  # Unidad de display opcional, p. ej. "mm", "V", "L"
    updated_at = Column(DateTime(timezone=True), nullable=False)


class AttributePreset(Base):
    """Bundle nombrado y reusable de atributos (p. ej. "Llantas", "Pintura"), puramente como
    conveniencia de admin/bulk-import para aplicar varios atributos a la vez a un lote de
    productos similares. Plano, no jerarquico - a diferencia de `categories`, no representa
    una clasificacion del producto, solo un atajo de captura."""
    __tablename__ = "attribute_presets"

    uuid = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    slug = Column(String, nullable=False, unique=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class VariantGroup(Base):
    """Vinculo explicito, curado por un admin, entre distintos SKUs de Sicar X que son en
    realidad la misma pieza en variantes distintas (p. ej. color/talla) - Sicar X no tiene
    este concepto, cada variante es una fila de Product independiente con su propio
    sicar_uuid/precio/stock. `variant_attribute_slug` nombra que atributo distingue a los
    miembros (p. ej. "color") - texto libre, solo para que el frontend sepa que selector
    renderizar, no se valida contra `attributes.slug`."""
    __tablename__ = "variant_groups"

    uuid = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    variant_attribute_slug = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)
