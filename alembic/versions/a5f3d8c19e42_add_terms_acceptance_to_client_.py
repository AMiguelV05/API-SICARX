"""add terms acceptance to client_accounts and orders

Revision ID: a5f3d8c19e42
Revises: 4f35b7649d5b
Create Date: 2026-08-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a5f3d8c19e42'
down_revision: Union[str, Sequence[str], None] = '4f35b7649d5b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('client_accounts', sa.Column('terms_accepted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('client_accounts', sa.Column('terms_accepted_version', sa.String(), nullable=True))
    op.add_column('orders', sa.Column('terms_accepted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('orders', sa.Column('terms_accepted_version', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('orders', 'terms_accepted_version')
    op.drop_column('orders', 'terms_accepted_at')
    op.drop_column('client_accounts', 'terms_accepted_version')
    op.drop_column('client_accounts', 'terms_accepted_at')
