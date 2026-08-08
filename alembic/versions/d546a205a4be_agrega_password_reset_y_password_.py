"""agrega password reset y password_changed_at

Revision ID: d546a205a4be
Revises: acebdc7e2546
Create Date: 2026-08-08 12:49:12.157367

Agrega recuperacion de password para cuentas locales: tabla password_reset_tokens
(DB-backed, no un JWT stateless como email_verify - hace falta poder marcar un token
como usado y de un solo uso, ver security.py/client_service.py) y
client_accounts.password_changed_at, usado para invalidar sesiones (JWTs) emitidas
antes del ultimo cambio de password, ya sea via este flujo o via PATCH /auth/me.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd546a205a4be'
down_revision: Union[str, Sequence[str], None] = 'acebdc7e2546'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('password_reset_tokens',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('client_account_id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['client_account_id'], ['client_accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_password_reset_tokens_client_account_id'), 'password_reset_tokens', ['client_account_id'], unique=False)
    op.create_index(op.f('ix_password_reset_tokens_id'), 'password_reset_tokens', ['id'], unique=False)
    op.create_index(op.f('ix_password_reset_tokens_token_hash'), 'password_reset_tokens', ['token_hash'], unique=True)
    op.add_column('client_accounts', sa.Column('password_changed_at', sa.DateTime(timezone=True), nullable=True))
    # Drop de ix_order_idempotency_keys_id omitido: drift preexistente no relacionado con
    # este cambio (autogenerate lo detecto de paso), fuera de alcance aqui.


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('client_accounts', 'password_changed_at')
    op.drop_index(op.f('ix_password_reset_tokens_token_hash'), table_name='password_reset_tokens')
    op.drop_index(op.f('ix_password_reset_tokens_id'), table_name='password_reset_tokens')
    op.drop_index(op.f('ix_password_reset_tokens_client_account_id'), table_name='password_reset_tokens')
    op.drop_table('password_reset_tokens')
