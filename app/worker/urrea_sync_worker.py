"""Sync del catalogo de URREA (opcion 4, "disponibilidad masiva") hacia supplier_products, las
columnas desnormalizadas de products (supplier_available, fallback_image_url) y el PIM.

- Corre cada RUN_INTERVAL (o antes, si un admin lo pide con POST /admin/suppliers/urrea/sync).
  El job revisa cada minuto si toca (`run_if_due`); la API nunca corre el sync.
- La masiva (~22 MB) se descarga a un archivo temporal y se recorre en streaming por lotes de
  CHUNK filas, con commit por lote: la memoria no crece con el catalogo.
- Solo se escriben filas que cambian (WAL/RAM, mismo criterio que el sync de Sicar X).
- Codigos que dejan de venir en la masiva: su fila se conserva (guarda pim_fields_applied) con
  availability_status NULL, y el producto pierde supplier_available.
- PIM: ver urrea_pim_service.py (la primera aplicacion reemplaza, despues solo llena vacios).

Simulacion sin escribir nada, con reporte CSV de los cambios de PIM que haria:

    python -m app.worker.urrea_sync_worker --dry-run --report cambios.csv [--file masiva.json]
"""
import argparse
import asyncio
import csv
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Iterator

from sqlalchemy import bindparam, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.error_tracking import capture_exception
from app.models.product import Product
from app.models.supplier_product import SupplierProduct, SupplierSyncStatus
from app.services import urrea_client
from app.services.urrea_client import AVAILABLE, BULK_JOB as JOB, LIMITED, SUPPLIER, UrreaCatalogRow
from app.services.urrea_matching import match_codes
from app.services.urrea_pim_service import PimChange, apply_supplier_info

logger = logging.getLogger(__name__)

CHUNK = 2000
RUN_INTERVAL = timedelta(hours=3)
# Tras un fallo se reintenta antes que el intervalo normal.
RETRY_AFTER_FAILURE = timedelta(minutes=30)

# Columnas de supplier_products que escribe el sync (ademas de supplier/supplier_code).
_SUPPLIER_COLUMNS = (
    "product_id", "match_method", "barcode", "list_price", "currency", "sale_multiple",
    "availability_status", "photo_urls", "video_url", "datasheet_url", "manual_url",
    "set_contents_url", "info", "pim_fields_applied",
)


@dataclass
class SyncResult:
    received: int = 0
    matched: int = 0
    changed: int = 0
    pim_fields_written: int = 0
    duplicates: int = 0
    pim_changes: list[PimChange] = field(default_factory=list)


def _supplier_values(row: UrreaCatalogRow, product_id: int, method: str) -> dict:
    return {
        "product_id": product_id,
        "match_method": method,
        "barcode": row.barcode,
        "list_price": row.price,
        "currency": row.currency,
        "sale_multiple": row.sale_multiple,
        "availability_status": row.status,
        "photo_urls": row.photo_urls or None,
        "video_url": row.video_url,
        "datasheet_url": row.datasheet_url,
        "manual_url": row.manual_url,
        "set_contents_url": row.set_contents_url,
        "info": row.info(),
    }


def _differs(stored: SupplierProduct | None, values: dict) -> bool:
    if stored is None:
        return True
    for column, value in values.items():
        current = getattr(stored, column)
        if isinstance(value, Decimal) and current is not None:
            if Decimal(current) != value:
                return True
        elif current != value:
            return True
    return False


def _chunks(rows: Iterable[UrreaCatalogRow], size: int) -> Iterator[list[UrreaCatalogRow]]:
    batch: list[UrreaCatalogRow] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


