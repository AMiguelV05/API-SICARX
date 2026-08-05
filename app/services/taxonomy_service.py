import logging
import re
import unicodedata
import uuid as uuid_lib
from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import select, text, func, delete, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.taxonomy import Category, product_categories
from app.models.product import Product

logger = logging.getLogger(__name__)


class CategoryTreeNode:
    """Nodo en memoria del arbol - no es el modelo ORM (evita mutar `Category.children`, usado por la relacion perezosa de SQLAlchemy)."""

    __slots__ = ("uuid", "name", "slug", "children")

    def __init__(self, uuid: str, name: str, slug: str):
        self.uuid = uuid
        self.name = name
        self.slug = slug
        self.children: list["CategoryTreeNode"] = []


async def get_category_tree(db: AsyncSession) -> list[CategoryTreeNode]:
    """Arbol completo desde Postgres, sin sync con Sicar X (tabla administrada por humanos, ver CLAUDE.md). Tabla chica - se trae completa y se arma en Python agrupando por `parent_uuid`. Ordenado por `name` en la consulta, lo que alfabetiza tanto roots como children."""
    result = await db.execute(
        select(Category.uuid, Category.name, Category.slug, Category.parent_uuid).order_by(func.lower(Category.name))
    )
    rows = result.all()

    nodes = {row.uuid: CategoryTreeNode(row.uuid, row.name, row.slug) for row in rows}
    roots: list[CategoryTreeNode] = []
    for row in rows:
        node = nodes[row.uuid]
        if row.parent_uuid and row.parent_uuid in nodes:
            nodes[row.parent_uuid].children.append(node)
        else:
            roots.append(node)
    return roots


async def get_descendant_uuids(db: AsyncSession, category_uuid: str) -> list[str]:
    """category_uuid + todos sus descendientes via WITH RECURSIVE - filtrar por un nodo padre tambien devuelve productos etiquetados en un hijo."""
    query = text("""
        WITH RECURSIVE descendants AS (
            SELECT uuid FROM categories WHERE uuid = :category_uuid
            UNION ALL
            SELECT c.uuid FROM categories c
            INNER JOIN descendants d ON c.parent_uuid = d.uuid
        )
        SELECT uuid FROM descendants
    """)
    result = await db.execute(query, {"category_uuid": category_uuid})
    return [row[0] for row in result.all()]


async def get_categories_for_product(db: AsyncSession, product_uuid: str) -> list[Category]:
    """Direccion inversa de `list_category_products`: categorias asignadas DIRECTAMENTE a un producto (sin ancestros)."""
    product = await db.scalar(select(Product).where(Product.sicar_uuid == product_uuid, Product.is_deleted == False))
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado.")

    stmt = select(Category).join(
        product_categories, Category.uuid == product_categories.c.category_uuid
    ).where(
        product_categories.c.product_id == product.id
    ).order_by(func.lower(Category.name))
    result = await db.execute(stmt)
    return list(result.scalars().all())


# Desde aqui: muta categories/product_categories, solo llamado por admin_categories.py (validate_admin_key).

