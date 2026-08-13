"""orders dashboard index and total_quantity precision fix

Revision ID: bdabd9ca8c46
Revises: a1c9f26db47e
Create Date: 2026-08-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bdabd9ca8c46'
down_revision: Union[str, Sequence[str], None] = 'a1c9f26db47e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # GET /admin/dashboard/summary|top-products|top-categories filtran exactamente
    # status='PAID' AND deleted_at IS NULL AND created_at BETWEEN ... - sin este indice
    # cada llamada hace un seq scan completo de orders a medida que crece.
    op.create_index(
        'ix_orders_paid_created_at',
        'orders',
        ['created_at'],
        unique=False,
        postgresql_where=sa.text("status = 'PAID' AND deleted_at IS NULL"),
    )
    # total_quantity era Numeric(10,2) mientras Product.stock/reserved/sales_count (que
    # tambien representan cantidades de productos a granel) son Numeric(12,3) - una linea
    # a granel con 3 decimales (p. ej. 1.005 kg) se redondeaba silenciosamente al sumarse
    # aqui, desalineando el total de la orden contra sus propias lineas en items.
    op.alter_column(
        'orders', 'total_quantity',
        existing_type=sa.Numeric(10, 2),
        type_=sa.Numeric(12, 3),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        'orders', 'total_quantity',
        existing_type=sa.Numeric(12, 3),
        type_=sa.Numeric(10, 2),
        existing_nullable=False,
    )
    op.drop_index('ix_orders_paid_created_at', table_name='orders')
