"""agrega_indice_funcional_lower_brand

Revision ID: 8803c1101b10
Revises: f1a3c7e9b2d4
Create Date: 2026-09-22 17:50:52.937211

Product.brand (agregado en 94aedc7603b7) estaba hasta ahora restringido a solo detalle de
producto (GET /products/{uuid}), sin indice. Se agrega el filtro `brand` a POST /products y
POST /search, mas GET /products/brands (listado de marcas distintas para armar un picklist en
el frontend) - ver CLAUDE.md. El match es case-insensitive exacto (lower(brand) = lower(:value))
porque brand se escribe hoy como texto libre sin normalizacion (PATCH
/admin/products/{uuid}/info, hoja "InfoProducto" del bulk-import) - filtrar por lower() en vez
de normalizar en escritura evita romper valores ya guardados. Btree simple sobre lower(brand),
no GIN/trigram - es match exacto, no busqueda difusa como sku/name. Parcial sobre el mismo
predicado is_deleted/is_active que ya usan ix_products_available_stock/ix_products_sales_count,
ya que get_local_catalog/search_products siempre filtran por esos dos campos.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8803c1101b10'
down_revision: Union[str, Sequence[str], None] = 'f1a3c7e9b2d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_products_brand_lower ON products (lower(brand)) "
        "WHERE is_deleted = false AND is_active = true"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS ix_products_brand_lower")
