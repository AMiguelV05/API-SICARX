"""drop dead columns sicar_document_uuid sicar_date target_dispatch_status dispatch_history

Revision ID: 56842c6e8af0
Revises: 5cbd5e4aa1be
Create Date: 2026-07-31 18:46:32.353914

Drops four columns confirmed to have zero readers or writers anywhere in the
codebase (verified by grep across app/, not just migrations) - leftovers from
the SICAR-inventory-only redesign (orders.sicar_document_uuid, orders.sicar_date,
sicar_sync_outbox.target_dispatch_status) and a field that was exposed in
schemas but never actually populated by any code path (orders.dispatch_history).
See CLAUDE.md.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '56842c6e8af0'
down_revision: Union[str, Sequence[str], None] = '5cbd5e4aa1be'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('orders', 'sicar_document_uuid')
    op.drop_column('orders', 'dispatch_history')
    op.drop_column('orders', 'sicar_date')
    op.drop_column('sicar_sync_outbox', 'target_dispatch_status')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('sicar_sync_outbox', sa.Column('target_dispatch_status', sa.VARCHAR(), autoincrement=False, nullable=True))
    op.add_column('orders', sa.Column('sicar_date', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True))
    op.add_column('orders', sa.Column('dispatch_history', postgresql.JSON(astext_type=sa.Text()), autoincrement=False, nullable=True))
    op.add_column('orders', sa.Column('sicar_document_uuid', sa.VARCHAR(), autoincrement=False, nullable=True))
