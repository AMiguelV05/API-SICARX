"""Notificaciones: webhook_outbox, stock_alerts y consentimiento de marketing

Revision ID: e7b3c5d9f1a4
Revises: d5f1a9c3e7b2
Create Date: 2026-10-10 00:00:00.000000

Ver CLAUDE.md, "Notificaciones salientes con outbox", "Avisos de reabastecimiento" y
"Correos de marketing". client_accounts.marketing_emails entra en false para TODAS las
cuentas existentes (nunca lo aceptaron).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7b3c5d9f1a4'
down_revision: Union[str, Sequence[str], None] = 'd5f1a9c3e7b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "webhook_outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uuid", sa.String(), nullable=False),
        sa.Column("event", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uuid"),
    )
    op.create_index(
        "ix_webhook_outbox_claimable", "webhook_outbox", ["next_attempt_at"],
        postgresql_where=sa.text("status IN ('PENDING', 'IN_PROGRESS')"),
    )

    op.create_table(
        "stock_alerts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uuid", sa.String(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("client_account_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["client_account_id"], ["client_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uuid"),
    )
    op.create_index("ix_stock_alerts_email", "stock_alerts", ["email"])
    op.create_index("ix_stock_alerts_client_account_id", "stock_alerts", ["client_account_id"])
    active = sa.text("notified_at IS NULL AND unsubscribed_at IS NULL")
    op.create_index("ux_stock_alerts_active_product_email", "stock_alerts", ["product_id", "email"], unique=True, postgresql_where=active)
    op.create_index("ix_stock_alerts_active_product", "stock_alerts", ["product_id"], postgresql_where=active)

    op.add_column("client_accounts", sa.Column("marketing_emails", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("client_accounts", sa.Column("marketing_emails_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("client_accounts", sa.Column("last_price_drop_email_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("client_accounts", "last_price_drop_email_at")
    op.drop_column("client_accounts", "marketing_emails_updated_at")
    op.drop_column("client_accounts", "marketing_emails")
    op.drop_index("ix_stock_alerts_active_product", table_name="stock_alerts")
    op.drop_index("ux_stock_alerts_active_product_email", table_name="stock_alerts")
    op.drop_index("ix_stock_alerts_client_account_id", table_name="stock_alerts")
    op.drop_index("ix_stock_alerts_email", table_name="stock_alerts")
    op.drop_table("stock_alerts")
    op.drop_index("ix_webhook_outbox_claimable", table_name="webhook_outbox")
    op.drop_table("webhook_outbox")
