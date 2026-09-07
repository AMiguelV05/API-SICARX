"""completed_uuids en outbox + items a jsonb

Revision ID: b2f6a4d19e37
Revises: 7a9c1e5f2b83
Create Date: 2026-09-07 00:00:00.000000

Dos cambios independientes, agrupados en una sola migracion porque ambos salen de la misma
pasada de revision:

1. `sicar_sync_outbox.completed_uuids` (JSON, default '[]') - progreso parcial de un intento
   de sincronizacion con Sicar X. Antes, un reintento tras fallo parcial (item 3 de 5 fallo)
   reprocesaba TODOS los items de la orden, incluidos los que ya se habian aplicado con
   exito contra el stock real de Sicar X - ver sicar_stock_service.apply_order_stock_delta y
   sicar_sync_worker.py::_process_claimed_row. Ahora cada intento persiste que uuids ya se
   aplicaron, y un reintento los salta.

2. `orders.items`: JSON -> JSONB. dashboard_service.get_top_products/get_top_categories ya
   agregan sobre esta columna via `jsonb_array_elements(o.items::jsonb)` - la columna JSON
   original obligaba a Postgres a re-parsear el texto de cada fila a jsonb en cada llamada,
   sin ninguna ventaja a cambio (nada mas en el codebase hace containment/indexado sobre
   esta columna). `delivery_info` se queda JSON a proposito - nada consulta dentro de el.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b2f6a4d19e37'
down_revision: Union[str, Sequence[str], None] = '7a9c1e5f2b83'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'sicar_sync_outbox',
        sa.Column('completed_uuids', sa.JSON(), nullable=False, server_default='[]'),
    )

    op.alter_column(
        'orders', 'items',
        existing_type=sa.JSON(),
        type_=postgresql.JSONB(astext_type=sa.Text()),
        postgresql_using='items::jsonb',
        existing_nullable=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        'orders', 'items',
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        type_=sa.JSON(),
        postgresql_using='items::json',
        existing_nullable=False,
    )

    op.drop_column('sicar_sync_outbox', 'completed_uuids')
