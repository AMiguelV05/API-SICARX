"""Venta sobre pedido (backorder) con URREA, detras de URREA_BACKORDER_ENABLED.

Una linea puede pedir mas de lo que hay en existencia (Product.available_stock) si el producto
lo permite (Product.backorder_allowed) y URREA confirma, con la opcion 2, que tiene el faltante.

Las lineas sobre pedido reservan la cantidad COMPLETA en Product.reserved, no solo la parte en
existencia: asi, cuando la mercancia de URREA se da de entrada en Sicar X, esas piezas no
aparecen como disponibles para otro cliente. Cancelacion y ACCEPT no cambian. La alerta de
deriva (sync_task.py) descuenta lo reservado sobre pedido con open_backorder_quantities().

Las respuestas de URREA se guardan en memoria del proceso: frescas 10 min, y hasta 60 min como
respaldo si URREA no responde. "Suficiente" para N implica suficiente para menos de N, e
"Insuficiente" para N implica insuficiente para mas."""
import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import Numeric, String, column, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.product import Product
from app.models.supplier_product import SupplierProduct
from app.services import urrea_client
from app.services.urrea_client import SUPPLIER, UrreaError

logger = logging.getLogger(__name__)

FRESH_SECONDS = 10 * 60
FALLBACK_SECONDS = 60 * 60
CONCURRENCY = 4
STOCK = "STOCK"
BACKORDER = "BACKORDER"

# code -> [(momento, cantidad consultada, suficiente)]
_cache: dict[str, list[tuple[float, int, bool]]] = {}


def clear_cache() -> None:
    _cache.clear()


def shortfall(product: Product, quantity) -> Decimal:
    """Lo que falta en existencia para surtir `quantity` (0 si alcanza)."""
    missing = Decimal(str(quantity)) - product.available_stock
    return missing if missing > 0 else Decimal("0")


def _whole(quantity: Decimal) -> int | None:
    return int(quantity) if quantity == quantity.to_integral_value() else None


def _cached(code: str, quantity: int, max_age: float) -> bool | None:
    now = time.monotonic()
    for at, qty, sufficient in reversed(_cache.get(code, [])):
        if now - at > max_age:
            continue
        if sufficient and qty >= quantity:
            return True
        if not sufficient and qty <= quantity:
            return False
    return None


def _remember(code: str, quantity: int, sufficient: bool) -> None:
    now = time.monotonic()
    entries = [e for e in _cache.get(code, []) if now - e[0] <= FALLBACK_SECONDS][-9:]
    entries.append((now, quantity, sufficient))
    _cache[code] = entries


async def supplier_has(code: str, quantity: int) -> bool | None:
    """True/False segun URREA para `quantity` piezas de `code`; None si no se pudo confirmar
    (URREA caido o mal configurado y sin respuesta reciente que sirva de respaldo)."""
    cached = _cached(code, quantity, FRESH_SECONDS)
    if cached is not None:
        return cached
    try:
        result = await urrea_client.check_availability(code, quantity)
    except UrreaError as e:
        fallback = _cached(code, quantity, FALLBACK_SECONDS)
        logger.warning(f"No se pudo confirmar con URREA {code} x{quantity}: {e}. Respaldo: {fallback}.")
        return fallback
    _remember(code, quantity, result.sufficient)
    return result.sufficient


async def _supplier_codes(db: AsyncSession, product_ids: list[int]) -> dict[int, str]:
    if not product_ids:
        return {}
    rows = await db.execute(
        select(SupplierProduct.product_id, SupplierProduct.supplier_code)
        .where(SupplierProduct.supplier == SUPPLIER, SupplierProduct.product_id.in_(product_ids))
    )
    return dict(rows.all())


@dataclass(frozen=True)
class QuantityCheck:
    quantity: Decimal
    stock: Decimal
    backorder_quantity: Decimal
    available: bool | None
    lead_days: int | None