async def _process_chunk(session: AsyncSession, rows: list[UrreaCatalogRow], seen_codes: set[str],
                         claimed_products: set[int], result: SyncResult) -> None:
    rows = [r for r in rows if r.code not in seen_codes]
    seen_codes.update(r.code for r in rows)
    result.received += len(rows)

    matches = await match_codes(session, [(r.code, r.barcode) for r in rows], settings.URREA_SKU_PREFIX)
    matched_pids = {pid for pid, _ in matches.values()}
    stored_rows = (await session.execute(
        select(SupplierProduct).where(
            SupplierProduct.supplier == SUPPLIER,
            or_(SupplierProduct.supplier_code.in_([r.code for r in rows]), SupplierProduct.product_id.in_(matched_pids)),
        )
        # Las escrituras de este modulo son core (INSERT ... ON CONFLICT): sin esto, una fila ya
        # cargada en la sesion conservaria valores viejos.
        .execution_options(populate_existing=True)
    )).scalars().all()
    stored_by_code = {s.supplier_code: s for s in stored_rows}
    code_by_product = {s.product_id: s.supplier_code for s in stored_rows}

    upserts: list[dict] = []
    pim_items: list[tuple[int, list[str] | None, UrreaCatalogRow]] = []
    desired_product_state: dict[int, tuple[bool, str | None]] = {}
    for row in rows:
        match = matches.get(row.code)
        if match is None:
            continue
        product_id, method = match
        owner = code_by_product.get(product_id)
        if product_id in claimed_products or (owner is not None and owner != row.code):
            # Otro codigo de URREA ya tiene este producto: se queda el primero.
            result.duplicates += 1
            continue
        claimed_products.add(product_id)
        result.matched += 1

        stored = stored_by_code.get(row.code)
        values = _supplier_values(row, product_id, method)
        info_changed = stored is None or stored.info != values["info"] or stored.product_id != product_id
        applied = stored.pim_fields_applied if stored is not None else None
        if info_changed or applied is None:
            pim_items.append((product_id, applied, row))
        if _differs(stored, values):
            upserts.append({"supplier_code": row.code, **values, "pim_fields_applied": applied})
        desired_product_state[product_id] = (
            row.status in (AVAILABLE, LIMITED),
            row.photo_urls[0] if row.photo_urls else None,
        )

    changes, applied_updates = await apply_supplier_info(session, pim_items)
    result.pim_changes.extend(changes)
    result.pim_fields_written += len(changes)
    if applied_updates:
        upsert_by_code = {u["supplier_code"]: u for u in upserts}
        row_by_product = {pid: row for pid, _, row in pim_items}
        for product_id, applied in applied_updates.items():
            row = row_by_product[product_id]
            entry = upsert_by_code.get(row.code)
            if entry is None:
                entry = {"supplier_code": row.code, **_supplier_values(row, product_id, matches[row.code][1])}
                upserts.append(entry)
            entry["pim_fields_applied"] = applied

    # ~16 parametros por fila: lotes de 1000 para no pasar el tope de asyncpg (32767).
    for i in range(0, len(upserts), 1000):
        stmt = insert(SupplierProduct).values([{"supplier": SUPPLIER, **u} for u in upserts[i:i + 1000]])
        stmt = stmt.on_conflict_do_update(
            constraint="uq_supplier_products_code",
            set_={**{c: getattr(stmt.excluded, c) for c in _SUPPLIER_COLUMNS}, "updated_at": func.now()},
        )
        await session.execute(stmt)
    result.changed += len(upserts)

    await _apply_product_state(session, desired_product_state)


async def _apply_product_state(session: AsyncSession, desired: dict[int, tuple[bool, str | None]]) -> None:
    """products.supplier_available / fallback_image_url, solo donde cambian."""
    if not desired:
        return
    current = (await session.execute(
        select(Product.id, Product.supplier_available, Product.fallback_image_url).where(Product.id.in_(list(desired)))
    )).all()
    rows = [
        {"pid": pid, "available": desired[pid][0], "fallback": desired[pid][1]}
        for pid, available, fallback in current
        if (available, fallback) != desired[pid]
    ]
    if rows:
        stmt = (
            update(Product)
            .where(Product.id == bindparam("pid"))
            .values(supplier_available=bindparam("available"), fallback_image_url=bindparam("fallback"))
            .execution_options(dml_strategy="core_only")
        )
        await session.execute(stmt, rows)


async def _clear_missing(session: AsyncSession, seen_codes: set[str]) -> int:
    """Codigos que ya no vienen en la masiva: sin estatus y sin supplier_available. La fila se
    conserva por pim_fields_applied (si el codigo vuelve, no se repite el reemplazo inicial)."""
    stale = (await session.execute(
        select(SupplierProduct.id, SupplierProduct.supplier_code, SupplierProduct.product_id).where(
            SupplierProduct.supplier == SUPPLIER, SupplierProduct.availability_status.isnot(None),
        )
    )).all()
    stale = [s for s in stale if s.supplier_code not in seen_codes]
    if not stale:
        return 0
    ids = [s.id for s in stale]
    pids = [s.product_id for s in stale]
    for i in range(0, len(ids), 5000):
        await session.execute(
            update(SupplierProduct).where(SupplierProduct.id.in_(ids[i:i + 5000]))
            .values(availability_status=None, updated_at=func.now())
        )
        await session.execute(
            update(Product).where(Product.id.in_(pids[i:i + 5000]), Product.supplier_available == True)  # noqa: E712
            .values(supplier_available=False)
        )
    return len(stale)


