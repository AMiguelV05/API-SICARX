"""agrega indice de expresion para available_stock (greatest(stock-reserved,0))

Revision ID: 3a208e2812e1
Revises: 380517fdd109
Create Date: 2026-08-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3a208e2812e1'
down_revision: Union[str, Sequence[str], None] = '380517fdd109'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # La expresion tiene que coincidir EXACTAMENTE con Product.available_stock.expression
    # (GREATEST(stock - reserved, 0)) para que el planner la use en los filtros in_stock de
    # catalog_service.py (Product.available_stock > 0) - un indice sobre (stock - reserved)
    # sin el GREATEST no la cubre, Postgres no razona la equivalencia algebraica.
    op.execute(
        "CREATE INDEX ix_products_available_stock ON products (GREATEST(stock - reserved, 0)) "
        "WHERE is_deleted = false AND is_active = true"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX ix_products_available_stock")
