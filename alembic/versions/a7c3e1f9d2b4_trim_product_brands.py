"""trim_product_brands

Revision ID: a7c3e1f9d2b4
Revises: 8803c1101b10
Create Date: 2026-09-23 12:00:00.000000

Solo datos, sin cambio de esquema. Product.brand se escribia como texto libre sin
normalizar; desde ahora toda escritura pasa por brand_service.normalize_brand (trim, "" ->
null). Esta migracion aplica lo mismo una vez a los valores ya guardados, para que agrupar/
matchear por lower(brand) (GET /admin/brands, rename, filtro publico `brand`) no deje
"Surtek" y "Surtek " como marcas distintas. Downgrade no-op: el valor original sin recortar
no se puede ni se necesita recuperar.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a7c3e1f9d2b4'
down_revision: Union[str, None] = '8803c1101b10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE products SET brand = NULLIF(btrim(brand), '') "
        "WHERE brand IS DISTINCT FROM NULLIF(btrim(brand), '')"
    )


def downgrade() -> None:
    pass
