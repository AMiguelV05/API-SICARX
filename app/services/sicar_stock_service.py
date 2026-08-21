import httpx
import logging
from decimal import Decimal
from app.services.sicar_auth import sicar_auth
from app.core.retry import request_with_backoff
from app.core.sicar_headers import admin_app_headers
from app.core.sicar_validation import is_safe_sicar_id
from app.core.upstream_errors import raise_upstream_error

STOCK_URL = "https://api.sicarx.com/stock/v1/stock"
STOCK_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
logger = logging.getLogger(__name__)


async def _read_current_stock(product_uuid: str, branch_id: int | None) -> Decimal:
    """GET /stock/v1/stock/{uuid}/all - se lee justo antes de cada PATCH, sin confiar en el cache local de Postgres. Con varios almacenes, se usa la fila cuyo warehouseId coincide con branch_id."""
    async def attempt(admin_token: str):
        headers = admin_app_headers(admin_token)
        async with httpx.AsyncClient(timeout=STOCK_TIMEOUT) as client:
            return await client.get(f"{STOCK_URL}/{product_uuid}/all", headers=headers)

    async def call_with_auth():
        return await sicar_auth.request_with_retry(attempt)

    response = await request_with_backoff(call_with_auth, context=f"Sicar X stock read {product_uuid}")
    if response.status_code != 200:
        raise_upstream_error(
            response,
            f"Error al leer la existencia de {product_uuid} en Sicar X",
            "No se pudo leer la existencia del producto en Sicar X.",
        )

    rows = (response.json() or {}).get("stock") or []
    if not rows:
        raise ValueError(f"Sicar X no devolvio ninguna fila de almacen para el producto {product_uuid}.")

    row = rows[0]
    if len(rows) > 1 and branch_id is not None:
        row = next((r for r in rows if str(r.get("warehouseId")) == str(branch_id)), row)

    return Decimal(str(row.get("stock", 0)))


async def _patch_stock(product_uuid: str, current_stock: Decimal, new_stock: Decimal) -> None:
    """PATCH /stock/v1/stock/{uuid} con {"currentStock", "newStock", "available"} - `available` siempre igual a `newStock` (sin reservas separadas para esta cuenta)."""
    async def attempt(admin_token: str):
        headers = admin_app_headers(admin_token)
        payload = {
            "currentStock": float(current_stock),
            "newStock": float(new_stock),
            "available": float(new_stock),
        }
        async with httpx.AsyncClient(timeout=STOCK_TIMEOUT) as client:
            return await client.patch(f"{STOCK_URL}/{product_uuid}", json=payload, headers=headers)

    async def call_with_auth():
        return await sicar_auth.request_with_retry(attempt)

    response = await request_with_backoff(call_with_auth, context=f"Sicar X stock patch {product_uuid}")
    if response.status_code != 200:
        raise_upstream_error(
            response,
            f"Sicar X rechazo el ajuste de inventario para {product_uuid}",
            "Sicar X rechazó el ajuste de inventario.",
        )


async def apply_order_stock_delta(order_items: list[dict], branch_id: int | None, *, sign: int) -> None:
    """sign=-1 al aceptar, sign=+1 al cancelar; unica via por la que este backend le avisa algo a Sicar X sobre una orden (solo inventario, ver CLAUDE.md). Riesgo conocido: un reintento tras falla parcial reprocesa TODAS las lineas, incluida la ya aplicada (no idempotente)."""
    for item in order_items or []:
        product_uuid = item.get("uuid")
        try:
            quantity = Decimal(str(item.get("quantity", 0)))
        except (TypeError, ValueError, ArithmeticError):
            quantity = Decimal("0")

        if not product_uuid or not is_safe_sicar_id(product_uuid) or quantity == 0:
            continue

        current_stock = await _read_current_stock(product_uuid, branch_id)
        new_stock = current_stock + (sign * quantity)
        await _patch_stock(product_uuid, current_stock, new_stock)
        logger.info(
            f"Sicar X: existencia de {product_uuid} ajustada {current_stock} -> {new_stock} "
            f"(sign={sign:+d}, quantity={quantity})."
        )
