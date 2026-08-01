from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Table, Index
from sqlalchemy.orm import relationship
from app.core.database import Base

# Tabla N:M producto<->categoria (PIM propio, ver CLAUDE.md "Taxonomia"). PK
# compuesta category_uuid-primero porque la consulta dominante es "productos de
# esta categoria (y sus descendientes)", no "categorias de este producto" - esa
# segunda direccion la cubre el indice ix_product_categories_product_id.
# Empieza vacia: no hay migracion que la puebla, ni endpoint admin todavia para
# llenarla (fuera de alcance de este plan).
product_categories = Table(
    "product_categories",
    Base.metadata,
    Column("category_uuid", String, ForeignKey("categories.uuid"), primary_key=True),
    Column("product_id", Integer, ForeignKey("products.id"), primary_key=True),
    Index("ix_product_categories_product_id", "product_id"),
)

class Category(Base):
    """Arbol de categorias propio (PIM), auto-referenciado via parent_uuid -
    profundidad arbitraria, no los 2 niveles fijos que impone Sicar X (ver
    CLAUDE.md, "Taxonomia"). `uuid` sigue siendo el identificador (String),
    mismo patron que Order.uuid/ClientAddress.uuid - las filas ya sincronizadas
    desde Sicar X conservan su uuid real; los nodos nuevos que cree un admin
    (trabajo futuro, fuera de alcance) usaran un uuid4() generado localmente,
    igual que Order.sicar_order_id despues del cambio a "Sicar solo ERP de
    inventario".

    Antes existia una tabla `departments` separada, relacionada N:M con
    `categories` via `department_category` - eliminadas ambas: un
    "departamento" ahora es simplemente una categoria con `parent_uuid = NULL`.
    Las filas que antes eran `Department` se migraron aqui como nodos raiz
    (ver la migracion que introduce estas columnas).

    Ya NO se sincroniza automaticamente desde Sicar X (a diferencia de antes) -
    es una tabla administrada por humanos; dejar corriendo un job automatico
    aqui arriesgaria sobreescribir `parent_uuid`/`slug` editados a mano."""
    __tablename__ = "categories"
    __table_args__ = (
        Index("ix_categories_parent_uuid", "parent_uuid"),
    )

    uuid = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    slug = Column(String, nullable=False, unique=True)
    parent_uuid = Column(String, ForeignKey("categories.uuid"), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    parent = relationship("Category", remote_side=[uuid], back_populates="children")
    children = relationship("Category", back_populates="parent")
