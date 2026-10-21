"""Quita indices sobrantes de products

Revision ID: c1f4a8e2d6b9
Revises: b6e2c9a4f1d7
Create Date: 2026-10-21 00:00:00.000000

Medido en produccion (pg_stat_user_indexes, contadores desde 2026-09-09):
- ix_products_id (6.5 MB): duplica products_pkey (index=True sobre la llave primaria).
- ix_products_last_sync_id (6.5 MB, 1 scan): la unica consulta que usa la columna es
  `last_sync_id IS NOT NULL`, que casi todas las filas cumplen; el planner no lo usa.
- ix_products_department_uuid (4.3 MB, 1 scan) e ix_products_category_uuid (7.2 MB, 3 scans):
  el storefront no filtra por estos campos crudos de Sicar X (usa taxonomyUuid). /products y
  /search los siguen aceptando; sin indice ese filtro recorre la tabla, aceptable para un
  filtro que nadie usa.

CONCURRENTLY dentro de autocommit_block: un DROP INDEX normal toma ACCESS EXCLUSIVE sobre
products y, si espera detras de una transaccion larga del worker, bloquea tambien todas las
lecturas del storefront mientras tanto.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'c1f4a8e2d6b9'
down_revision: Union[str, Sequence[str], None] = 'b6e2c9a4f1d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEXES = {
    "ix_products_id": "id",
    "ix_products_last_sync_id": "last_sync_id",
    "ix_products_department_uuid": "department_uuid",
    "ix_products_category_uuid": "category_uuid",
}


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, column in _INDEXES.items():
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON products ({column})")