async def sync_bulk_catalog(session: AsyncSession, path: str, *, commit: bool = True) -> SyncResult:
    """Aplica una masiva ya descargada. commit=False: todo queda en la transaccion (simulacion
    o pruebas); el llamador hace rollback."""
    result = SyncResult()
    seen_codes: set[str] = set()
    claimed_products: set[int] = set()
    for chunk in _chunks(urrea_client.iter_bulk_rows(path), CHUNK):
        await _process_chunk(session, chunk, seen_codes, claimed_products, result)
        if commit:
            await session.commit()
        else:
            await session.flush()
    if result.received:
        # Una masiva vacia no borra nada (mismo criterio que el sync de Sicar X).
        cleared = await _clear_missing(session, seen_codes)
        if cleared:
            logger.info(f"Codigos de URREA que ya no vienen en la masiva: {cleared}.")
    if commit:
        await session.commit()
    logger.info(
        f"Sync URREA: {result.received} codigos, {result.matched} emparejados, {result.changed} filas de proveedor "
        f"escritas, {result.pim_fields_written} campos de PIM escritos, {result.duplicates} codigos duplicados por producto."
    )
    return result


# --- Programacion -----------------------------------------------------------------------

async def _get_status(session: AsyncSession) -> SupplierSyncStatus | None:
    return await session.scalar(
        select(SupplierSyncStatus).where(SupplierSyncStatus.supplier == SUPPLIER, SupplierSyncStatus.job == JOB)
    )


def is_due(status: SupplierSyncStatus | None, now: datetime) -> bool:
    if status is None or status.last_run_started_at is None:
        return True
    if status.requested_at is not None and status.requested_at > status.last_run_started_at:
        return True
    elapsed = now - status.last_run_started_at
    failed = status.last_error is not None
    return elapsed >= (RETRY_AFTER_FAILURE if failed else RUN_INTERVAL)


async def _record(values: dict) -> None:
    async with AsyncSessionLocal() as session:
        stmt = insert(SupplierSyncStatus).values(supplier=SUPPLIER, job=JOB, **values)
        stmt = stmt.on_conflict_do_update(constraint="uq_supplier_sync_status_job", set_={k: getattr(stmt.excluded, k) for k in values})
        await session.execute(stmt)
        await session.commit()


async def run_if_due() -> None:
    """Job de cada minuto: corre el sync si toca. No-op sin credenciales de URREA."""
    if not urrea_client.is_enabled():
        return
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as session:
        if not is_due(await _get_status(session), now):
            return
    await _record({"last_run_started_at": now})
    path = None
    try:
        path = await urrea_client.download_bulk_catalog()
        async with AsyncSessionLocal() as session:
            result = await sync_bulk_catalog(session, path)
        finished = datetime.now(timezone.utc)
        await _record({
            "last_run_finished_at": finished, "last_success_at": finished, "last_error": None,
            "rows_received": result.received, "rows_matched": result.matched,
            "rows_changed": result.changed, "pim_fields_written": result.pim_fields_written,
        })
    except Exception as e:
        logger.error(f"Fallo el sync de URREA: {e!r}")
        capture_exception(e, job="urrea_sync")
        try:
            await _record({"last_run_finished_at": datetime.now(timezone.utc), "last_error": str(e)[:2000]})
        except Exception as inner:
            logger.error(f"Fallo tambien al registrar el error del sync de URREA: {inner!r}")
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


async def scheduled_urrea_sync_job() -> None:
    try:
        await run_if_due()
    except Exception as e:
        logger.error(f"Fallo en la tarea programada de URREA: {e!r}")
        capture_exception(e, job="urrea_sync")


# --- Simulacion por linea de comandos ---------------------------------------------------

def _fmt(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "as_columns"):
        return " / ".join(f"{k.removeprefix('package_')}={v}" for k, v in value.as_columns().items())
    return str(value)


async def _dry_run(path: str | None, report: str) -> None:
    from app.core.database import engine

    downloaded = None
    try:
        if path is None:
            path = downloaded = await urrea_client.download_bulk_catalog()
        async with AsyncSessionLocal() as session:
            result = await sync_bulk_catalog(session, path, commit=False)
            await session.rollback()
        with open(report, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(["sku", "codigo_urrea", "campo", "anterior", "nuevo"])
            for c in result.pim_changes:
                writer.writerow([c.sku, c.supplier_code, c.field, _fmt(c.old), _fmt(c.new)])
        logger.info(f"Simulacion terminada (sin cambios en la BD). Reporte de PIM: {report} ({len(result.pim_changes)} cambios).")
    finally:
        if downloaded and os.path.exists(downloaded):
            os.unlink(downloaded)
        await engine.dispose()  # sin esto asyncpg puede tronar al cerrar el interprete en Windows


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="Sync del catalogo de URREA (solo simulacion desde la linea de comandos).")
    parser.add_argument("--dry-run", action="store_true", required=True, help="Obligatorio: solo simula, nunca escribe")
    parser.add_argument("--report", default="urrea_cambios_pim.csv", help="CSV con los cambios de PIM que se harian")
    parser.add_argument("--file", help="Masiva ya descargada (JSON); sin esto se descarga de URREA")
    args = parser.parse_args()
    asyncio.run(_dry_run(args.file, args.report))


if __name__ == "__main__":
    main()
