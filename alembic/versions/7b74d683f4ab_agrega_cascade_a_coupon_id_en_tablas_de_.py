"""agrega cascade a coupon_id en tablas de alcance de cupones

Revision ID: 7b74d683f4ab
Revises: 102f9fd949c4
Create Date: 2026-08-10 16:23:06.565149

Bug encontrado en vivo probando GET .../categories|products|clients (lectura del alcance
de un cupon, agregada como seguimiento a 102f9fd949c4): borrar un Coupon que todavia tenia
categorias/productos/clientes asignados fallaba con un IntegrityError crudo (500) en vez de
completarse - las FK de coupon_id en coupon_categories/coupon_products/coupon_assigned_clients
no tenian ON DELETE CASCADE. A diferencia de product_categories/product_vehicles (donde
ambos lados son entidades independientes y borrar la categoria/vehiculo se bloquea a
proposito si tiene productos asignados), estas filas de alcance/elegibilidad no tienen
sentido sin el cupon - cascada aqui es lo correcto, no un bloqueo. El lado
category_uuid/product_id/client_account_id se deja sin cascade a proposito - ver la
migracion hermana que agrega el chequeo de dependientes en taxonomy_service.delete_category.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7b74d683f4ab'
down_revision: Union[str, Sequence[str], None] = '102f9fd949c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CASCADED_FKS = [
    ("coupon_categories_coupon_id_fkey", "coupon_categories", "coupon_id"),
    ("coupon_products_coupon_id_fkey", "coupon_products", "coupon_id"),
    ("coupon_assigned_clients_coupon_id_fkey", "coupon_assigned_clients", "coupon_id"),
]


def upgrade() -> None:
    """Upgrade schema."""
    for constraint_name, table_name, column_name in _CASCADED_FKS:
        op.drop_constraint(constraint_name, table_name, type_="foreignkey")
        op.create_foreign_key(constraint_name, table_name, "coupons", [column_name], ["id"], ondelete="CASCADE")


def downgrade() -> None:
    """Downgrade schema."""
    for constraint_name, table_name, column_name in _CASCADED_FKS:
        op.drop_constraint(constraint_name, table_name, type_="foreignkey")
        op.create_foreign_key(constraint_name, table_name, "coupons", [column_name], ["id"])
