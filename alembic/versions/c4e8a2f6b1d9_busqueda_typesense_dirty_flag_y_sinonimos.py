"""Busqueda con Typesense: products.search_dirty_at + triggers, y tabla search_synonyms

Revision ID: c4e8a2f6b1d9
Revises: b8d2f5a1c7e3
Create Date: 2026-09-26 00:00:00.000000

Ver CLAUDE.md, "Busqueda con Typesense", y docs/superpowers/specs/2026-09-26-typesense-search-design.md.

1. products.search_dirty_at: "pendiente de reindexar en Typesense". La ponen en now()
   triggers de Postgres, no el codigo de la app, para cubrir a los ~30 escritores actuales de
   datos de producto (sync_task, brand_service, bulk_import_service, taxonomy_service,
   vehicle_service, apply_*_deltas...) y a cualquiera futuro sin tocarlos.
   - products_mark_search_dirty (BEFORE INSERT OR UPDATE, por fila): solo marca si cambio
     de verdad (IS DISTINCT FROM) una columna que el indice usa. last_sync_id queda fuera a
     proposito - el upsert de sync_task.py reescribe las ~124k filas cada 5 minutos, y asi
     solo se marcan las que cambiaron. Del stock solo importa si cruza 0 (disponible <->
     agotado); una reserva/reabasto normal no toca el indice.
   - mark_products_dirty_from_link (AFTER INSERT/DELETE, por sentencia, con tablas de
     transicion) sobre product_categories y product_vehicles: una sola UPDATE por
     importacion masiva o PUT de reemplazo, no una por fila. Tablas de transicion exigen un
     trigger por evento, de ahi 4 triggers.
2. search_synonyms: fuente de verdad de los sinonimos del buscador (admin), sembrada con
   las 4 entradas de la spec, ya normalizadas como las guarda synonym_service.
"""
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c4e8a2f6b1d9'
down_revision: Union[str, Sequence[str], None] = 'b8d2f5a1c7e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Ya normalizadas (minusculas, sin acentos, unidades pegadas, singular) - la forma en que
# Typesense compara. "baterias" se colapsa en "bateria" al normalizar.
SEED_SYNONYMS = [
    (None, ["cuadro", "entrada", "mando"]),
    (None, ["allen", "hexagonal"]),
    ("inalambrico", ["bateria", "20v", "12v"]),
    (None, ["desarmador", "destornillador"]),
]


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("products", sa.Column("search_dirty_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_products_search_dirty", "products", ["search_dirty_at"],
        postgresql_where=sa.text("search_dirty_at IS NOT NULL"),
    )

    # additional_skus es JSON (no JSONB) - json no tiene operador de igualdad, de ahi el ::text.
    # En INSERT, OLD es NULL (Postgres >= 11), asi que referenciarlo no falla.
    op.execute("""
        CREATE OR REPLACE FUNCTION products_mark_search_dirty() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT'
               OR NEW.sku IS DISTINCT FROM OLD.sku
               OR NEW.name IS DISTINCT FROM OLD.name
               OR NEW.additional_skus::text IS DISTINCT FROM OLD.additional_skus::text
               OR NEW.brand IS DISTINCT FROM OLD.brand
               OR NEW.description IS DISTINCT FROM OLD.description
               OR NEW.price IS DISTINCT FROM OLD.price
               OR NEW.is_active IS DISTINCT FROM OLD.is_active
               OR NEW.is_deleted IS DISTINCT FROM OLD.is_deleted
               OR NEW.department_uuid IS DISTINCT FROM OLD.department_uuid
               OR NEW.category_uuid IS DISTINCT FROM OLD.category_uuid
               OR NEW.sales_count IS DISTINCT FROM OLD.sales_count
               OR (GREATEST(NEW.stock - NEW.reserved, 0) > 0)
                  IS DISTINCT FROM (GREATEST(OLD.stock - OLD.reserved, 0) > 0)
            THEN
                NEW.search_dirty_at := now();
            END IF;
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_products_search_dirty
        BEFORE INSERT OR UPDATE ON products
        FOR EACH ROW EXECUTE FUNCTION products_mark_search_dirty()
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION mark_products_dirty_from_link() RETURNS trigger AS $$
        BEGIN
            UPDATE products SET search_dirty_at = now()
            WHERE id IN (SELECT DISTINCT product_id FROM changed);
            RETURN NULL;
        END
        $$ LANGUAGE plpgsql
    """)
    for table in ("product_categories", "product_vehicles"):
        op.execute(f"""
            CREATE TRIGGER trg_{table}_search_dirty_ins
            AFTER INSERT ON {table}
            REFERENCING NEW TABLE AS changed
            FOR EACH STATEMENT EXECUTE FUNCTION mark_products_dirty_from_link()
        """)
        op.execute(f"""
            CREATE TRIGGER trg_{table}_search_dirty_del
            AFTER DELETE ON {table}
            REFERENCING OLD TABLE AS changed
            FOR EACH STATEMENT EXECUTE FUNCTION mark_products_dirty_from_link()
        """)

    search_synonyms = op.create_table(
        "search_synonyms",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uuid", sa.String(), nullable=False),
        sa.Column("root", sa.String(), nullable=True),
        sa.Column("synonyms", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by_admin_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["updated_by_admin_id"], ["admin_users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_search_synonyms_id"), "search_synonyms", ["id"], unique=False)
    op.create_index(op.f("ix_search_synonyms_uuid"), "search_synonyms", ["uuid"], unique=True)
    op.create_index("ix_search_synonyms_updated_at", "search_synonyms", ["updated_at"], unique=False)
    op.create_index(
        op.f("ix_search_synonyms_updated_by_admin_id"), "search_synonyms", ["updated_by_admin_id"], unique=False
    )

    op.bulk_insert(
        search_synonyms,
        [{"uuid": str(uuid.uuid4()), "root": root, "synonyms": words} for root, words in SEED_SYNONYMS],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_search_synonyms_updated_by_admin_id"), table_name="search_synonyms")
    op.drop_index("ix_search_synonyms_updated_at", table_name="search_synonyms")
    op.drop_index(op.f("ix_search_synonyms_uuid"), table_name="search_synonyms")
    op.drop_index(op.f("ix_search_synonyms_id"), table_name="search_synonyms")
    op.drop_table("search_synonyms")

    for table in ("product_categories", "product_vehicles"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_search_dirty_del ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_search_dirty_ins ON {table}")
    op.execute("DROP FUNCTION IF EXISTS mark_products_dirty_from_link()")
    op.execute("DROP TRIGGER IF EXISTS trg_products_search_dirty ON products")
    op.execute("DROP FUNCTION IF EXISTS products_mark_search_dirty()")

    op.drop_index("ix_products_search_dirty", table_name="products")
    op.drop_column("products", "search_dirty_at")