async def check_quantity(db: AsyncSession, product: Product, quantity: Decimal) -> QuantityCheck:
    """¿Se pueden comprar `quantity` piezas ahora? Sin llamar a URREA si alcanza la existencia."""
    missing = shortfall(product, quantity)
    stock = product.available_stock
    if missing == 0:
        return QuantityCheck(quantity, stock, missing, True, None)
    whole = _whole(missing)
    if not product.backorder_allowed or whole is None:
        return QuantityCheck(quantity, stock, Decimal("0"), False, None)
    code = (await _supplier_codes(db, [product.id])).get(product.id)
    if code is None:
        return QuantityCheck(quantity, stock, Decimal("0"), False, None)
    # Sin transaccion abierta durante la llamada a URREA (hasta 5 s).
    await db.commit()
    has = await supplier_has(code, whole)
    return QuantityCheck(quantity, stock, missing if has else Decimal("0"), has, settings.URREA_BACKORDER_LEAD_DAYS if has else None)


async def confirm_backorders(db: AsyncSession, quantities: dict[str, float]) -> dict[str, Decimal]:
    """Pre-check de POST /orders, FUERA del FOR UPDATE: para cada linea que excede la
    existencia y puede ir sobre pedido, confirma el faltante con URREA (en paralelo).
    Devuelve {uuid: faltante confirmado}. Las lineas que URREA rechaza no se incluyen y
    validate_cart_items las rechaza con 409. 503 si URREA no responde y no hay respaldo.
    Hace commit antes de llamar a URREA (no deja la transaccion abierta)."""
    if not settings.urrea_backorder_active:
        return {}
    result = await db.execute(
        select(Product).where(Product.sicar_uuid.in_(list(quantities)), Product.is_deleted == False, Product.is_active == True)  # noqa: E712
    )
    candidates: dict[str, tuple[Product, int]] = {}
    for product in result.scalars().all():
        missing = shortfall(product, quantities[product.sicar_uuid])
        whole = _whole(missing)
        if missing > 0 and whole is not None and product.backorder_allowed:
            candidates[product.sicar_uuid] = (product, whole)
    if not candidates:
        return {}
    codes = await _supplier_codes(db, [p.id for p, _ in candidates.values()])
    await db.commit()

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def check(product_uuid: str, product: Product, whole: int) -> tuple[str, int, bool | None]:
        code = codes.get(product.id)
        if code is None:
            return product_uuid, whole, False
        async with semaphore:
            return product_uuid, whole, await supplier_has(code, whole)

    outcomes = await asyncio.gather(*(check(u, p, w) for u, (p, w) in candidates.items()))
    if any(has is None for _, _, has in outcomes):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No pudimos confirmar la disponibilidad sobre pedido con el proveedor. Intenta de nuevo en unos minutos o reduce la cantidad a la existencia.",
        )
    return {product_uuid: Decimal(whole) for product_uuid, whole, has in outcomes if has}


def backorder_quantities(products: dict[str, Product], quantities: dict[str, float]) -> dict[str, Decimal]:
    """Faltante por linea con la existencia ya bloqueada (FOR UPDATE)."""
    return {u: shortfall(products[u], q) for u, q in quantities.items() if u in products}


def open_backorder_quantities_subquery():
    """(product_uuid, qty): lo reservado sobre pedido por ordenes abiertas (no canceladas ni
    aceptadas). Al aceptar, la reserva se libera completa (sicar_sync_worker)."""
    return text("""
        SELECT item->>'uuid' AS product_uuid, SUM((item->>'backorderQuantity')::numeric) AS qty
        FROM orders, jsonb_array_elements(orders.items) AS item
        WHERE orders.status <> 'CANCELLED' AND orders.accepted_at IS NULL
          AND item->>'fulfillment' = 'BACKORDER'
        GROUP BY item->>'uuid'
    """).columns(column("product_uuid", String), column("qty", Numeric)).subquery("open_backorders")
