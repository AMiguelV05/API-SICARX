"""add guest checkout support

Revision ID: a1c9f26db47e
Revises: e4586463dc71
Create Date: 2026-08-11 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c9f26db47e'
down_revision: Union[str, Sequence[str], None] = 'e4586463dc71'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('orders', 'client_account_id', existing_type=sa.Integer(), nullable=True)
    op.add_column('orders', sa.Column('guest_email', sa.String(), nullable=True))
    op.create_index('ix_orders_guest_email', 'orders', ['guest_email'])
    # Garantiza que toda orden tenga alguna identidad (cuenta o invitado) - protege contra un bug futuro que deje ambas en NULL.
    op.create_check_constraint(
        'ck_orders_has_identity', 'orders', 'client_account_id IS NOT NULL OR guest_email IS NOT NULL',
    )

    op.alter_column('order_idempotency_keys', 'client_account_id', existing_type=sa.Integer(), nullable=True)
    op.add_column('order_idempotency_keys', sa.Column('guest_email', sa.String(), nullable=True))
    # Indice unico parcial para el camino de invitado: la restriccion existente
    # (client_account_id, idempotency_key) nunca detecta colisiones para invitados, ya que
    # Postgres trata NULL != NULL en un unique constraint - con client_account_id siempre
    # NULL para ordenes de invitado, cada fila pasaria como unica sin este indice aparte.
    op.create_index(
        'ux_order_idempotency_guest_key', 'order_idempotency_keys', ['guest_email', 'idempotency_key'],
        unique=True, postgresql_where=sa.text('client_account_id IS NULL'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ux_order_idempotency_guest_key', table_name='order_idempotency_keys')
    op.drop_column('order_idempotency_keys', 'guest_email')
    op.alter_column('order_idempotency_keys', 'client_account_id', existing_type=sa.Integer(), nullable=False)

    op.drop_constraint('ck_orders_has_identity', 'orders', type_='check')
    op.drop_index('ix_orders_guest_email', table_name='orders')
    op.drop_column('orders', 'guest_email')
    op.alter_column('orders', 'client_account_id', existing_type=sa.Integer(), nullable=False)
