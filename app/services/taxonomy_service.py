import logging
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.taxonomy import Category

logger = logging.getLogger(__name__)


class CategoryTreeNode:
    """Nodo en memoria del arbol de categorias - no es el modelo ORM (evita
    mutar `Category.children`, que SQLAlchemy ya usa para su propia relacion
    perezosa)."""

    __slots__ = ("uuid", "name", "slug", "children")

    def __init__(self, uuid: str, name: str, slug: str):
        self.uuid = uuid
        self.name = name
        self.slug = slug
        self.children: list["CategoryTreeNode"] = []


async def get_category_tree(db: AsyncSession) -> list[CategoryTreeNode]:
    """Arbol completo de categorias, siempre desde Postgres - ya no hay
    sincronizacion perezosa con Sicar X (ver CLAUDE.md, "Taxonomia": esta
    tabla es administrada por humanos, no un cache de Sicar X). La tabla es
    chica (decenas/cientos de filas incluso con 500k productos, es taxonomia
    no dato por producto) asi que se trae completa en una sola consulta y el
    arbol se arma en Python agrupando por `parent_uuid`, en vez de una consulta
    recursiva - mas simple y mas que suficiente a este tamano."""
    result = await db.execute(select(Category.uuid, Category.name, Category.slug, Category.parent_uuid))
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
    """`{category_uuid} unido a todos sus descendientes`, via `WITH RECURSIVE`
    - usado para que filtrar por un nodo padre (p. ej. "Motocicleta") tambien
    devuelva productos etiquetados solo en un hijo (p. ej. "Encendido"). Una
    CTE recursiva es la herramienta correcta aqui - una tabla de cierre
    transitivo o nested sets serian optimizacion prematura para un arbol de
    este tamano (ver CLAUDE.md)."""
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
