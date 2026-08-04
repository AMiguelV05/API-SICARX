from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Table, Index
from sqlalchemy.orm import relationship
from app.core.database import Base

# N:M producto<->categoria (PIM). PK category-primero: la consulta dominante es "productos de esta categoria".
product_categories = Table(
    "product_categories",
    Base.metadata,
    Column("category_uuid", String, ForeignKey("categories.uuid"), primary_key=True),
    Column("product_id", Integer, ForeignKey("products.id"), primary_key=True),
    Index("ix_product_categories_product_id", "product_id"),
)

class Category(Base):
    """Arbol de categorias (PIM propio), auto-referenciado via parent_uuid, profundidad arbitraria -
    ya no sincronizado desde Sicar X, administrado por humanos."""
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
