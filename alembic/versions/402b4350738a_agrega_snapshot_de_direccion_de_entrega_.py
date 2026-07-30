"""agrega snapshot de direccion de entrega y guia de envio a orders

Revision ID: 402b4350738a
Revises: d258e6f72a3a
Create Date: 2026-07-29 16:35:09.421089

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '402b4350738a'
down_revision: Union[str, Sequence[str], None] = 'd258e6f72a3a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('orders', sa.Column('delivery_address_snapshot', sa.JSON(), nullable=True))
    op.add_column('orders', sa.Column('shipping_label', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('orders', 'shipping_label')
    op.drop_column('orders', 'delivery_address_snapshot')
