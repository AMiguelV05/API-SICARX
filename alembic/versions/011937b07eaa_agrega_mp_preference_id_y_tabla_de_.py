"""agrega mp_preference_id y tabla de idempotencia de ordenes

Revision ID: 011937b07eaa
Revises: cd494948cea8
Create Date: 2026-07-27 12:40:25.953512

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '011937b07eaa'
down_revision: Union[str, Sequence[str], None] = 'cd494948cea8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('orders', sa.Column('mp_preference_id', sa.String(), nullable=True))

    op.create_table(
        'order_idempotency_keys',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_account_id', sa.Integer(), nullable=False),
        sa.Column('idempotency_key', sa.String(), nullable=False),
        sa.Column('order_uuid', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['client_account_id'], ['client_accounts.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('client_account_id', 'idempotency_key', name='ux_order_idempotency_client_key'),
    )
    op.create_index(op.f('ix_order_idempotency_keys_client_account_id'), 'order_idempotency_keys', ['client_account_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_order_idempotency_keys_client_account_id'), table_name='order_idempotency_keys')
    op.drop_table('order_idempotency_keys')
    op.drop_column('orders', 'mp_preference_id')
