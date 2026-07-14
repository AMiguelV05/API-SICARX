"""Agrega busqueda por trigram en sku y name

Revision ID: 224799e4444b
Revises: efb3b56c3a03
Create Date: 2026-07-13 18:39:39.917190

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '224799e4444b'
down_revision: Union[str, Sequence[str], None] = 'efb3b56c3a03'
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
