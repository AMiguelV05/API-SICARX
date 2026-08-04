"""agrega unit_short_name a products

Revision ID: 9442999722db
Revises: 52ccc4e9556e
Create Date: 2026-08-04 09:26:54.747172

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9442999722db'
down_revision: Union[str, Sequence[str], None] = '52ccc4e9556e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ix_order_idempotency_keys_id omitido a proposito: ya cubierto por la PK.
    op.add_column('products', sa.Column('unit_short_name', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('products', 'unit_short_name')
