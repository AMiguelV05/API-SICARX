"""add cancellation_reason to orders

Revision ID: acebdc7e2546
Revises: 3a208e2812e1
Create Date: 2026-08-07 13:31:37.419380

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'acebdc7e2546'
down_revision: Union[str, Sequence[str], None] = '3a208e2812e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('orders', sa.Column('cancellation_reason', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('orders', 'cancellation_reason')
