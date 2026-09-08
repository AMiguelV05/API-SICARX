"""Busqueda insensible a acentos (unaccent) en sku/name

Revision ID: f1a3c7e9b2d4
Revises: d4e8b1c6f295
Create Date: 2026-09-08 00:00:00.000000

/search hacia ILIKE directo contra sku/name, asi que "cafe" y "café" (o "camion"/"camión")
no matcheaban entre si - un problema real para un catalogo en espanol. La extension
unaccent() de Postgres resuelve esto, pero es STABLE (no IMMUTABLE, depende de
search_path/config de texto), asi que no se puede usar directo dentro de un indice
funcional - de ahi el wrapper immutable_unaccent() de abajo, el patron estandar
recomendado por la propia documentacion de Postgres para este caso. Los indices trgm
planos (ix_products_sku_trgm/ix_products_name_trgm, ver 224799e4444b/806cd48b3b2a) quedan
superados por estos - nada mas los usa (ver CLAUDE.md), y mantener ambos pares duplicaria
el costo de storage/escritura en products (~124k filas) sin beneficio: la query nueva de
catalog_service.search_products ya no puede usar el indice viejo (no matchea la expresion
unaccented).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a3c7e9b2d4'
down_revision: Union[str, Sequence[str], None] = 'd4e8b1c6f295'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute(
        "CREATE OR REPLACE FUNCTION immutable_unaccent(text) RETURNS text AS $$ "
        "SELECT unaccent('unaccent', $1) "
        "$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT"
    )

    op.execute("DROP INDEX IF EXISTS ix_products_sku_trgm")
    op.execute("DROP INDEX IF EXISTS ix_products_name_trgm")

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_products_sku_trgm_unaccent "
        "ON products USING gin (immutable_unaccent(sku) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_products_name_trgm_unaccent "
        "ON products USING gin (immutable_unaccent(name) gin_trgm_ops)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS ix_products_name_trgm_unaccent")
    op.execute("DROP INDEX IF EXISTS ix_products_sku_trgm_unaccent")

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_products_sku_trgm "
        "ON products USING gin (sku gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_products_name_trgm "
        "ON products USING gin (name gin_trgm_ops)"
    )

    op.execute("DROP FUNCTION IF EXISTS immutable_unaccent(text)")
