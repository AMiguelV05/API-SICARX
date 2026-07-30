"""add target_dispatch_status to sicar_sync_outbox

Revision ID: 01606a3b914f
Revises: 402b4350738a
Create Date: 2026-07-30 09:40:44.623532

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '01606a3b914f'
down_revision: Union[str, Sequence[str], None] = '402b4350738a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ix_order_idempotency_keys_id fue excluido a proposito: es drift preexistente sin
    # relacion con este cambio (autogenerate lo detecto de forma independiente), no algo
    # que esta migracion deba tocar.
    op.add_column('sicar_sync_outbox', sa.Column('target_dispatch_status', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('sicar_sync_outbox', 'target_dispatch_status')
