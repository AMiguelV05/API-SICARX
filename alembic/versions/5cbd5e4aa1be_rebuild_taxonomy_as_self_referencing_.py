"""rebuild taxonomy as self referencing category tree

Revision ID: 5cbd5e4aa1be
Revises: 01606a3b914f
Create Date: 2026-07-31 18:27:13.373131

Colapsa `departments`/`categories`/`department_category` (relacion N:M fija a
2 niveles, sincronizada desde Sicar X) en una sola tabla `categories`
auto-referenciada via `parent_uuid` (arbol de profundidad arbitraria,
administrado por humanos - ver CLAUDE.md, "Taxonomia"). No se reconstruye la
jerarquia N:M anterior: cada fila existente (categorias Y departamentos)
sobrevive como nodo raiz (`parent_uuid = NULL`), decision confirmada con el
usuario para que `GET /taxonomy` siga sirviendo datos reales de inmediato en
vez de empezar vacio. Tambien crea `product_categories` (N:M producto<->
categoria propio del PIM), que si empieza vacia - no hay endpoint admin
todavia para poblarla.
"""
import re
import unicodedata
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '5cbd5e4aa1be'
down_revision: Union[str, Sequence[str], None] = '01606a3b914f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _slugify(name: str) -> str:
    """ascii, minusculas, guiones - sin depender de un paquete externo de
    slugify. Quita acentos (comunes en nombres de categoria en espanol, p.ej.
    "Ferreteria") via unicodedata en vez de una tabla de reemplazo manual."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug or "categoria"


def _unique_slug(base_slug: str, used: set[str], disambiguator: str) -> str:
    if base_slug not in used:
        return base_slug
    candidate = f"{base_slug}-{disambiguator[:6]}"
    if candidate not in used:
        return candidate
    # Ultimo recurso, virtualmente inalcanzable con los ~cientos de filas de
    # esta tabla, pero evita un fallo duro si de verdad colisiona.
    suffix = 2
    while f"{candidate}-{suffix}" in used:
        suffix += 1
    return f"{candidate}-{suffix}"


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()

    # 1. slug nullable primero (safe-migration pattern: backfill antes de
    # exigir NOT NULL/UNIQUE) - ver supabase-postgres-best-practices,
    # schema-constraints.md.
    op.add_column('categories', sa.Column('slug', sa.String(), nullable=True))

    used_slugs: set[str] = set()
    categories = bind.execute(sa.text("SELECT uuid, name FROM categories")).fetchall()
    for row in categories:
        slug = _unique_slug(_slugify(row.name), used_slugs, row.uuid)
        used_slugs.add(slug)
        bind.execute(sa.text("UPDATE categories SET slug = :slug WHERE uuid = :uuid"), {"slug": slug, "uuid": row.uuid})

    op.alter_column('categories', 'slug', nullable=False)
    op.create_unique_constraint('ux_categories_slug', 'categories', ['slug'])

    # 2. parent_uuid - nullable, ninguna fila existente necesita backfill (todas
    # se quedan como nodos raiz, ver docstring del modulo).
    op.add_column('categories', sa.Column('parent_uuid', sa.String(), nullable=True))
    op.create_index('ix_categories_parent_uuid', 'categories', ['parent_uuid'], unique=False)
    op.create_foreign_key('fk_categories_parent_uuid', 'categories', 'categories', ['parent_uuid'], ['uuid'])

    # 3. Migra cada `department` a una fila nueva en `categories` (nodo raiz) -
    # reusa el uuid del departamento tal cual (namespaces de uuid de Sicar X
    # distintos para departamentos y categorias, no deberian colisionar; el
    # chequeo de existencia de abajo es una defensa extra, no la garantia
    # principal).
    departments = bind.execute(sa.text("SELECT uuid, name, updated_at FROM departments")).fetchall()
    for row in departments:
        already_exists = bind.execute(
            sa.text("SELECT 1 FROM categories WHERE uuid = :uuid"), {"uuid": row.uuid}
        ).first()
        if already_exists:
            continue
        slug = _unique_slug(_slugify(row.name), used_slugs, row.uuid)
        used_slugs.add(slug)
        bind.execute(
            sa.text(
                "INSERT INTO categories (uuid, name, slug, parent_uuid, updated_at) "
                "VALUES (:uuid, :name, :slug, NULL, :updated_at)"
            ),
            {"uuid": row.uuid, "name": row.name, "slug": slug, "updated_at": row.updated_at},
        )

    # 4. Tablas viejas fuera - ya no hay nada que las use (department_category
    # antes de departments por la FK).
    op.drop_table('department_category')
    op.drop_table('departments')

    # 5. Nueva tabla N:M producto<->categoria (PIM), vacia.
    op.create_table('product_categories',
        sa.Column('category_uuid', sa.String(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['category_uuid'], ['categories.uuid']),
        sa.ForeignKeyConstraint(['product_id'], ['products.id']),
        sa.PrimaryKeyConstraint('category_uuid', 'product_id'),
    )
    op.create_index('ix_product_categories_product_id', 'product_categories', ['product_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema.

    Con perdida de datos a proposito: no hay forma de saber, despues del
    hecho, cuales filas de `categories` originalmente venian de `departments`
    (se insertaron como categorias normales), ni de reconstruir el N:M
    original department_category (nunca se intento reconstruir al subir, ver
    docstring del modulo). Esto revierte el ESQUEMA (columnas/tablas nuevas
    fuera, tablas viejas de vuelta, vacias) - no restaura el contenido."""
    op.drop_index('ix_product_categories_product_id', table_name='product_categories')
    op.drop_table('product_categories')

    op.create_table('departments',
        sa.Column('uuid', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=True),
        sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('uuid'),
    )
    op.create_table('department_category',
        sa.Column('department_uuid', sa.String(), nullable=False),
        sa.Column('category_uuid', sa.String(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['category_uuid'], ['categories.uuid']),
        sa.ForeignKeyConstraint(['department_uuid'], ['departments.uuid']),
        sa.PrimaryKeyConstraint('department_uuid', 'category_uuid'),
    )

    op.drop_constraint('fk_categories_parent_uuid', 'categories', type_='foreignkey')
    op.drop_index('ix_categories_parent_uuid', table_name='categories')
    op.drop_column('categories', 'parent_uuid')
    op.drop_constraint('ux_categories_slug', 'categories', type_='unique')
    op.drop_column('categories', 'slug')
