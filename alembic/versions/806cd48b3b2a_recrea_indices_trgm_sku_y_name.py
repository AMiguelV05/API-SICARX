"""recrea indices trgm sku y name

Revision ID: 806cd48b3b2a
Revises: bda8078263da
Create Date: 2026-07-27 12:34:51.375330

Recrea ix_products_sku_trgm / ix_products_name_trgm, eliminados por error en
bda8078263da (falso positivo de autogenerate que, a diferencia de las otras 6
migraciones que tocaron products antes, no se recorto manualmente). Desde
bda8078263da, sku/name en products no tienen ningun indice: /search hace un
sequential scan completo sobre ~124k filas. Ver CLAUDE.md y Product.__table_args__
(app/models/product.py) - ambos indices ahora tambien se declaran en el modelo
para que autogenerate deje de proponer este mismo drop en el futuro.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '806cd48b3b2a'
down_revision: Union[str, Sequence[str], None] = 'bda8078263da'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_products_sku_trgm "
        "ON products USING gin (sku gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_products_name_trgm "
        "ON products USING gin (name gin_trgm_ops)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS ix_products_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_products_sku_trgm")
