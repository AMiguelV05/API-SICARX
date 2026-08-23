"""rename_description_details_to_description

Revision ID: 4b1f8274a2dd
Revises: 94aedc7603b7
Create Date: 2026-08-22 19:00:00.000000

`description_details` used to be synced from Sicar X's GraphQL `details` field (lazy
refresh in GET /products/{uuid}); it's now admin-owned, same treatment as
brand/bulletPoints/technicalSpecs/contents (see "Info de producto propia" in CLAUDE.md).
Renamed to `description` and wiped (drop+add, not a data-preserving rename) - confirmed
with the user during brainstorming that existing Sicar-sourced text should not carry over.
Both DROP COLUMN and ADD COLUMN ... NULL are metadata-only in Postgres, no table rewrite
regardless of row count.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4b1f8274a2dd'
down_revision: Union[str, Sequence[str], None] = '94aedc7603b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('products', 'description_details')
    op.add_column('products', sa.Column('description', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('products', 'description')
    op.add_column('products', sa.Column('description_details', sa.Text(), nullable=True))
