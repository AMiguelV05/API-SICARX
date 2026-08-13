"""add orders.coupon_id and coupon_categories.category_uuid indexes

Revision ID: 4f35b7649d5b
Revises: bdabd9ca8c46
Create Date: 2026-08-13 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4f35b7649d5b'
down_revision: Union[str, Sequence[str], None] = 'bdabd9ca8c46'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # orders.coupon_id es FK sin indice, a diferencia de client_account_id en la misma
    # tabla - inconsistencia menor, sin impacto real hoy (nada la consulta directamente
    # todavia, ver CouponRedemption.coupon_id que si esta indexado), pero se agrega para
    # consistencia con la convencion de este codebase de indexar toda FK.
    op.create_index('ix_orders_coupon_id', 'orders', ['coupon_id'], unique=False)
    # category_uuid es la columna NO lider de la PK compuesta de coupon_categories - sin
    # este indice, taxonomy_service.delete_category (filtra solo por category_uuid, sin
    # coupon_id) seq-scanea esta tabla en cada DELETE /admin/categories/{uuid}.
    op.create_index('ix_coupon_categories_category_uuid', 'coupon_categories', ['category_uuid'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_coupon_categories_category_uuid', table_name='coupon_categories')
    op.drop_index('ix_orders_coupon_id', table_name='orders')
