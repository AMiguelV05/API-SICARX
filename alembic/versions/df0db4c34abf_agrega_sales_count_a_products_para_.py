"""agrega sales_count a products para registro de mas vendidos

Revision ID: df0db4c34abf
Revises: 418f7e68c49e
Create Date: 2026-08-05 16:59:27.161986

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'df0db4c34abf'
down_revision: Union[str, Sequence[str], None] = '418f7e68c49e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nota: autogenerate tambien detecto un 'ix_order_idempotency_keys_id' faltante -
    # drift preexistente sin relacion con este cambio, omitido a proposito de esta migracion.
    op.add_column('products', sa.Column('sales_count', sa.Numeric(precision=12, scale=3), server_default='0', nullable=False))
    op.create_index('ix_products_sales_count', 'products', ['sales_count'], unique=False, postgresql_where=sa.text('is_deleted = false AND is_active = true'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_products_sales_count', table_name='products', postgresql_where=sa.text('is_deleted = false AND is_active = true'))
    op.drop_column('products', 'sales_count')
