"""Busqueda ignora guiones en sku/name ("wd40" encuentra "WD-40")

Revision ID: b8d2f5a1c7e3
Revises: a7c3e1f9d2b4
Create Date: 2026-09-24 00:00:00.000000

/search comparaba contra immutable_unaccent(sku/name) tal cual, asi que "wd40" no matcheaba
"WD-40" (el guion rompe la subcadena). search_normalize() = immutable_unaccent() + quitar
"-", aplicado de ambos lados (columna y termino buscado) en catalog_service.search_products
- el guion se trata como "nulo". El caso "wd 40" ya funcionaba por el AND entre palabras
("wd" y "40" aparecen ambas en "WD-40").

Los indices trgm sobre immutable_unaccent() de f1a3c7e9b2d4 quedan superados por estos -
search_products era su unico consumidor y la nueva expresion no los puede usar, mismo
razonamiento que f1a3c7e9b2d4 para los trgm planos. immutable_unaccent() se conserva
(search_normalize() la usa).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8d2f5a1c7e3'
down_revision: Union[str, Sequence[str], None] = 'a7c3e1f9d2b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "CREATE OR REPLACE FUNCTION search_normalize(text) RETURNS text AS $$ "
        "SELECT replace(immutable_unaccent($1), '-', '') "
        "$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT"
    )

    op.execute("DROP INDEX IF EXISTS ix_products_sku_trgm_unaccent")
    op.execute("DROP INDEX IF EXISTS ix_products_name_trgm_unaccent")

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_products_sku_trgm_search "
        "ON products USING gin (search_normalize(sku) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_products_name_trgm_search "
        "ON products USING gin (search_normalize(name) gin_trgm_ops)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS ix_products_name_trgm_search")
    op.execute("DROP INDEX IF EXISTS ix_products_sku_trgm_search")

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_products_sku_trgm_unaccent "
        "ON products USING gin (immutable_unaccent(sku) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_products_name_trgm_unaccent "
        "ON products USING gin (immutable_unaccent(name) gin_trgm_ops)"
    )

    op.execute("DROP FUNCTION IF EXISTS search_normalize(text)")
