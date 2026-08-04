import logging
import re
from dataclasses import dataclass, field
from io import BytesIO
from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font
from fastapi import HTTPException, status
from sqlalchemy import select, func, or_, and_, cast
from sqlalchemy.dialects.postgresql import insert as pg_insert, JSONB, array as pg_array
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.product import Product
from app.models.taxonomy import Category, product_categories
from app.models.vehicle import Vehicle, product_vehicles

logger = logging.getLogger(__name__)

MAX_ROWS_PER_SHEET = 20_000
MAX_FILE_SIZE_BYTES = 4 * 1024 * 1024  # 4 MB - limite crudo antes de siquiera abrir el workbook
MAX_VEHICLE_COMBOS_PER_QUERY = 300  # troceo defensivo, ver _resolve_vehicle_combos

CATEGORIES_SHEET = "Categorias"
VEHICLES_SHEET = "Vehiculos"
CATEGORIES_REQUIRED_COLUMNS = ("sku", "categorySlug")
VEHICLES_REQUIRED_COLUMNS = ("sku", "make", "model", "year")  # vehicleType/engine son opcionales


@dataclass
class _RowError:
    row: int
    reason_code: str
    reason: str
    sku: str | None = None


@dataclass
class _SheetOutcome:
    found: bool = False
    processed_rows: int = 0
    assigned_count: int = 0
    errors: list[_RowError] = field(default_factory=list)


@dataclass
class BulkImportOutcome:
    categories: _SheetOutcome
    vehicles: _SheetOutcome


def _load_workbook(file_bytes: bytes):
    """Un archivo invalido/corrupto lanza distintas excepciones segun el caso - todas se
    capturan como un 400 uniforme."""
    try:
        return load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as exc:
        logger.warning(f"Bulk import: archivo invalido/corrupto ({exc}).")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Archivo invalido o corrupto - se espera un .xlsx valido.")


def _read_sheet(wb, sheet_name: str, required_columns: tuple[str, ...]) -> tuple[bool, list[tuple[int, dict]]]:
    """Devuelve (existe_la_hoja, filas), fila = (numero_de_fila_excel, dict columna->valor).
    Falta de la hoja no es error (puede traerse solo una de las dos); hoja presente sin
    columnas requeridas si lo es (422, archivo completo). Limite de filas se cuenta
    iterando, no via ws.max_row (impreciso en modo read_only en archivos editados a mano)."""
    if sheet_name not in wb.sheetnames:
        return False, []
    ws = wb[sheet_name]
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header = next(rows_iter)
    except StopIteration:
        return True, []  # hoja presente pero completamente vacia (ni header)

    header_map = {str(h).strip(): idx for idx, h in enumerate(header) if h is not None}
    missing = [c for c in required_columns if c not in header_map]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Hoja '{sheet_name}': faltan columnas requeridas: {', '.join(missing)}.",
        )

    rows: list[tuple[int, dict]] = []
    for excel_row_num, raw_row in enumerate(rows_iter, start=2):
        if all(v is None for v in raw_row):
            continue  # fila completamente vacia - se ignora, no cuenta para el limite ni se reporta
        if len(rows) >= MAX_ROWS_PER_SHEET:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Hoja '{sheet_name}': excede el limite de {MAX_ROWS_PER_SHEET} filas de datos.",
            )
        row_dict = {col: raw_row[idx] if idx < len(raw_row) else None for col, idx in header_map.items()}
        rows.append((excel_row_num, row_dict))
    return True, rows


def _clean_str(value) -> str:
    return str(value).strip() if value is not None else ""


def _split_slugs(value) -> list[str]:
    """`categorySlug` acepta uno o varios slugs separados por coma/punto y coma en la misma
    celda; sin delimitador se devuelve como lista de un solo elemento (compatible con el
    formato de una categoria por fila)."""
    raw = _clean_str(value)
    if not raw:
        return []
    return [token.strip() for token in re.split(r"[,;]", raw) if token.strip()]


