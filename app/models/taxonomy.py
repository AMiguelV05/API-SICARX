from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Table
from sqlalchemy.orm import relationship
from app.core.database import Base

# Tabla de asociación N:M — un departamento tiene una "selection" de categorías,
# y una misma categoría puede aparecer bajo varios departamentos en Sicar X.
department_category = Table(
    "department_category",
    Base.metadata,
    Column("department_uuid", String, ForeignKey("departments.uuid"), primary_key=True),
    Column("category_uuid", String, ForeignKey("categories.uuid"), primary_key=True),
    Column("sort_order", Integer, default=0),
)

class Department(Base):
    __tablename__ = "departments"

    uuid = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    sort_order = Column(Integer, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    categories = relationship(
        "Category",
        secondary=department_category,
        back_populates="departments",
        order_by=department_category.c.sort_order,
    )

class Category(Base):
    __tablename__ = "categories"

    uuid = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    departments = relationship(
        "Department",
        secondary=department_category,
        back_populates="categories",
    )
