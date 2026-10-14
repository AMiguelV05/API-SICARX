"""Reindexa en Typesense cuando un producto gana o pierde su foto

Revision ID: a3d9f7c2e5b1
Revises: f2c8e4a6b0d3
Create Date: 2026-10-13 00:00:00.000000

El orden por defecto de /search pone los productos con foto antes que los que no tienen
(campo has_image del indice, SCHEMA_VERSION 4). Reemplaza products_mark_search_dirty: mismo
cuerpo que en d5f1a9c3e7b2 mas el cruce de "tiene foto" - solo el cruce, no cualquier cambio
de URL, igual que la disponibilidad.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'a3d9f7c2e5b1'
down_revision: Union[str, Sequence[str], None] = 'f2c8e4a6b0d3'
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
           OR NEW.compare_at_price IS DISTINCT FROM OLD.compare_at_price
           OR NEW.sale_ends_at IS DISTINCT FROM OLD.sale_ends_at
           {extra}
        THEN
            NEW.search_dirty_at := now();
        END IF;
        RETURN NEW;
    END
    $$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.execute(_TRIGGER_FN.format(extra="""
           OR (NEW.image_url IS NOT NULL AND NEW.image_url <> '')
              IS DISTINCT FROM (OLD.image_url IS NOT NULL AND OLD.image_url <> '')"""))


def downgrade() -> None:
    op.execute(_TRIGGER_FN.format(extra=""))