async def _resolve_products(db: AsyncSession, skus: set[str]) -> dict[str, int]:
    """sku (case-insensitive) -> Product.id, solo productos activos. Resuelve primero por
    Product.sku exacto y luego, para los que quedan sin resolver, hace fallback contra
    additional_skus (JSON, casteado a JSONB inline)."""
    if not skus:
        return {}

    upper_skus = {s.upper() for s in skus}
    result = await db.execute(
        select(Product.id, Product.sku).where(func.upper(Product.sku).in_(upper_skus), Product.is_deleted == False)
    )
    found = {row.sku.upper(): row.id for row in result.all()}

    remaining = upper_skus - found.keys()
    if remaining:
        fallback = await db.execute(
            select(Product.id, Product.additional_skus).where(
                Product.is_deleted == False,
                Product.additional_skus.isnot(None),
                cast(Product.additional_skus, JSONB).has_any(pg_array(sorted(remaining))),
            )
        )
        for row in fallback.all():
            for candidate_sku in (row.additional_skus or []):
                upper_candidate = candidate_sku.upper()
                if upper_candidate in remaining and upper_candidate not in found:
                    found[upper_candidate] = row.id
    return found


async def _resolve_categories(db: AsyncSession, slugs: set[str]) -> dict[str, str]:
    """slug (case-sensitive - ya vienen normalizados en minuscula por el slugify existente) ->
    Category.uuid, un solo IN (...) (tabla chica, decenas de filas)."""
    if not slugs:
        return {}
    result = await db.execute(select(Category.uuid, Category.slug).where(Category.slug.in_(slugs)))
    return {row.slug: row.uuid for row in result.all()}


async def _resolve_vehicle_combos(
    db: AsyncSession, combos: set[tuple[str | None, str, str, str | None]]
) -> dict[tuple[str | None, str, str, str | None], list[Vehicle]]:
    """combo = (vehicleType|None, make, model, engine|None), comparado case-insensitive ->
    todos los fitments que coinciden para cualquier anio (el filtro por anio se aplica
    despues, por fila, en _vehicles_matching_year). Una sola consulta (OR de ANDs) para
    toda la hoja, troceada en bloques de MAX_VEHICLE_COMBOS_PER_QUERY solo como
    salvaguarda."""
    if not combos:
        return {}

    combos_list = sorted(combos, key=lambda c: (c[1], c[2], c[0] or "", c[3] or ""))
    all_vehicles: list[Vehicle] = []
    for i in range(0, len(combos_list), MAX_VEHICLE_COMBOS_PER_QUERY):
        chunk = combos_list[i : i + MAX_VEHICLE_COMBOS_PER_QUERY]
        conditions = []
        for vt, make, model, engine in chunk:
            cond = and_(func.upper(Vehicle.make) == make.upper(), func.upper(Vehicle.model) == model.upper())
            if vt:
                cond = and_(cond, Vehicle.vehicle_type == vt)
            if engine:
                cond = and_(cond, func.upper(Vehicle.engine) == engine.upper())
            conditions.append(cond)
        result = await db.execute(select(Vehicle).where(or_(*conditions)))
        all_vehicles.extend(result.scalars().all())

    # Re-agrupa en memoria porque el OR compartido no indica de que combo vino cada fila.
    by_combo: dict[tuple, list[Vehicle]] = {c: [] for c in combos}
    for v in all_vehicles:
        for vt, make, model, engine in combos:
            if (
                v.make.upper() == make.upper()
                and v.model.upper() == model.upper()
                and (not vt or v.vehicle_type == vt)
                and (not engine or (v.engine or "").upper() == engine.upper())
            ):
                by_combo[(vt, make, model, engine)].append(v)
    return by_combo


def _vehicles_matching_year(vehicles: list[Vehicle], year: int) -> list[Vehicle]:
    return [v for v in vehicles if v.year_start <= year and (v.year_end is None or v.year_end >= year)]


async def _bulk_insert_pairs(db: AsyncSession, table, pairs: list[dict], conflict_cols: list[str], returning_col) -> int:
    """ON CONFLICT DO NOTHING hace esto aditivo e idempotente - subir el mismo archivo dos
    veces no duplica ni falla."""
    if not pairs:
        return 0
    stmt = pg_insert(table).values(pairs).on_conflict_do_nothing(index_elements=conflict_cols).returning(returning_col)
    result = await db.execute(stmt)
    return len(result.all())


