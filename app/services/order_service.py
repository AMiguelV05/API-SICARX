import logging
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.product import Product

logger = logging.getLogger(__name__)

async def validate_cart_items(db: AsyncSession, uuids: list, requested_quantities: dict) -> dict[str, Product]:
    """Valida stock/disponibilidad/precio contra Postgres local, sin llamada a Sicar X. `price <= 0` se trata como no disponible - evita que un producto sincronizado con precio 0.00 se compre gratis."""
    result = await db.execute(
        select(Product).where(
            Product.sicar_uuid.in_(uuids),
            Product.is_deleted == False,
            Product.is_active == True,
        )
    )
    products_by_uuid = {p.sicar_uuid: p for p in result.scalars().all()}

    insufficient = []
    for product_uuid in uuids:
        requested_qty = requested_quantities.get(product_uuid, 0)
        product = products_by_uuid.get(product_uuid)

        if (
            product is None
            or Decimal(str(requested_qty)) > product.available_stock
            or product.price is None
            or product.price <= 0
        ):
            insufficient.append(product_uuid)

    if insufficient:
        logger.warning(f"Carrito rechazado por falta de disponibilidad: {insufficient}")
        raise HTTPException(
            status_code=409,
            detail=f"Los siguientes productos no tienen disponibilidad suficiente: {', '.join(insufficient)}"
        )

    return products_by_uuid

def _to_decimal(value) -> Decimal:
    """Convierte a Decimal via str() para no heredar el error de representación binaria de float."""
    return Decimal(str(value))

def _format_amount(value) -> str:
    return str(_to_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def _format_quantity(value) -> str:
    qty = float(value)
    return str(int(qty)) if qty == int(qty) else str(qty)

def compute_subtotal(local_products: dict, quantities: dict, *, strict: bool = True) -> Decimal:
    """Suma price*quantity sobre `quantities` - misma acumulacion que hace el loop de
    build_order_payload, extraida para reutilizarse donde no hace falta construir tambien
    order_lines (p. ej. el preview de cupon en el carrito). strict=True (default, usado en
    el camino real de creacion de orden) exige que cada uuid tenga un local_product con
    precio > 0 - misma defensa en profundidad de build_order_payload. strict=False (solo
    usado por coupon_service para el preview) ignora en silencio los uuids ausentes/sin
    precio en vez de fallar, ya que un preview de carrito puede incluir productos que ya
    no estan disponibles."""
    total = Decimal("0")
    for product_uuid, quantity in quantities.items():
        local_product = local_products.get(product_uuid)
        if local_product is None or local_product.price is None or local_product.price <= 0:
            if strict:
                logger.error(f"Inconsistencia de datos: falta el precio local del producto {product_uuid}.")
                raise HTTPException(status_code=500, detail="Inconsistencia de datos: falta el precio local de uno o más productos.")
            continue
        total += _to_decimal(local_product.price) * _to_decimal(quantity)
    return total

def build_order_payload(
    local_products: dict,
    quantities: dict,
    delivery_info: dict,
    branch_id: int,
    price_list_uuid: str,
    content_id: str,
    wholesale_prices: bool = False,
    discount_amount: Decimal = Decimal("0"),
) -> dict:
    """Ya NO se envia a Sicar X (solo ERP de inventario, ver CLAUDE.md) - construye el snapshot local de precios/totales/lineas. Conserva el calculo de impuestos original (amountTax = total de linea, no precio unitario) porque sigue siendo la fuente de verdad de como cobrar correctamente.

    `discount_amount` (opcional, cupon ya validado/bloqueado por el llamador - ver
    coupon_service) se resta del subtotal acumulado UNA sola vez, despues de construir las
    lineas, nunca redistribuida entre ellas - `amountTax` de cada linea sigue siendo el
    total de esa linea SIN descuento, el descuento vive solo a nivel de orden
    (`ecOrderDto.total`/`discountAmount`)."""
    order_lines = []
    total = Decimal("0")

    for product_uuid, quantity in quantities.items():
        local_product = local_products.get(product_uuid)

        # Defensa en profundidad: validate_cart_items ya garantiza price > 0; si esto falla es un bug interno, de ahi el 500 (no 502).
        if local_product is None or local_product.price is None or local_product.price <= 0:
            logger.error(f"Inconsistencia de datos: falta el precio local del producto {product_uuid}.")
            raise HTTPException(status_code=500, detail="Inconsistencia de datos: falta el precio local de uno o más productos.")

        net_price_decimal = _to_decimal(local_product.price)
        quantity_decimal = _to_decimal(quantity)
        line_total_decimal = net_price_decimal * quantity_decimal
        total += line_total_decimal

        order_lines.append({
            "uuid": product_uuid,
            "sku": local_product.sku or "",
            "description": local_product.name or "",
            "quantity": _format_quantity(quantity),
            "unit": local_product.unit_short_name or "PZA",
            "priceBaseTax": _format_amount(net_price_decimal),
            "priceTax": _format_amount(net_price_decimal),
            "amountTax": _format_amount(line_total_decimal),
        })

    subtotal = total
    # compute_discount_amount (coupon_service) ya garantiza discount_amount <= subtotal
    # alcanzado por el cupon (y por extension <= subtotal completo); el max(..., 0) de aqui
    # es solo defensa en profundidad, no se espera que dispare nunca.
    total = subtotal - discount_amount
    if total < 0:
        total = Decimal("0")
    total_str = _format_amount(total)
    subtotal_str = _format_amount(subtotal)

    return {
        "contentId": content_id,
        "branchId": branch_id,
        "priceListUuid": price_list_uuid,
        "priceNumber": 1,
        "totalTax": total_str,
        "totalQuantity": _format_quantity(sum(quantities.values())),
        "wholesalePrices": wholesale_prices,
        "ecOrderDto": {
            "uuid": str(uuid4()),
            "timeZone": "America/Mexico_City",
            "type": "SALE",
            "serie": "TL",
            "isoCurrency": "MXN",
            "decimals": 2,
            "opMode": "MX",
            "total": total_str,
            "subtotal": subtotal_str,
            "discountAmount": _format_amount(discount_amount),
            "products": order_lines,
            "ecOrderType": "REMOTE",
            "deliveryInfo": delivery_info,
        },
    }

