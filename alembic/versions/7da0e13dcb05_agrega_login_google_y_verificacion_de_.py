"""agrega login google y verificacion de correo

Revision ID: 7da0e13dcb05
Revises: 011937b07eaa
Create Date: 2026-07-27 16:09:46.791809

Agrega soporte para cuentas via Google (auth_provider/google_sub, hashed_password ahora
nullable) y verificacion de correo "suave" (is_verified/email_verified_at - no bloquea
nada hoy, solo se expone en ClientPublic). Cuentas existentes se backfillean como
auth_provider='local'/is_verified=true (via server_default transitorio, mismo patron que
56c152338087 uso para client_addresses.uuid) - no tiene sentido marcar como "sin verificar"
a clientes que ya venian operando normalmente antes de que esta columna existiera.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7da0e13dcb05'
down_revision: Union[str, Sequence[str], None] = '011937b07eaa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('client_accounts', sa.Column('auth_provider', sa.String(), nullable=False, server_default=sa.text("'local'")))
    op.alter_column('client_accounts', 'auth_provider', server_default=None)

    op.add_column('client_accounts', sa.Column('google_sub', sa.String(), nullable=True))
    op.create_index(op.f('ix_client_accounts_google_sub'), 'client_accounts', ['google_sub'], unique=True)

    op.add_column('client_accounts', sa.Column('is_verified', sa.Boolean(), nullable=False, server_default=sa.text('true')))
    op.alter_column('client_accounts', 'is_verified', server_default=None)

    op.add_column('client_accounts', sa.Column('email_verified_at', sa.DateTime(timezone=True), nullable=True))

    op.alter_column('client_accounts', 'hashed_password', existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    # Falla si hay cuentas Google (hashed_password NULL) - esperado, downgrade best-effort.
    op.alter_column('client_accounts', 'hashed_password', existing_type=sa.String(), nullable=False)

    op.drop_column('client_accounts', 'email_verified_at')
    op.drop_column('client_accounts', 'is_verified')
    op.drop_index(op.f('ix_client_accounts_google_sub'), table_name='client_accounts')
    op.drop_column('client_accounts', 'google_sub')
    op.drop_column('client_accounts', 'auth_provider')
