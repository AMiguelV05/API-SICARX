"""agrega tabla de chargebacks

Revision ID: 7a9c1e5f2b83
Revises: 4b1f8274a2dd
Create Date: 2026-08-31 00:00:00.000000

Contracargos ("Compra no reconocida") sobre una orden ya PAID - una fila por evento,
mismo criterio que refunds. mp_chargeback_id es NULL hasta que llega la notificacion
enriquecida del topic "chargebacks" de Mercado Pago. orders.disputed_at es un marcador
historico (nunca se limpia) de que la orden tuvo al menos un contracargo. Ver
CLAUDE.md, "Contracargos de Mercado Pago".
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a9c1e5f2b83'
down_revision: Union[str, Sequence[str], None] = '4b1f8274a2dd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('orders', sa.Column('disputed_at', sa.DateTime(timezone=True), nullable=True))

    op.create_table('chargebacks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=False),
    sa.Column('mp_chargeback_id', sa.String(), nullable=True),
    sa.Column('mp_payment_id', sa.String(), nullable=True),
    sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('reason', sa.String(), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('coverage_eligible', sa.Boolean(), nullable=True),
    sa.Column('documentation_required', sa.Boolean(), nullable=True),
    sa.Column('documentation_deadline', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_chargebacks_id'), 'chargebacks', ['id'], unique=False)
    op.create_index(op.f('ix_chargebacks_order_id'), 'chargebacks', ['order_id'], unique=False)
    op.create_index(op.f('ix_chargebacks_mp_chargeback_id'), 'chargebacks', ['mp_chargeback_id'], unique=True)
    op.create_index(op.f('ix_chargebacks_mp_payment_id'), 'chargebacks', ['mp_payment_id'], unique=False)
    op.create_index('ix_chargebacks_order_id_created_at', 'chargebacks', ['order_id', 'created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_chargebacks_order_id_created_at', table_name='chargebacks')
    op.drop_index(op.f('ix_chargebacks_mp_payment_id'), table_name='chargebacks')
    op.drop_index(op.f('ix_chargebacks_mp_chargeback_id'), table_name='chargebacks')
    op.drop_index(op.f('ix_chargebacks_order_id'), table_name='chargebacks')
    op.drop_index(op.f('ix_chargebacks_id'), table_name='chargebacks')
    op.drop_table('chargebacks')

    op.drop_column('orders', 'disputed_at')
