"""Cotizacion de envio en el checkout: tabla shipping_quotes y orders.shipping_quote

Revision ID: f3b9d2e7a4c1
Revises: e2a7c4b9d1f3
Create Date: 2026-11-10 00:00:00.000000

Ver shipping_quote_service.py. orders.delivery_cost ya existia (placeholder sin uso); desde
aqui guarda el costo de envio cobrado, incluido en orders.total.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = 'f3b9d2e7a4c1'
down_revision: Union[str, None] = 'e2a7c4b9d1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shipping_quotes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", sa.String(), nullable=False, unique=True),
        sa.Column("client_account_id", sa.Integer(), sa.ForeignKey("client_accounts.id", ondelete="CASCADE"), nullable=True),
        sa.Column("items_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("destination_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("packages", postgresql.JSONB(), nullable=True),
        sa.Column("options", postgresql.JSONB(), nullable=False),
        sa.Column("missing_dimensions", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_shipping_quotes_lookup", "shipping_quotes", ["items_fingerprint", "destination_key", "created_at"])
    op.create_index("ix_shipping_quotes_created_at", "shipping_quotes", ["created_at"])
    op.add_column("orders", sa.Column("shipping_quote", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "shipping_quote")
    op.drop_index("ix_shipping_quotes_created_at", table_name="shipping_quotes")
    op.drop_index("ix_shipping_quotes_lookup", table_name="shipping_quotes")
    op.drop_table("shipping_quotes")
