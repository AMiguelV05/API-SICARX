"""agrega tabla de admin_audit_log

Revision ID: c9e2d4a7f158
Revises: b3f7a91c4e2d
Create Date: 2026-08-18 00:05:00.000000

Registro de las mutaciones admin de mas alto valor (accept/cancel/refund de ordenes,
gestion de AdminUser, cupones, borrado de categoria/vehiculo, moderacion de resenas) -
ver audit_service.log_action y CLAUDE.md, "Admin RBAC y auditoria".
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9e2d4a7f158'
down_revision: Union[str, Sequence[str], None] = 'b3f7a91c4e2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('admin_audit_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('admin_user_id', sa.Integer(), nullable=False),
    sa.Column('action', sa.String(), nullable=False),
    sa.Column('resource_type', sa.String(), nullable=False),
    sa.Column('resource_id', sa.String(), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['admin_user_id'], ['admin_users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_admin_audit_log_id'), 'admin_audit_log', ['id'], unique=False)
    op.create_index(op.f('ix_admin_audit_log_admin_user_id'), 'admin_audit_log', ['admin_user_id'], unique=False)
    op.create_index(
        'ix_admin_audit_log_admin_user_id_created_at',
        'admin_audit_log',
        ['admin_user_id', 'created_at'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_admin_audit_log_admin_user_id_created_at', table_name='admin_audit_log')
    op.drop_index(op.f('ix_admin_audit_log_admin_user_id'), table_name='admin_audit_log')
    op.drop_index(op.f('ix_admin_audit_log_id'), table_name='admin_audit_log')
    op.drop_table('admin_audit_log')
