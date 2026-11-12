"""Integracion con URREA: supplier_products, supplier_sync_status y columnas de proveedor en products

Revision ID: a7c3e9f1b5d2
Revises: f3b9d2e7a4c1
Create Date: 2026-11-12 00:00:00.000000

Ver app/models/supplier_product.py y urrea_sync_worker.py. products.supplier_available y
products.fallback_image_url son locales (fuera del upsert del sync de Sicar X). Ninguna entra
todavia en el trigger de busqueda (products_mark_search_dirty).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = 'a7c3e9f1b5d2'
down_revision: Union[str, None] = 'f3b9d2e7a4c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "supplier_products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("supplier", sa.String(length=20), nullable=False),
        sa.Column("supplier_code", sa.String(length=50), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("match_method", sa.String(length=20), nullable=False),
        sa.Column("barcode", sa.String(length=50), nullable=True),
        sa.Column("list_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("sale_multiple", sa.Integer(), nullable=True),
        sa.Column("availability_status", sa.String(length=20), nullable=True),
        sa.Column("photo_urls", postgresql.JSONB(), nullable=True),
        sa.Column("video_url", sa.String(), nullable=True),
        sa.Column("datasheet_url", sa.String(), nullable=True),
        sa.Column("manual_url", sa.String(), nullable=True),
        sa.Column("set_contents_url", sa.String(), nullable=True),
        sa.Column("info", postgresql.JSONB(), nullable=True),
        sa.Column("pim_fields_applied", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("supplier", "supplier_code", name="uq_supplier_products_code"),
        sa.UniqueConstraint("supplier", "product_id", name="uq_supplier_products_product"),
    )
    op.create_index("ix_supplier_products_product_id", "supplier_products", ["product_id"])

    op.create_table(
        "supplier_sync_status",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("supplier", sa.String(length=20), nullable=False),
        sa.Column("job", sa.String(length=40), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rows_received", sa.Integer(), nullable=True),
        sa.Column("rows_matched", sa.Integer(), nullable=True),
        sa.Column("rows_changed", sa.Integer(), nullable=True),
        sa.Column("pim_fields_written", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.UniqueConstraint("supplier", "job", name="uq_supplier_sync_status_job"),
    )

    op.add_column("products", sa.Column("supplier_available", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("products", sa.Column("fallback_image_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "fallback_image_url")
    op.drop_column("products", "supplier_available")
    op.drop_table("supplier_sync_status")
    op.drop_index("ix_supplier_products_product_id", table_name="supplier_products")
    op.drop_table("supplier_products")
