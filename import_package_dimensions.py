"""Carga peso y medidas de empaque desde la plantilla de distribuidores de Urrea
("Plantilla para distribuidores <MES> <AA>.xlsx") a Product.package_*. Script suelto, no lo
importa la app.

    python import_package_dimensions.py "Plantilla.xlsx"                  # simulacion contra DATABASE_URL, no escribe
    python import_package_dimensions.py "Plantilla.xlsx" --apply          # escribe en DATABASE_URL
    python import_package_dimensions.py "Plantilla.xlsx" --out medidas.xlsx

--out genera un .xlsx con la hoja "Medidas" para subirlo en POST /v1/admin/bulk-import/products
(la forma de cargarlo en produccion sin conexion directa a la BD). --apply usa las mismas
funciones que esa importacion, asi que ambos caminos validan y escriben igual.

Emparejamiento: en Sicar X los productos de Urrea/Surtek/Foy/Lock/Prolok tienen sku =
"U" + Codigo de la plantilla (99% de coincidencia en el catalogo de nov 2026, sin conflictos
contra el EAN). Comparar el Codigo crudo contra sku es INCORRECTO: choca con productos de
otras marcas que comparten el mismo codigo numerico. Si "U"+Codigo no resuelve, se intenta el
EAN/UPC contra sku y additional_skus.

Las medidas de la plantilla son del empaque de UNA pieza (p. ej. la espuma de 500 ml con
multiplo de venta 12 trae 6.5 x 6.5 x 24 cm, 0.6 kg). Filas con "Pendiente", 0, rangos
("31-33.7") o fuera de los topes de package_dimensions_service se omiten y salen en el
reporte CSV.
"""
import argparse
import asyncio
import csv
import logging
import sys
import unicodedata
from dataclasses import dataclass

from openpyxl import Workbook, load_workbook

from app.services.package_dimensions_service import PackageDimensions, validate_dimensions

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("import_package_dimensions")

# Encabezados de la plantilla, normalizados con _norm (sin acentos, minusculas, espacios colapsados).
COL_CODE = "codigo"
COL_EAN = "ean o upc"
COL_LENGTH = "largo (paquete) cm"
COL_WIDTH = "ancho (paquete) cm"
COL_HEIGHT = "alto paquete cm"
COL_WEIGHT = "peso (paquete) kg"
REQUIRED = (COL_CODE, COL_LENGTH, COL_WIDTH, COL_HEIGHT, COL_WEIGHT)


@dataclass
class SourceRow:
    excel_row: int
    code: str
    ean: str | None
    dims: PackageDimensions | None
    error: str | None


