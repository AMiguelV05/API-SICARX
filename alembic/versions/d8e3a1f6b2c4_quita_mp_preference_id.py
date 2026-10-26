"""Quita orders.mp_preference_id

Revision ID: d8e3a1f6b2c4
Revises: c1f4a8e2d6b9
Create Date: 2026-10-25 00:00:00.000000

La preferencia de Checkout Pro solo existia para ofrecer Wallet ("Cuenta de Mercado Pago") en
el Payment Brick. Con la migracion a la Orders API se quito Wallet y ya no se crean
preferencias; la columna no tiene escritores ni lectores.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'd8e3a1f6b2c4'
down_revision: Union[str, None] = 'c1f4a8e2d6b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('orders', 'mp_preference_id')


def downgrade() -> None:
    op.add_column('orders', sa.Column('mp_preference_id', sa.String(), nullable=True))