def _process_categories_rows(
    rows: list[tuple[int, dict]], product_map: dict[str, int], category_map: dict[str, str]
) -> tuple[list[dict], list[_RowError]]:
    """Cada slug de `categorySlug` se resuelve de forma independiente - un slug invalido no
    bloquea los demas de la misma fila."""
    pairs, errors = [], []
    seen = set()
    for row_num, data in rows:
        sku = _clean_str(data.get("sku"))
        slugs = _split_slugs(data.get("categorySlug"))
        if not sku or not slugs:
            errors.append(_RowError(row_num, "MISSING_FIELDS", "Fila incompleta: falta sku o categorySlug.", sku or None))
            continue
        product_id = product_map.get(sku.upper())
        if product_id is None:
            errors.append(_RowError(row_num, "SKU_NOT_FOUND", f"SKU no encontrado: {sku}.", sku))
            continue
        for slug in slugs:
            category_uuid = category_map.get(slug)
            if category_uuid is None:
                errors.append(_RowError(row_num, "CATEGORY_SLUG_NOT_FOUND", f"Categoria con slug no encontrado: {slug}.", sku))
                continue
            key = (category_uuid, product_id)
            if key not in seen:
                seen.add(key)
                pairs.append({"category_uuid": category_uuid, "product_id": product_id})
    return pairs, errors


def _process_vehicles_rows(
    rows: list[tuple[int, dict]],
    product_map: dict[str, int],
    combo_map: dict[tuple, list[Vehicle]],
) -> tuple[list[dict], list[_RowError]]:
    pairs, errors = [], []
    seen = set()
    for row_num, data in rows:
        sku = _clean_str(data.get("sku"))
        make = _clean_str(data.get("make"))
        model = _clean_str(data.get("model"))
        vehicle_type = _clean_str(data.get("vehicleType")) or None
        engine = _clean_str(data.get("engine")) or None
        year_raw = data.get("year")

        if not sku or not make or not model or year_raw is None:
            errors.append(_RowError(row_num, "MISSING_FIELDS", "Fila incompleta: falta sku, make, model o year.", sku or None))
            continue
        try:
            year = int(year_raw)
        except (TypeError, ValueError):
            errors.append(_RowError(row_num, "INVALID_YEAR", f"year invalido (no es un entero): {year_raw!r}.", sku))
            continue

        product_id = product_map.get(sku.upper())
        if product_id is None:
            errors.append(_RowError(row_num, "SKU_NOT_FOUND", f"SKU no encontrado: {sku}.", sku))
            continue

        candidates = combo_map.get((vehicle_type, make, model, engine), [])
        matches = _vehicles_matching_year(candidates, year)
        if not matches:
            suffix = f" motor={engine}" if engine else ""
            errors.append(_RowError(
                row_num, "VEHICLE_NOT_FOUND",
                f"No se encontro ningun vehiculo para {make} {model} {year}{suffix}.", sku,
            ))
            continue

        for v in matches:
            key = (v.uuid, product_id)
            if key not in seen:
                seen.add(key)
                pairs.append({"vehicle_uuid": v.uuid, "product_id": product_id})
    return pairs, errors