def _norm(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return " ".join(text.lower().split())


def read_template(path: str) -> list[SourceRow]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header = {_norm(h): i for i, h in enumerate(next(rows)) if h is not None}
        missing = [c for c in REQUIRED if c not in header]
        if missing:
            sys.exit(f"La hoja '{ws.title}' no tiene las columnas: {', '.join(missing)}")

        out = []
        for excel_row, raw in enumerate(rows, start=2):
            def cell(col):
                idx = header.get(col)
                return raw[idx] if idx is not None and idx < len(raw) else None

            code = str(cell(COL_CODE) or "").strip()
            if not code:
                continue
            ean = str(cell(COL_EAN) or "").strip() or None
            try:
                dims = validate_dimensions(cell(COL_WEIGHT), cell(COL_LENGTH), cell(COL_WIDTH), cell(COL_HEIGHT))
                out.append(SourceRow(excel_row, code, ean, dims, None))
            except ValueError as exc:
                out.append(SourceRow(excel_row, code, ean, None, str(exc)))
        return out
    finally:
        wb.close()


def write_bulk_import_workbook(rows: list[SourceRow], prefix: str, path: str) -> None:
    from app.services.bulk_import_service import PACKAGE_DIMENSIONS_REQUIRED_COLUMNS, PACKAGE_DIMENSIONS_SHEET

    wb = Workbook()
    ws = wb.active
    ws.title = PACKAGE_DIMENSIONS_SHEET
    ws.append(list(PACKAGE_DIMENSIONS_REQUIRED_COLUMNS))
    for r in rows:
        if r.dims:
            ws.append([f"{prefix}{r.code}", float(r.dims.weight_kg), float(r.dims.length_cm), float(r.dims.width_cm), float(r.dims.height_cm)])
    wb.save(path)


async def match_and_write(rows: list[SourceRow], prefix: str, report: list[dict], commit: bool) -> None:
    """Empareja contra el catalogo y escribe; con commit=False hace rollback (simulacion)."""
    from app.core.database import engine

    try:
        await _match_and_write(rows, prefix, report, commit)
    finally:
        await engine.dispose()  # sin esto asyncpg puede tronar (segfault) al cerrar el interprete en Windows


async def _match_and_write(rows: list[SourceRow], prefix: str, report: list[dict], commit: bool) -> None:
    from app.core.database import AsyncSessionLocal
    from app.services.bulk_import_service import _apply_package_dimensions_updates
    from app.services.urrea_matching import match_codes

    valid = [r for r in rows if r.dims]
    async with AsyncSessionLocal() as db:
        matches = await match_codes(db, [(r.code, r.ean) for r in valid], prefix)

        updates: dict[int, PackageDimensions] = {}
        source_of: dict[int, SourceRow] = {}
        for r in valid:
            product_id = matches[r.code][0] if r.code in matches else None
            if product_id is None:
                report.append({"fila": r.excel_row, "codigo": r.code, "motivo": "SIN_PRODUCTO", "detalle": f"Ni {prefix}{r.code} ni el EAN {r.ean} existen en el catalogo."})
                continue
            previous = source_of.get(product_id)
            if previous and previous.dims != r.dims:
                report.append({"fila": r.excel_row, "codigo": r.code, "motivo": "DUPLICADO", "detalle": f"Mismo producto que la fila {previous.excel_row} con otras medidas; gana esta fila."})
            updates[product_id] = r.dims
            source_of[product_id] = r

        changed = await _apply_package_dimensions_updates(db, updates)
        if commit:
            await db.commit()
        else:
            await db.rollback()
    verb = "escritos" if commit else "se escribirian (simulacion, sin cambios)"
    logger.info(f"Productos emparejados: {len(updates)}; con medidas nuevas o distintas, {verb}: {changed}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Carga peso y medidas de empaque desde la plantilla de distribuidores de Urrea.")
    parser.add_argument("template", help="Ruta del .xlsx del distribuidor")
    parser.add_argument("--sku-prefix", default="U", help='Prefijo del sku en Sicar X (default "U")')
    parser.add_argument("--apply", action="store_true", help="Escribir en la BD de DATABASE_URL (sin esto solo simula)")
    parser.add_argument("--out", help='Genera un .xlsx con la hoja "Medidas" para la importacion masiva del admin')
    parser.add_argument("--report", default="reporte_medidas.csv", help="CSV con las filas omitidas (default reporte_medidas.csv)")
    args = parser.parse_args()

    rows = read_template(args.template)
    report = [{"fila": r.excel_row, "codigo": r.code, "motivo": "MEDIDAS_INVALIDAS", "detalle": r.error} for r in rows if r.error]
    logger.info(f"Filas leidas: {len(rows)}; con medidas validas: {len(rows) - len(report)}; omitidas por medidas invalidas: {len(report)}.")

    if args.out:
        write_bulk_import_workbook(rows, args.sku_prefix, args.out)
        logger.info(f"Archivo para la importacion masiva: {args.out}")
    if args.apply or not args.out:
        asyncio.run(match_and_write(rows, args.sku_prefix, report, commit=args.apply))

    with open(args.report, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=["fila", "codigo", "motivo", "detalle"])
        writer.writeheader()
        writer.writerows(sorted(report, key=lambda x: x["fila"]))
    logger.info(f"Reporte de filas omitidas ({len(report)}): {args.report}")


if __name__ == "__main__":
    main()
