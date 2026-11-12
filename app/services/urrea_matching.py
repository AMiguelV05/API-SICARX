"""Emparejamiento de codigos de URREA con productos locales. Unica regla, compartida por el
worker (urrea_sync_worker.py) e import_package_dimensions.py:

1. sku = URREA_SKU_PREFIX ("U") + codigo. En Sicar X los productos de Urrea/Surtek/Foy/Lock/
   Prolok llevan ese sku. Comparar el codigo crudo contra sku es INCORRECTO: choca con
   productos de otras marcas que comparten el mismo codigo numerico.
2. Si no resuelve, el codigo de barras contra sku y additional_skus.

Solo productos no borrados (bulk_import_service._resolve_products)."""
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.bulk_import_service import _resolve_products

SKU_PREFIX = "SKU_PREFIX"
BARCODE = "BARCODE"

# asyncpg admite hasta 32767 parametros por consulta.
_CHUNK = 5000


async def _resolve_chunked(db: AsyncSession, values: set[str]) -> dict[str, int]:
    ordered = sorted(values)
    found: dict[str, int] = {}
    for i in range(0, len(ordered), _CHUNK):
        found.update(await _resolve_products(db, set(ordered[i:i + _CHUNK])))
    return found


async def match_codes(db: AsyncSession, pairs: list[tuple[str, str | None]], prefix: str) -> dict[str, tuple[int, str]]:
    """[(codigo, codigo_de_barras)] -> {codigo: (product_id, metodo)}. Codigos sin producto
    local no aparecen en el resultado."""
    by_sku = await _resolve_chunked(db, {f"{prefix}{code}" for code, _ in pairs})
    matches: dict[str, tuple[int, str]] = {}
    pending: list[tuple[str, str]] = []
    for code, barcode in pairs:
        product_id = by_sku.get(f"{prefix}{code}".upper())
        if product_id is not None:
            matches[code] = (product_id, SKU_PREFIX)
        elif barcode:
            pending.append((code, barcode))
    if pending:
        by_barcode = await _resolve_chunked(db, {b for _, b in pending})
        for code, barcode in pending:
            product_id = by_barcode.get(barcode.upper())
            if product_id is not None:
                matches[code] = (product_id, BARCODE)
    return matches
