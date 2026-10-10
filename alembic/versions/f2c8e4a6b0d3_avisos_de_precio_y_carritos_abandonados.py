"""Aviso de baja de precio en favoritos y carritos abandonados

Revision ID: f2c8e4a6b0d3
Revises: e7b3c5d9f1a4
Create Date: 2026-10-10 00:00:00.000000

wishlist_items.baseline_price se rellena con el precio actual de cada producto: solo
cuentan las bajas a partir de hoy. Ver CLAUDE.md, "Correos de marketing".
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f2c8e4a6b0d3'
down_revision: Union[str, Sequence[str], None] = 'e7b3c5d9f1a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("wishlist_items", sa.Column("baseline_price", sa.Numeric(10, 2), nullable=True))
    op.add_column("wishlist_items", sa.Column("last_notified_price", sa.Numeric(10, 2), nullable=True))
    op.execute("UPDATE wishlist_items wi SET baseline_price = p.price FROM products p WHERE p.id = wi.product_id")
    op.add_column("carts", sa.Column("abandoned_notified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("carts", "abandoned_notified_at")
    op.drop_column("wishlist_items", "last_notified_price")
    op.drop_column("wishlist_items", "baseline_price")
