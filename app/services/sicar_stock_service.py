import httpx
import logging
from decimal import Decimal
from app.services.sicar_auth import sicar_auth
from app.core.sicar_headers import admin_app_headers
from app.core.sicar_validation import is_safe_sicar_id
from app.core.upstream_errors import raise_upstream_error

STOCK_URL = "https://api.sicarx.com/stock/v1/stock"
STOCK_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
logger = logging.getLogger(__name__)


async def _read_current_stock(product_uuid: str, branch_id: int | None) -> Decimal:
    """GET /stock/v1/stock/{uuid}/all - confirmado en vivo (agent-browser contra el
    panel de Sicar X, boton "Ajustar inventario" de un producto) como la lectura que
    respalda ese mismo dialogo. Se llama justo antes de cada PATCH en vez de confiar en
    el cache local de Postgres (que puede estar desfasado) - mismo comportamiento que el
    propio dialogo de Sicar X, que vuelve a leer cada vez que se abre en vez de reusar un
    valor viejo. Si el producto tiene mas de un almacen, se toma la fila cuyo warehouseId
    coincide con branch_id (confirmado en vivo que branchId/warehouseId son el mismo
    valor para esta cuenta); con un solo almacen (el caso de esta cuenta hoy) se usa esa
    fila directamente."""
    async def attempt(admin_token: str):
        headers = admin_app_headers(admin_token)
        async with httpx.AsyncClient(timeout=STOCK_TIMEOUT) as client:
            return await client.get(f"{STOCK_URL}/{product_uuid}/all", headers=headers)

    response = await sicar_auth.request_with_retry(attempt)
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
    """PATCH /stock/v1/stock/{uuid} - mismo endpoint/forma de payload confirmados en vivo
    (interceptado con `network route ... --abort` antes de enviarlo, para no aplicar
    ningun cambio real durante la investigacion): {"currentStock", "newStock",
    "available"}. `available` siempre igual a `newStock` - confirmado que asi lo manda el
    propio dialogo "Ajustar inventario" de Sicar X para esta cuenta (un solo almacen, sin
    reservas separadas)."""
    async def attempt(admin_token: str):
        headers = admin_app_headers(admin_token)
        payload = {
            "currentStock": float(current_stock),
            "newStock": float(new_stock),
            "available": float(new_stock),
        }
        async with httpx.AsyncClient(timeout=STOCK_TIMEOUT) as client:
            return await client.patch(f"{STOCK_URL}/{product_uuid}", json=payload, headers=headers)

    response = await sicar_auth.request_with_retry(attempt)
    if response.status_code != 200:
        raise_upstream_error(
            response,
            f"Sicar X rechazo el ajuste de inventario para {product_uuid}",
            "Sicar X rechazó el ajuste de inventario.",
        )


async def apply_order_stock_delta(order_items: list[dict], branch_id: int | None, *, sign: int) -> None:
    """Aplica en Sicar X el efecto de inventario de una orden completa, linea por linea:
    sign=-1 al aceptar (la venta ya se confirmo, se descuenta), sign=+1 al cancelar una
    orden que ya habia sido aceptada (se revierte el descuento). Es la UNICA forma en que
    este backend le avisa algo a Sicar X sobre una orden - no crea, paga ni cancela ningun
    documento/orden ahi, solo ajusta existencias (ver CLAUDE.md, "SICAR es solo ERP de
    inventario"). Llamada exclusivamente desde sicar_sync_worker.py (acciones ACCEPT/
    CANCEL), nunca desde una ruta de la API ni dentro de una transaccion/lock de Postgres -
    cada linea hace su propio GET+PATCH, fuera de cualquier lock, igual que el resto de
    las llamadas HTTP salientes de este modulo.

    Riesgo conocido, no resuelto aqui (ver el plan de este cambio, seccion "Design
    constraints"): si una orden tiene varias lineas y esta funcion falla a medio camino
    (linea 2 de 3, por ejemplo), el reintento del outbox (sicar_sync_worker.py, backoff
    exponencial) vuelve a procesar TODAS las lineas desde cero, incluida la que ya se
    aplico con exito - un GET+PATCH extra sobre esa linea no es idempotente (podria
    descontar dos veces). Aceptable por ahora dado que la mayoria de ordenes de esta
    tienda tienen pocas lineas y una falla a medio camino es rara; si se vuelve un
    problema real, la solucion es trackear que lineas de la orden ya se sincronizaron
    (p. ej. una columna nueva) en vez de reprocesar la lista completa en cada intento."""
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
