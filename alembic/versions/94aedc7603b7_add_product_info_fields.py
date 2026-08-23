"""add_product_info_fields

Revision ID: 94aedc7603b7
Revises: f4b8c72a91d3
Create Date: 2026-08-22 18:05:13.561718

Cuatro columnas nullable en products (brand/bullet_points/technical_specs/contents) - PIM
propio, no sincronizado desde Sicar X, mismo tratamiento que attributes/variant_group_uuid.
Editables via PATCH /admin/products/{uuid}/info o la hoja "InfoProducto" de la importacion
masiva; expuestas solo en GET /products/{uuid} (detalle), no en catalogo/busqueda. Ver
CLAUDE.md.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '94aedc7603b7'
down_revision: Union[str, Sequence[str], None] = 'f4b8c72a91d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('products', sa.Column('brand', sa.String(), nullable=True))
    op.add_column('products', sa.Column('bullet_points', sa.Text(), nullable=True))
    op.add_column('products', sa.Column('technical_specs', sa.Text(), nullable=True))
    op.add_column('products', sa.Column('contents', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('products', 'contents')
    op.drop_column('products', 'technical_specs')
    op.drop_column('products', 'bullet_points')
    op.drop_column('products', 'brand')
