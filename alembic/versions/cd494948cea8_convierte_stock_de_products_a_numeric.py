"""convierte stock de products a numeric

Revision ID: cd494948cea8
Revises: 806cd48b3b2a
Create Date: 2026-07-27 12:38:57.696438

products.stock era Float; se convierte a Numeric(12, 3) por la misma razon que price
se convirtio en 069dd8677786 - evitar el error de representacion binaria de float en
la aritmetica de stock (product_stock_service.apply_stock_deltas). 3 decimales para
productos vendidos por peso (Product.is_bulk).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cd494948cea8'
down_revision: Union[str, Sequence[str], None] = '806cd48b3b2a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        'products', 'stock',
        existing_type=sa.Float(),
        type_=sa.Numeric(precision=12, scale=3),
        existing_nullable=True,
        postgresql_using='stock::numeric(12,3)',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        'products', 'stock',
        existing_type=sa.Numeric(precision=12, scale=3),
        type_=sa.Float(),
        existing_nullable=True,
        postgresql_using='stock::double precision',
    )
