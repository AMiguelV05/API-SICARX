"""Peso y medidas de empaque por producto (Product.package_*), para cotizar envios.

Unico lugar con las reglas de validacion: lo usan PUT /admin/products/{uuid}/package-dimensions,
la hoja "Medidas" de la importacion masiva e import_package_dimensions.py. Las cuatro medidas
van juntas o ninguna (ck_products_package_dims_all_or_none)."""
import logging
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product

logger = logging.getLogger(__name__)

# Topes de cordura, no limites de paqueteria: un producto real de este catalogo (cajas
# fuertes, compresores) llega a ~1,100 kg. Lo que pasa de aqui es casi seguro un error de
# captura (p. ej. el largo de un rollo desenrollado, 4500 cm, en vez del empaque).
MAX_WEIGHT_KG = Decimal("1500")
MAX_DIMENSION_CM = Decimal("600")

_WEIGHT_STEP = Decimal("0.001")
_DIMENSION_STEP = Decimal("0.01")
_UNIT_SUFFIX = re.compile(r"\s*(cm|cms|kg|kgs)\.?$", re.IGNORECASE)
_DECIMAL_COMMA = re.compile(r"^\d+,\d{1,2}$|^\d+,\d{4,}$")  # "4,29" si; "1,100" es ambiguo (miles) y se rechaza


@dataclass(frozen=True)
class PackageDimensions:
    weight_kg: Decimal
    length_cm: Decimal
    width_cm: Decimal
    height_cm: Decimal

    def as_columns(self) -> dict[str, Decimal]:
        return {
            "package_weight_kg": self.weight_kg,
            "package_length_cm": self.length_cm,
            "package_width_cm": self.width_cm,
            "package_height_cm": self.height_cm,
        }


def parse_measure(raw) -> Decimal:
    """Celda de Excel / valor JSON -> Decimal. Acepta numeros, texto numerico, sufijo de
    unidad ("16 cm", "0.3 kg") y coma decimal sin ambiguedad ("4,29"). Lanza ValueError."""
    if raw is None or isinstance(raw, bool):
        raise ValueError("valor vacio")
    if isinstance(raw, (int, float, Decimal)):
        text = str(raw)
    else:
        text = _UNIT_SUFFIX.sub("", str(raw).strip())
        if _DECIMAL_COMMA.match(text):
            text = text.replace(",", ".")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"no es un numero: {raw!r}")
    if not value.is_finite():
        raise ValueError(f"no es un numero: {raw!r}")
    return value


def validate_dimensions(weight_kg, length_cm, width_cm, height_cm) -> PackageDimensions:
    """Valores crudos -> PackageDimensions redondeado (kg a 3 decimales, cm a 2,
    ROUND_HALF_UP). Un valor positivo que redondearia a 0 sube al minimo (0.001 kg / 0.01 cm):
    la plantilla de Urrea trae brocas de 0.0001 kg, reales, y perderlas por el redondeo
    tiraria tambien sus medidas. Lanza ValueError con un mensaje en espanol nombrando el campo."""
    parsed = {}
    for name, raw, step, maximum, unit in (
        ("weightKg", weight_kg, _WEIGHT_STEP, MAX_WEIGHT_KG, "kg"),
        ("lengthCm", length_cm, _DIMENSION_STEP, MAX_DIMENSION_CM, "cm"),
        ("widthCm", width_cm, _DIMENSION_STEP, MAX_DIMENSION_CM, "cm"),
        ("heightCm", height_cm, _DIMENSION_STEP, MAX_DIMENSION_CM, "cm"),
    ):
        try:
            value = parse_measure(raw)
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}")
        # Rango antes de quantize: un valor enorme haria fallar quantize con InvalidOperation.
        if value > maximum:
            raise ValueError(f"{name} excede el maximo de {maximum} {unit} (vino {raw!r}).")
        if value <= 0:
            raise ValueError(f"{name} debe ser mayor que 0 (vino {raw!r}).")
        parsed[name] = max(value.quantize(step, rounding=ROUND_HALF_UP), step)
    return PackageDimensions(parsed["weightKg"], parsed["lengthCm"], parsed["widthCm"], parsed["heightCm"])


async def _get_product(db: AsyncSession, product_uuid: str) -> Product:
    product = await db.scalar(select(Product).where(Product.sicar_uuid == product_uuid, Product.is_deleted == False))
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado.")
    return product


async def set_dimensions(db: AsyncSession, product_uuid: str, dims: PackageDimensions) -> Product:
    """Reemplaza las cuatro medidas. 404 si el producto no existe o esta eliminado. Sin commit."""
    product = await _get_product(db, product_uuid)
    for column, value in dims.as_columns().items():
        setattr(product, column, value)
    await db.flush()
    logger.info(f"Producto {product_uuid}: medidas de empaque actualizadas via /admin.")
    return product


async def clear_dimensions(db: AsyncSession, product_uuid: str) -> None:
    """Borra las cuatro medidas. Idempotente. 404 si el producto no existe. Sin commit."""
    product = await _get_product(db, product_uuid)
    for column in PackageDimensions.__dataclass_fields__:
        setattr(product, f"package_{column}", None)
    await db.flush()
    logger.info(f"Producto {product_uuid}: medidas de empaque borradas via /admin.")
