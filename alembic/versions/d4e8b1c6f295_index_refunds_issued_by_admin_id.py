"""indice en refunds.issued_by_admin_id

Revision ID: d4e8b1c6f295
Revises: b2f6a4d19e37
Create Date: 2026-09-07 00:00:00.000000

Unica FK de todo el esquema que se habia quedado sin indexar - el resto (
admin_audit_log.admin_user_id, coupon_redemptions.client_account_id,
product_reviews.client_account_id, etc.) ya lo tenia. Tabla chica hoy, pero es exactamente
el join que un futuro reporte de actividad admin ("cuanto ha reembolsado cada admin")
necesitaria.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd4e8b1c6f295'
down_revision: Union[str, Sequence[str], None] = 'b2f6a4d19e37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(op.f('ix_refunds_issued_by_admin_id'), 'refunds', ['issued_by_admin_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_refunds_issued_by_admin_id'), table_name='refunds')
