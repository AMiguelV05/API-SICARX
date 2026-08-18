"""agrega indice para barrido de ordenes abandonadas

Revision ID: 7f2c9a1e4d33
Revises: a5f3d8c19e42
Create Date: 2026-08-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7f2c9a1e4d33'
down_revision: Union[str, Sequence[str], None] = 'a5f3d8c19e42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Respalda abandoned_order_worker.py, que busca exactamente esta combinacion cada
    # 5 minutos para cancelar ordenes TO_PAY sin ningun intento de pago - sin este indice
    # cada corrida hace un seq scan completo de orders a medida que crece.
    op.create_index(
        'ix_orders_to_pay_abandoned_created_at',
        'orders',
        ['created_at'],
        unique=False,
        postgresql_where=sa.text("status = 'TO_PAY' AND deleted_at IS NULL AND mp_payment_id IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_orders_to_pay_abandoned_created_at', table_name='orders')