def _slugify(name: str) -> str:
    """Espeja la logica de slugify de la migracion 5cbd5e4aa1be (no la importa - las migraciones son historia congelada)."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug or "categoria"


async def _unique_slug(db: AsyncSession, name: str, *, exclude_uuid: str | None = None) -> str:
    """A diferencia del helper de la migracion, consulta los slugs reales de la tabla en cada create/rename."""
    base = _slugify(name)
    query = select(Category.slug).where(Category.slug.like(f"{base}%"))
    if exclude_uuid:
        query = query.where(Category.uuid != exclude_uuid)
    existing = {row[0] for row in (await db.execute(query)).all()}

    if base not in existing:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing:
        suffix += 1
    return f"{base}-{suffix}"


async def create_category(db: AsyncSession, name: str, parent_uuid: str | None) -> Category:
    if parent_uuid is not None:
        parent = await db.get(Category, parent_uuid)
        if parent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="La categoria padre indicada no existe.")

    category = Category(
        uuid=str(uuid_lib.uuid4()),
        name=name,
        slug=await _unique_slug(db, name),
        parent_uuid=parent_uuid,
        updated_at=datetime.now(timezone.utc),
    )
    db.add(category)
    await db.commit()
    await db.refresh(category)
    logger.info(f"Categoria '{category.name}' ({category.uuid}) creada via /admin, parent_uuid={parent_uuid}.")
    return category


async def update_category(db: AsyncSession, category_uuid: str, data: dict) -> Category:
    """`data` es exclude_unset=True: "parent_uuid": None significa mover a raiz; ausente significa no tocar el padre."""
    category = await db.get(Category, category_uuid)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Categoria no encontrada.")

    if "parent_uuid" in data:
        new_parent = data["parent_uuid"]
        if new_parent is not None:
            if new_parent == category_uuid:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Una categoria no puede ser su propio padre.")
            parent = await db.get(Category, new_parent)
            if parent is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="La categoria padre indicada no existe.")
            descendants = await get_descendant_uuids(db, category_uuid)  # incluye category_uuid
            if new_parent in descendants:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No se puede mover una categoria dentro de su propio subarbol.")
        category.parent_uuid = new_parent

    if "name" in data:
        new_name = data["name"]
        if not new_name:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="name no puede estar vacio.")
        category.name = new_name
        category.slug = await _unique_slug(db, new_name, exclude_uuid=category_uuid)

    category.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(category)
    logger.info(f"Categoria {category.uuid} actualizada via /admin.")
    return category


async def delete_category(db: AsyncSession, category_uuid: str) -> None:
    category = await db.get(Category, category_uuid)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Categoria no encontrada.")

    has_children = await db.scalar(select(Category.uuid).where(Category.parent_uuid == category_uuid).limit(1))
    if has_children:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Esta categoria tiene subcategorias; reasignalas o eliminalas primero.")

    has_products = await db.scalar(select(product_categories.c.product_id).where(product_categories.c.category_uuid == category_uuid).limit(1))
    if has_products:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Esta categoria tiene productos asignados; quitalos primero.")

    await db.delete(category)
    await db.commit()
    logger.info(f"Categoria {category_uuid} eliminada via /admin.")


async def replace_category_products(db: AsyncSession, category_uuid: str, product_uuids: list[str]) -> list[str]:
    """Reemplaza el conjunto COMPLETO de productos asignados (no add/remove incremental)."""
    category = await db.get(Category, category_uuid)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Categoria no encontrada.")

    unique_uuids = sorted(set(product_uuids))
    product_ids: list[int] = []
    if unique_uuids:
        result = await db.execute(
            select(Product.id, Product.sicar_uuid).where(Product.sicar_uuid.in_(unique_uuids), Product.is_deleted == False)
        )
        found = {row.sicar_uuid: row.id for row in result.all()}
        missing = [u for u in unique_uuids if u not in found]
        if missing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Productos no encontrados: {', '.join(missing)}")
        product_ids = [found[u] for u in unique_uuids]

    await db.execute(delete(product_categories).where(product_categories.c.category_uuid == category_uuid))
    if product_ids:
        await db.execute(
            insert(product_categories),
            [{"category_uuid": category_uuid, "product_id": pid} for pid in product_ids],
        )
    await db.commit()
    logger.info(f"Categoria {category_uuid}: productos asignados reemplazados via /admin ({len(product_ids)} productos).")
    return unique_uuids


async def patch_category_products(db: AsyncSession, category_uuid: str, add_uuids: list[str], remove_uuids: list[str]) -> tuple[list[str], list[str], int, int]:
    """Incremental (a diferencia de replace_category_products): agrega/quita sin tocar el
    resto del conjunto asignado - pensado para categorias con mas productos asignados que
    el limite de GET .../products. `remove_uuids` es tolerante (no valida existencia)."""
    category = await db.get(Category, category_uuid)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Categoria no encontrada.")

    unique_add = sorted(set(add_uuids))
    add_product_ids: list[int] = []
    if unique_add:
        result = await db.execute(
            select(Product.id, Product.sicar_uuid).where(Product.sicar_uuid.in_(unique_add), Product.is_deleted == False)
        )
        found = {row.sicar_uuid: row.id for row in result.all()}
        missing = [u for u in unique_add if u not in found]
        if missing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Productos no encontrados: {', '.join(missing)}")
        add_product_ids = [found[u] for u in unique_add]

    added_count = 0
    if add_product_ids:
        insert_stmt = pg_insert(product_categories).values(
            [{"category_uuid": category_uuid, "product_id": pid} for pid in add_product_ids]
        ).on_conflict_do_nothing(index_elements=["category_uuid", "product_id"]).returning(product_categories.c.product_id)
        insert_result = await db.execute(insert_stmt)
        added_count = len(insert_result.all())

    unique_remove = sorted(set(remove_uuids))
    removed_count = 0
    if unique_remove:
        delete_stmt = delete(product_categories).where(
            product_categories.c.category_uuid == category_uuid,
            product_categories.c.product_id.in_(select(Product.id).where(Product.sicar_uuid.in_(unique_remove))),
        ).returning(product_categories.c.product_id)
        delete_result = await db.execute(delete_stmt)
        removed_count = len(delete_result.all())

    await db.commit()
    logger.info(
        f"Categoria {category_uuid}: {added_count} producto(s) agregado(s), {removed_count} quitado(s) via PATCH /admin."
    )
    return unique_add, unique_remove, added_count, removed_count


async def list_category_products(db: AsyncSession, category_uuid: str, limit: int, offset: int) -> tuple[int, list[Product]]:
    """Solo productos asignados DIRECTAMENTE (sin descendientes, a diferencia del filtro taxonomy_uuid) - para la UI de edicion, no para el storefront."""
    category = await db.get(Category, category_uuid)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Categoria no encontrada.")

    base = select(Product).join(product_categories, Product.id == product_categories.c.product_id).where(
        product_categories.c.category_uuid == category_uuid,
        Product.is_deleted == False,
    )
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    result = await db.execute(base.order_by(Product.name).limit(limit).offset(offset))
    return total or 0, list(result.scalars().all())
