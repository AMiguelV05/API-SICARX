"""Elimina indices redundantes de sku y name

Revision ID: 6854715fa27b
Revises: 224799e4444b
Create Date: 2026-07-14 11:56:58.145404

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6854715fa27b'
down_revision: Union[str, Sequence[str], None] = '224799e4444b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index("ix_products_sku", table_name="products")
    op.drop_index("ix_products_name", table_name="products")


def downgrade() -> None:
    """Downgrade schema."""
    op.create_index("ix_products_name", "products", ["name"])
    op.create_index("ix_products_sku", "products", ["sku"])
