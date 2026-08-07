"""agrega reserved a products para separar reserva local de stock real de sicar

Revision ID: 380517fdd109
Revises: df0db4c34abf
Create Date: 2026-08-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '380517fdd109'
down_revision: Union[str, Sequence[str], None] = 'df0db4c34abf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('products', sa.Column('reserved', sa.Numeric(precision=12, scale=3), server_default='0', nullable=False))
    # No restringe reserved <= stock: puede ser transitoriamente cierto y legitimo (stock
    # real de Sicar X bajo por una venta en tienda mientras habia unidades reservadas
    # localmente) - ver Product.available_stock. Solo evita que reserved se vaya negativo
    # por un bug de doble liberacion.
    op.create_check_constraint('ck_products_reserved_non_negative', 'products', 'reserved >= 0')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('ck_products_reserved_non_negative', 'products', type_='check')
    op.drop_column('products', 'reserved')
