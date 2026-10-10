"""Productos relacionados ("Tambien te puede servir", GET /products/{uuid}/related). Ver
CLAUDE.md, "Productos relacionados".

Se llenan por niveles, sin repetir, hasta `limit`: (1) comprados juntos en pedidos pagados de
los ultimos 180 dias, (2) mismo nodo de taxonomia (o su padre si el nodo da pocos), (3) misma
marca, (4) compatibles con los mismos vehiculos. Siempre se excluye el propio producto y los de
su grupo de variantes, y solo cuentan productos activos y no eliminados.

Lo que se cachea (en memoria del proceso api, TTL 1 h) es solo la lista ordenada de
sicar_uuid; precio y stock se leen de Postgres en cada request (search_service.hydrate),
mismo principio de "nunca confiar en una copia del precio/stock" que la busqueda. Sirve
porque hoy hay una sola instancia de api; con varias, cada una tendria su propia cache."""
import time
from collections import OrderedDict
from typing import Optional

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product
from app.models.taxonomy import Category, product_categories
from app.models.vehicle import product_vehicles
from app.services.search_service import hydrate
from app.services.taxonomy_service import get_descendant_uuids

BOUGHT_TOGETHER_DAYS = 180
CACHE_TTL_SECONDS = 3600
CACHE_MAX_ENTRIES = 5000
# Lo que se calcula y cachea por producto, independiente del `limit` pedido (<= 24), para
# que distintos limit compartan la misma entrada.
MAX_RELATED = 24

_cache: "OrderedDict[str, tuple[float, list[str]]]" = OrderedDict()


def clear_cache() -> None:
    _cache.clear()


def _cache_get(key: str) -> Optional[list[str]]:
    entry = _cache.get(key)
    if entry is None:
        return None
    stored_at, value = entry
    if time.monotonic() - stored_at > CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return value


def _cache_set(key: str, value: list[str]) -> None:
    _cache[key] = (time.monotonic(), value)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_MAX_ENTRIES:
        _cache.popitem(last=False)


def _candidates(product: Product):
    """Base de cada nivel: activos, no eliminados, ni el propio producto ni su grupo de
    variantes; primero los que tienen existencia, luego los mas vendidos."""
    stmt = select(Product.sicar_uuid).where(
        Product.is_deleted == False,  # noqa: E712
        Product.is_active == True,  # noqa: E712
        Product.id != product.id,
    )
    if product.variant_group_uuid:
        stmt = stmt.where(
            (Product.variant_group_uuid.is_(None)) | (Product.variant_group_uuid != product.variant_group_uuid)
        )
    return stmt.order_by((Product.available_stock > 0).desc(), Product.sales_count.desc(), Product.name.asc())


async def _bought_together(db: AsyncSession, product: Product, limit: int) -> list[str]:
    # Order.items es JSON (no JSONB): se castea para desempacarlo. El volumen de pedidos
    # pagados en 180 dias es chico, asi que no hace falta un indice para esto.
    rows = await db.execute(text("""
        WITH lines AS (
            SELECT o.id AS order_id, line->>'uuid' AS uuid
            FROM orders o, jsonb_array_elements(o.items::jsonb) AS line
            WHERE o.status = 'PAID' AND o.deleted_at IS NULL
              AND o.created_at > now() - make_interval(days => :days)
        ),
        target_orders AS (SELECT DISTINCT order_id FROM lines WHERE uuid = :uuid)
        SELECT l.uuid, COUNT(DISTINCT l.order_id) AS times
        FROM lines l JOIN target_orders t ON t.order_id = l.order_id
        WHERE l.uuid <> :uuid
        GROUP BY l.uuid
        ORDER BY times DESC
        LIMIT :limit
    """), {"uuid": product.sicar_uuid, "days": BOUGHT_TOGETHER_DAYS, "limit": limit * 4})
    ranked = [row.uuid for row in rows]
    if not ranked:
        return []
    # Filtra por las mismas reglas que los demas niveles, conservando el orden por frecuencia.
    allowed = set((await db.execute(_candidates(product).where(Product.sicar_uuid.in_(ranked)))).scalars().all())
    return [u for u in ranked if u in allowed]


async def _same_taxonomy(db: AsyncSession, product: Product, limit: int, exclude: set[str]) -> list[str]:
    direct = (await db.execute(
        select(product_categories.c.category_uuid).where(product_categories.c.product_id == product.id)
    )).scalars().all()
    if not direct:
        return []

    async def by_categories(category_uuids: list[str]) -> list[str]:
        stmt = _candidates(product).where(Product.id.in_(
            select(product_categories.c.product_id).where(product_categories.c.category_uuid.in_(category_uuids))
        ))
        if exclude:
            stmt = stmt.where(Product.sicar_uuid.not_in(exclude))
        return list((await db.execute(stmt.limit(limit))).scalars().all())

    found = await by_categories(list(direct))
    if len(found) >= limit:
        return found

    # Pocos en el nodo: se sube al padre (con todos sus descendientes).
    parents = (await db.execute(
        select(Category.parent_uuid).where(Category.uuid.in_(direct), Category.parent_uuid.is_not(None))
    )).scalars().all()
    widened: list[str] = []
    for parent_uuid in dict.fromkeys(parents):
        widened.extend(await get_descendant_uuids(db, parent_uuid))
    if widened:
        found += [u for u in await by_categories(list(dict.fromkeys(widened))) if u not in found]
    return found


async def _same_brand(db: AsyncSession, product: Product, limit: int, exclude: set[str]) -> list[str]:
    if not product.brand:
        return []
    stmt = _candidates(product).where(func.lower(Product.brand) == product.brand.lower())
    if exclude:
        stmt = stmt.where(Product.sicar_uuid.not_in(exclude))
    return list((await db.execute(stmt.limit(limit))).scalars().all())


async def _same_vehicles(db: AsyncSession, product: Product, limit: int, exclude: set[str]) -> list[str]:
    vehicle_uuids = select(product_vehicles.c.vehicle_uuid).where(product_vehicles.c.product_id == product.id)
    stmt = _candidates(product).where(Product.id.in_(
        select(product_vehicles.c.product_id).where(product_vehicles.c.vehicle_uuid.in_(vehicle_uuids))
    ))
    if exclude:
        stmt = stmt.where(Product.sicar_uuid.not_in(exclude))
    return list((await db.execute(stmt.limit(limit))).scalars().all())


async def compute_related_uuids(db: AsyncSession, product: Product, limit: int = MAX_RELATED) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    def take(uuids: list[str]) -> None:
        for u in uuids:
            if len(result) >= limit:
                return
            if u not in seen:
                seen.add(u)
                result.append(u)

    take(await _bought_together(db, product, limit))
    for level in (_same_taxonomy, _same_brand, _same_vehicles):
        if len(result) >= limit:
            break
        take(await level(db, product, limit - len(result), seen))
    return result


async def get_related_products(db: AsyncSession, product: Product, limit: int) -> list[Product]:
    uuids = _cache_get(product.sicar_uuid)
    if uuids is None:
        uuids = await compute_related_uuids(db, product, MAX_RELATED)
        _cache_set(product.sicar_uuid, uuids)
    # hydrate omite lo que se haya desactivado desde que se cacheo; se piden de mas para
    # seguir llenando `limit` en ese caso.
    return (await hydrate(db, uuids))[:limit]
