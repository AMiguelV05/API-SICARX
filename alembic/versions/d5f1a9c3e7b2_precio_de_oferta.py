"""Precio de oferta: products.compare_at_price/sale_ends_at

Revision ID: d5f1a9c3e7b2
Revises: c4e8a2f6b1d9
Create Date: 2026-10-10 00:00:00.000000

Ver CLAUDE.md, "Precio de oferta". Columnas PIM propias (capturadas en el admin, nunca
sincronizadas desde Sicar X). Ademas reemplaza products_mark_search_dirty para que capturar
o quitar una oferta reindexe el producto en Typesense (campo sale_until, filtro onSale) -
mismo cuerpo que en c4e8a2f6b1d9 mas las dos columnas nuevas.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5f1a9c3e7b2'
down_revision: Union[str, Sequence[str], None] = 'c4e8a2f6b1d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TRIGGER_FN = """
    CREATE OR REPLACE FUNCTION products_mark_search_dirty() RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'INSERT'
           OR NEW.sku IS DISTINCT FROM OLD.sku
           OR NEW.name IS DISTINCT FROM OLD.name
           OR NEW.additional_skus::text IS DISTINCT FROM OLD.additional_skus::text
           OR NEW.brand IS DISTINCT FROM OLD.brand
           OR NEW.description IS DISTINCT FROM OLD.description
           OR NEW.price IS DISTINCT FROM OLD.price
           OR NEW.is_active IS DISTINCT FROM OLD.is_active
           OR NEW.is_deleted IS DISTINCT FROM OLD.is_deleted
           OR NEW.department_uuid IS DISTINCT FROM OLD.department_uuid
           OR NEW.category_uuid IS DISTINCT FROM OLD.category_uuid
           OR NEW.sales_count IS DISTINCT FROM OLD.sales_count
           OR (GREATEST(NEW.stock - NEW.reserved, 0) > 0)
              IS DISTINCT FROM (GREATEST(OLD.stock - OLD.reserved, 0) > 0)
           {extra}
        THEN
            NEW.search_dirty_at := now();
        END IF;
        RETURN NEW;
    END
    $$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.add_column("products", sa.Column("compare_at_price", sa.Numeric(10, 2), nullable=True))
    op.add_column("products", sa.Column("sale_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_products_on_sale", "products", ["sale_ends_at"],
        postgresql_where=sa.text("compare_at_price IS NOT NULL AND is_deleted = false AND is_active = true"),
    )
    op.execute(_TRIGGER_FN.format(extra="""
           OR NEW.compare_at_price IS DISTINCT FROM OLD.compare_at_price
           OR NEW.sale_ends_at IS DISTINCT FROM OLD.sale_ends_at"""))


def downgrade() -> None:
    op.execute(_TRIGGER_FN.format(extra=""))
    op.drop_index("ix_products_on_sale", table_name="products")
    op.drop_column("products", "sale_ends_at")
    op.drop_column("products", "compare_at_price")
