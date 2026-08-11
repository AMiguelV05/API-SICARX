"""add shipping label cancellation audit columns to orders

Revision ID: e4586463dc71
Revises: 7b74d683f4ab
Create Date: 2026-08-11 09:21:14.316429

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4586463dc71'
down_revision: Union[str, Sequence[str], None] = '7b74d683f4ab'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nota: autogenerate tambien detecto un indice faltante preexistente y no relacionado
    # (ix_order_idempotency_keys_id) - se omite deliberadamente de esta migracion, que solo
    # cubre las columnas de auditoria de shipping/cancel.
    op.add_column('orders', sa.Column('shipping_cancellation_reason', sa.Text(), nullable=True))
    op.add_column('orders', sa.Column('shipping_label_cancelled_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('orders', 'shipping_label_cancelled_at')
    op.drop_column('orders', 'shipping_cancellation_reason')
