"""Arma las cajas de un envio a partir del peso y las medidas de empaque de cada pieza.

Funcion pura (sin BD ni red). Consolida las piezas en pocas cajas, como empacan en tienda, en
vez de mandar una caja por pieza (que encarece mucho los carritos con piezas chicas). Las
medidas resultantes son una estimacion: el volumen real de una caja nunca es la suma exacta
de los volumenes, de ahi PADDING_FACTOR.
"""
import math
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal

from app.services.package_dimensions_service import PackageDimensions

# Topes por caja consolidada. Una pieza que por si sola los pasa viaja sola.
MAX_BOX_WEIGHT_KG = Decimal("30")
MAX_BOX_VOLUME_CM3 = Decimal(60 * 60 * 60)
# Holgura de volumen (huecos, relleno) cuando la caja lleva mas de una pieza.
PADDING_FACTOR = Decimal("1.15")

_KG_STEP = Decimal("0.001")


@dataclass(frozen=True)
class Box:
    weight_kg: Decimal
    length_cm: Decimal
    width_cm: Decimal
    height_cm: Decimal
    pieces: int

    def as_dict(self) -> dict:
        """Forma que se guarda en shipping_quotes.packages / Order.shipping_quote."""
        return {
            "weightKg": float(self.weight_kg),
            "lengthCm": float(self.length_cm),
            "widthCm": float(self.width_cm),
            "heightCm": float(self.height_cm),
            "pieces": self.pieces,
        }


@dataclass
class _OpenBox:
    weight: Decimal = Decimal("0")
    volume: Decimal = Decimal("0")
    sides: list[Decimal] = field(default_factory=lambda: [Decimal("0")] * 3)  # maximos por lado, de mayor a menor
    pieces: int = 0

    def fits(self, weight: Decimal, volume: Decimal) -> bool:
        return self.weight + weight <= MAX_BOX_WEIGHT_KG and (self.volume + volume) * PADDING_FACTOR <= MAX_BOX_VOLUME_CM3

    def add(self, weight: Decimal, volume: Decimal, sides: tuple[Decimal, Decimal, Decimal]) -> None:
        self.weight += weight
        self.volume += volume
        self.sides = [max(a, b) for a, b in zip(self.sides, sides)]
        self.pieces += 1


def _ceil_cm(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_CEILING)


def _close(box: _OpenBox, single: PackageDimensions | None) -> Box:
    weight = box.weight.quantize(_KG_STEP, rounding=ROUND_CEILING)
    if box.pieces == 1 and single is not None:
        return Box(weight, single.length_cm, single.width_cm, single.height_cm, 1)
    # Largo >= raiz cubica del volumen y ancho/alto derivados del volumen: garantiza
    # largo >= ancho >= alto y que la caja contenga la pieza mas grande en cada eje.
    volume = float(box.volume * PADDING_FACTOR)
    a, b, c = (float(s) for s in box.sides)
    length = max(a, volume ** (1 / 3))
    width = max(b, math.sqrt(volume / length))
    height = max(c, volume / (length * width))
    return Box(weight, _ceil_cm(length), _ceil_cm(width), _ceil_cm(height), box.pieces)


def build_packages(lines: list[tuple[PackageDimensions, int]]) -> list[Box]:
    """`lines` = [(medidas de una pieza, cantidad entera > 0)]. Reparte las piezas en cajas con
    first-fit decreasing por peso, respetando MAX_BOX_WEIGHT_KG y MAX_BOX_VOLUME_CM3 (con
    holgura). Una pieza que no cabe sola en esos topes va en su propia caja con sus medidas."""
    pieces: list[PackageDimensions] = []
    for dims, quantity in lines:
        if quantity <= 0:
            raise ValueError("La cantidad de cada linea debe ser mayor que 0.")
        pieces.extend([dims] * quantity)
    pieces.sort(key=lambda d: d.weight_kg, reverse=True)

    boxes: list[Box] = []
    open_boxes: list[tuple[_OpenBox, PackageDimensions]] = []
    for dims in pieces:
        volume = dims.length_cm * dims.width_cm * dims.height_cm
        if dims.weight_kg > MAX_BOX_WEIGHT_KG or volume > MAX_BOX_VOLUME_CM3:
            boxes.append(Box(dims.weight_kg, dims.length_cm, dims.width_cm, dims.height_cm, 1))
            continue
        sides = tuple(sorted((dims.length_cm, dims.width_cm, dims.height_cm), reverse=True))
        target = next((ob for ob, _ in open_boxes if ob.fits(dims.weight_kg, volume)), None)
        if target is None:
            target = _OpenBox()
            open_boxes.append((target, dims))
        target.add(dims.weight_kg, volume, sides)

    boxes.extend(_close(ob, first) for ob, first in open_boxes)
    return boxes