async def import_bulk_assignments(db: AsyncSession, file_bytes: bytes) -> BulkImportOutcome:
    """Orquestador: parsea el .xlsx, resuelve productos/categorias/vehiculos en un numero
    constante de consultas, y hace un solo commit final (aditivo, idempotente). Las
    validaciones por fila nunca tocan la base de datos."""
    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Archivo demasiado grande (limite {MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB).",
        )

    wb = _load_workbook(file_bytes)
    try:
        cat_found, cat_rows = _read_sheet(wb, CATEGORIES_SHEET, CATEGORIES_REQUIRED_COLUMNS)
        veh_found, veh_rows = _read_sheet(wb, VEHICLES_SHEET, VEHICLES_REQUIRED_COLUMNS)
    finally:
        wb.close()

    if not cat_found and not veh_found:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"El archivo no contiene ninguna hoja '{CATEGORIES_SHEET}' ni '{VEHICLES_SHEET}'.",
        )

    all_skus = {_clean_str(r.get("sku")) for _, r in cat_rows if _clean_str(r.get("sku"))} | \
               {_clean_str(r.get("sku")) for _, r in veh_rows if _clean_str(r.get("sku"))}
    product_map = await _resolve_products(db, all_skus)

    category_slugs: set[str] = set()
    for _, r in cat_rows:
        category_slugs.update(_split_slugs(r.get("categorySlug")))
    category_map = await _resolve_categories(db, category_slugs)

    vehicle_combos: set[tuple[str | None, str, str, str | None]] = set()
    for _, r in veh_rows:
        make = _clean_str(r.get("make"))
        model = _clean_str(r.get("model"))
        if make and model:
            vt = _clean_str(r.get("vehicleType")) or None
            eng = _clean_str(r.get("engine")) or None
            vehicle_combos.add((vt, make, model, eng))
    combo_map = await _resolve_vehicle_combos(db, vehicle_combos)

    cat_pairs, cat_errors = _process_categories_rows(cat_rows, product_map, category_map)
    veh_pairs, veh_errors = _process_vehicles_rows(veh_rows, product_map, combo_map)

    cat_assigned = await _bulk_insert_pairs(db, product_categories, cat_pairs, ["category_uuid", "product_id"], product_categories.c.category_uuid)
    veh_assigned = await _bulk_insert_pairs(db, product_vehicles, veh_pairs, ["vehicle_uuid", "product_id"], product_vehicles.c.vehicle_uuid)
    await db.commit()

    logger.info(
        f"Bulk import via /admin: Categorias={len(cat_rows)} filas/{cat_assigned} nuevos/{len(cat_errors)} errores, "
        f"Vehiculos={len(veh_rows)} filas/{veh_assigned} nuevos/{len(veh_errors)} errores."
    )
    return BulkImportOutcome(
        categories=_SheetOutcome(cat_found, len(cat_rows), cat_assigned, cat_errors),
        vehicles=_SheetOutcome(veh_found, len(veh_rows), veh_assigned, veh_errors),
    )


def _write_header(ws, columns: tuple[str, ...], comments: dict[str, str]) -> None:
    """Reusa exactamente los nombres de columna que `_read_sheet` espera, para que la
    plantilla nunca se desalinee del lado que la lee."""
    for idx, name in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=idx, value=name)
        cell.font = Font(bold=True)
        if name in comments:
            cell.comment = Comment(comments[name], "API SICARX")
        ws.column_dimensions[cell.column_letter].width = max(14, len(name) + 4)


def build_template_workbook() -> bytes:
    """Mismas hojas/columnas que `_read_sheet` exige. Filas de ejemplo con datos ficticios -
    si un admin la sube sin editarla, cada fila sale como error por fila, no fatal (el
    diseno de exito parcial de import_bulk_assignments ya cubre ese caso)."""
    wb = Workbook()

    ws_cat = wb.active
    ws_cat.title = CATEGORIES_SHEET
    _write_header(ws_cat, CATEGORIES_REQUIRED_COLUMNS, {
        "categorySlug": "Slug (no nombre ni uuid) de una categoria ya existente - ver GET /taxonomy o GET /admin/categories. Para asignar varias categorias al mismo producto en una sola fila, separalas por coma o punto y coma (p. ej. \"categoria-ejemplo-1, categoria-ejemplo-2\").",
    })
    ws_cat.append(["SKU-EJEMPLO-1", "categoria-ejemplo-1"])
    ws_cat.append(["SKU-EJEMPLO-1", "categoria-ejemplo-2, categoria-ejemplo-3"])

    ws_veh = wb.create_sheet(VEHICLES_SHEET)
    veh_columns = VEHICLES_REQUIRED_COLUMNS + ("vehicleType", "engine")
    _write_header(ws_veh, veh_columns, {
        "make": "No distingue mayusculas/minusculas (igual que model, engine y sku).",
        "model": "No distingue mayusculas/minusculas (igual que make, engine y sku).",
        "year": "Un solo anio (no un rango yearStart/yearEnd) - se compara contra el rango de los fitments ya existentes en 'vehicles'.",
        "vehicleType": "Opcional: AUTOMOTIVE o MOTORCYCLE. Dejar vacio si no hace falta desambiguar.",
        "engine": "Opcional - dejar vacio para aplicar a TODAS las variantes de motor de esa marca/modelo/anio.",
    })
    # Orden de columnas: sku, make, model, year, vehicleType, engine (ver veh_columns arriba)
    ws_veh.append(["SKU-EJEMPLO-2", "Marca-Ejemplo", "Modelo-Ejemplo", 2020, None, None])
    ws_veh.append(["SKU-EJEMPLO-2", "Marca-Ejemplo", "Modelo-Ejemplo", 2020, "AUTOMOTIVE", "L4 1.6L"])

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
