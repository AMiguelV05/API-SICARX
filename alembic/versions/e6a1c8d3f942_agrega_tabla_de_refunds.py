"""agrega tabla de refunds

Revision ID: e6a1c8d3f942
Revises: c9e2d4a7f158
Create Date: 2026-08-18 00:10:00.000000

Reembolsos (parciales o totales) sobre una orden pagada - una orden puede tener varias
filas; el total reembolsado se deriva con SUM(amount), no hay columna de acumulado
aparte. issued_by_admin_id es NULL para el reembolso automatico de una cancelacion
iniciada por el cliente/invitado (POST /orders/{id}/cancel). Ver CLAUDE.md, "Reembolsos
parciales".
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e6a1c8d3f942'
down_revision: Union[str, Sequence[str], None] = 'c9e2d4a7f158'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('refunds',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('reason', sa.String(), nullable=False),
    sa.Column('mp_refund_id', sa.String(), nullable=True),
    sa.Column('issued_by_admin_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ),
    sa.ForeignKeyConstraint(['issued_by_admin_id'], ['admin_users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_refunds_id'), 'refunds', ['id'], unique=False)
    op.create_index(op.f('ix_refunds_order_id'), 'refunds', ['order_id'], unique=False)
    op.create_index('ix_refunds_order_id_created_at', 'refunds', ['order_id', 'created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_refunds_order_id_created_at', table_name='refunds')
    op.drop_index(op.f('ix_refunds_order_id'), table_name='refunds')
    op.drop_index(op.f('ix_refunds_id'), table_name='refunds')
    op.drop_table('refunds')
