"""Foto de respaldo del proveedor en el trigger de busqueda

Revision ID: c1e5b8d3f9a2
Revises: a7c3e9f1b5d2
Create Date: 2026-11-12 00:00:00.000000

Reemplaza products_mark_search_dirty: mismo cuerpo que en a3d9f7c2e5b1, pero "tiene foto" ahora
es coalesce(image_url no vacia, fallback_image_url) (Product.effective_image_url). Sin subir
SCHEMA_VERSION: fallback_image_url nace en NULL (a7c3e9f1b5d2), asi que los documentos actuales
ya son correctos, y el trigger marca para reindexar solo los productos que ganan o pierden foto
cuando el worker de URREA la escribe.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'c1e5b8d3f9a2'
down_revision: Union[str, None] = 'a7c3e9f1b5d2'
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
           {photo}
        THEN
            NEW.search_dirty_at := now();
        END IF;
        RETURN NEW;
    END
    $$ LANGUAGE plpgsql
"""

_PHOTO_SICAR = """
           OR (NEW.image_url IS NOT NULL AND NEW.image_url <> '')
              IS DISTINCT FROM (OLD.image_url IS NOT NULL AND OLD.image_url <> '')"""

_PHOTO_EFFECTIVE = """
           OR (COALESCE(NULLIF(NEW.image_url, ''), NEW.fallback_image_url) IS NOT NULL)
              IS DISTINCT FROM (COALESCE(NULLIF(OLD.image_url, ''), OLD.fallback_image_url) IS NOT NULL)"""


def upgrade() -> None:
    op.execute(_TRIGGER_FN.format(photo=_PHOTO_EFFECTIVE))


def downgrade() -> None:
    op.execute(_TRIGGER_FN.format(photo=_PHOTO_SICAR))
