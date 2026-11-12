"""Aplica la ficha de URREA a las columnas PIM de products.

Regla (decision de 2026-11-12):
- Primera aplicacion por producto (supplier_products.pim_fields_applied IS NULL): la ficha de
  URREA REEMPLAZA los valores locales en cada campo donde URREA trae dato. Los detalles
  actuales de los productos URREA vienen de la misma fuente y nadie los ha editado. Si URREA
  trae el campo vacio se conserva el local.
  Excepcion: las medidas de empaque solo se llenan si estan vacias, tambien la primera vez. Las
  de la plantilla de distribuidores son mas precisas (peso con 3 decimales; la API lo redondea
  a 2).
- Despues: solo se llenan campos que esten vacios en products y que nunca se hayan aplicado.
  Lo que el admin edite o borre a partir de ahi no se vuelve a tocar.

`technical_specs` no se toca: `caracteristica` llega vacia en el 100% de la masiva, y los
productos ya traen especificaciones de la plantilla de distribuidores que no hay que perder.
Sin commit: el llamador decide."""
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product
from app.services.brand_service import normalize_brand
from app.services.package_dimensions_service import PackageDimensions, validate_dimensions
from app.services.urrea_client import UrreaCatalogRow

# Nombre logico del campo -> columnas de products. Las medidas son un solo campo (van juntas).
TEXT_FIELDS = ("description", "brand", "bullet_points", "contents")
DIMENSIONS_FIELD = "package_dimensions"
DIMENSION_COLUMNS = ("package_weight_kg", "package_length_cm", "package_width_cm", "package_height_cm")
ALL_COLUMNS = TEXT_FIELDS + DIMENSION_COLUMNS
# Campos que nunca se reemplazan, ni en la primera aplicacion.
FILL_ONLY_FIELDS = frozenset({DIMENSIONS_FIELD})

# Un guion que abre una vinieta: al inicio o tras espacio, seguido de texto ("Cromo-Vanadio"
# y "1-9/16" no cuentan).
_BULLET_SPLIT = re.compile(r"(?:^|\s+)-(?=\S)")


def format_bullets(raw: str | None) -> str | None:
    """"-Pulido espejo -Marca Surtek" -> "-Pulido espejo\\n-Marca Surtek" (mismo formato que ya
    tienen las vinietas cargadas desde la plantilla)."""
    if not raw:
        return None
    parts = [p.strip() for p in _BULLET_SPLIT.split(raw.strip())]
    parts = [p for p in parts if p]
    if not parts:
        return None
    return "\n".join(f"-{p}" for p in parts)


def candidate_values(row: UrreaCatalogRow) -> dict[str, Any]:
    """Valores que URREA propone, solo los que trae con dato."""
    values: dict[str, Any] = {}
    if row.description:
        values["description"] = row.description
    brand = normalize_brand(row.brand)
    if brand:
        values["brand"] = brand
    bullets = format_bullets(row.bullets)
    if bullets:
        values["bullet_points"] = bullets
    if row.accessories:
        values["contents"] = row.accessories
    try:
        # fondo = largo del paquete.
        values[DIMENSIONS_FIELD] = validate_dimensions(row.weight_kg, row.depth_cm, row.width_cm, row.height_cm)
    except ValueError:
        pass
    return values


@dataclass(frozen=True)
class PimChange:
    product_id: int
    sku: str | None
    supplier_code: str
    field: str
    old: Any
    new: Any


def _current_value(current: dict, field: str) -> Any:
    if field == DIMENSIONS_FIELD:
        if current.get("package_weight_kg") is None:
            return None
        return PackageDimensions(*(current[c] for c in DIMENSION_COLUMNS))
    return current.get(field)


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


async def apply_supplier_info(
    db: AsyncSession, items: list[tuple[int, list[str] | None, UrreaCatalogRow]]
) -> tuple[list[PimChange], dict[int, list[str]]]:
    """items = [(product_id, pim_fields_applied actual, fila de URREA)]. Escribe solo los
    productos que cambian. Devuelve (cambios hechos, pim_fields_applied nuevo por producto -
    solo los que cambiaron)."""
    if not items:
        return [], {}
    product_ids = [pid for pid, _, _ in items]
    result = await db.execute(
        select(Product.id, Product.sku, *[getattr(Product, c) for c in ALL_COLUMNS]).where(Product.id.in_(product_ids))
    )
    current_by_id = {row[0]: {"sku": row[1], **dict(zip(ALL_COLUMNS, row[2:]))} for row in result.all()}

    changes: list[PimChange] = []
    applied_updates: dict[int, list[str]] = {}
    rows_to_update: list[dict] = []
    for product_id, applied, row in items:
        current = current_by_id.get(product_id)
        if current is None:
            continue
        first_time = applied is None
        applied_set = set(applied or [])
        new_columns: dict[str, Any] = {}
        for field, new_value in candidate_values(row).items():
            old_value = _current_value(current, field)
            replace = first_time and field not in FILL_ONLY_FIELDS
            if not replace and (field in applied_set or not _is_empty(old_value)):
                if first_time:
                    # Ofrecido y descartado: el valor local manda desde ahora.
                    applied_set.add(field)
                continue
            applied_set.add(field)
            if old_value == new_value:
                continue
            changes.append(PimChange(product_id, current["sku"], row.code, field, old_value, new_value))
            if field == DIMENSIONS_FIELD:
                new_columns.update(new_value.as_columns())
            else:
                new_columns[field] = new_value

        new_applied = sorted(applied_set)
        if first_time or new_applied != sorted(applied or []):
            applied_updates[product_id] = new_applied
        if new_columns:
            merged = {c: current[c] for c in ALL_COLUMNS}
            merged.update(new_columns)
            rows_to_update.append({"pid": product_id, **merged})

    if rows_to_update:
        stmt = (
            update(Product)
            .where(Product.id == bindparam("pid"))
            .values({c: bindparam(c) for c in ALL_COLUMNS})
            .execution_options(dml_strategy="core_only")
        )
        await db.execute(stmt, rows_to_update)
    return changes, applied_updates
