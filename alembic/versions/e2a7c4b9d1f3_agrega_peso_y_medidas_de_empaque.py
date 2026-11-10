"""Peso y medidas de empaque: products.package_weight_kg/length_cm/width_cm/height_cm

Revision ID: e2a7c4b9d1f3
Revises: d8e3a1f6b2c4
Create Date: 2026-11-10 00:00:00.000000

Columnas PIM propias (nunca sincronizadas desde Sicar X) para cotizar envios con datos
reales por producto en vez de medidas capturadas a mano por pedido. Nullable y sin default:
agregarlas no reescribe la tabla. No se tocan los triggers de busqueda (no se indexan).

Los CHECK se validan en la misma transaccion (las columnas recien creadas estan en NULL, el
escaneo de ~110k filas tarda menos de un segundo); separarlos en NOT VALID + VALIDATE no
ayudaria, el ADD COLUMN ya toma ACCESS EXCLUSIVE hasta el commit.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'e2a7c4b9d1f3'
down_revision: Union[str, None] = 'd8e3a1f6b2c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CHECKS = {
    "ck_products_package_dims_all_or_none":
        "num_nonnulls(package_weight_kg, package_length_cm, package_width_cm, package_height_cm) IN (0, 4)",
    "ck_products_package_dims_positive":
        "package_weight_kg > 0 AND package_length_cm > 0 AND package_width_cm > 0 AND package_height_cm > 0",
}


def upgrade() -> None:
    op.add_column("products", sa.Column("package_weight_kg", sa.Numeric(8, 3), nullable=True))
    op.add_column("products", sa.Column("package_length_cm", sa.Numeric(7, 2), nullable=True))
    op.add_column("products", sa.Column("package_width_cm", sa.Numeric(7, 2), nullable=True))
    op.add_column("products", sa.Column("package_height_cm", sa.Numeric(7, 2), nullable=True))
    for name, condition in _CHECKS.items():
        op.create_check_constraint(name, "products", condition)


def downgrade() -> None:
    for name in _CHECKS:
        op.drop_constraint(name, "products", type_="check")
    op.drop_column("products", "package_height_cm")
    op.drop_column("products", "package_width_cm")
    op.drop_column("products", "package_length_cm")
    op.drop_column("products", "package_weight_kg")
