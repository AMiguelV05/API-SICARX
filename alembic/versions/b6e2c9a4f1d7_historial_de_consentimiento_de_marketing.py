"""Historial del consentimiento de correos de marketing

Revision ID: b6e2c9a4f1d7
Revises: a3d9f7c2e5b1
Create Date: 2026-10-13 00:00:00.000000

marketing_consent_events solo crece: un trigger rechaza UPDATE, DELETE y TRUNCATE (es
evidencia legal). Las cuentas que ya tenian un estado guardado (marketing_emails_updated_at
no nulo) reciben un evento source='migrated' con ese estado y esa fecha, para que el
historial de cada cuenta empiece donde estaba el booleano; sin IP, user agent ni version
del aviso, porque no se guardaban.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b6e2c9a4f1d7'
down_revision: Union[str, Sequence[str], None] = 'a3d9f7c2e5b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "marketing_consent_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_account_id", sa.Integer(), sa.ForeignKey("client_accounts.id"), nullable=False),
        sa.Column("value", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("privacy_notice_version", sa.String(), nullable=True),
        sa.Column("ip_address", sa.String(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_marketing_consent_events_client_changed", "marketing_consent_events",
        ["client_account_id", "changed_at"],
    )
    op.execute("""
        INSERT INTO marketing_consent_events (client_account_id, value, source, changed_at)
        SELECT id, marketing_emails, 'migrated', marketing_emails_updated_at
        FROM client_accounts
        WHERE marketing_emails_updated_at IS NOT NULL
    """)
    op.execute("""
        CREATE FUNCTION marketing_consent_events_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'marketing_consent_events es un historial legal: solo admite INSERT';
        END
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER marketing_consent_events_no_update_delete
        BEFORE UPDATE OR DELETE ON marketing_consent_events
        FOR EACH ROW EXECUTE FUNCTION marketing_consent_events_append_only()
    """)
    op.execute("""
        CREATE TRIGGER marketing_consent_events_no_truncate
        BEFORE TRUNCATE ON marketing_consent_events
        FOR EACH STATEMENT EXECUTE FUNCTION marketing_consent_events_append_only()
    """)


def downgrade() -> None:
    op.drop_table("marketing_consent_events")  # se lleva sus triggers
    op.execute("DROP FUNCTION marketing_consent_events_append_only()")
