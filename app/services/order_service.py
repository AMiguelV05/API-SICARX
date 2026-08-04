import logging
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.product import Product

logger = logging.getLogger(__name__)

async def validate_cart_items(db: AsyncSession, uuids: list, requested_quantities: dict) -> dict[str, Product]:
    """Valida stock/disponibilidad/precio localmente contra Postgres - ya no hace ninguna
    llamada de red a Sicar X (ver CLAUDE.md, eliminacion de la dependencia del token de
    sesion del cliente web). Devuelve los Product locales indexados por sicar_uuid;
    build_order_payload los consume directamente en vez de una respuesta GraphQL.

    `product.price <= 0` se trata como no disponible, no como precio legitimo: un producto
    sincronizado con precio 0.00 (por ejemplo si Sicar X no devolvio ninguna entrada en su
    diccionario de precios para ese producto durante el sync - caso real confirmado en
    sync.log) no debe poder comprarse gratis."""
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
            or product.stock is None
            or Decimal(str(requested_qty)) > product.stock
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

def build_order_payload(
    local_products: dict,
    quantities: dict,
    delivery_info: dict,
    branch_id: int,
    price_list_uuid: str,
    content_id: str,
    wholesale_prices: bool = False,
) -> dict:
    """
    Construye la estructura de la orden a partir de datos ya obtenidos (no hace llamadas
    de red). Ya NO se envia a Sicar X — desde que SICAR pasó a ser solo ERP de inventario
    (ver CLAUDE.md, "Request flow: placing an order"), esto sirve unicamente para calcular
    precios/totales/lineas y darle forma al snapshot local (`order_history_service.create_local_order`).
    Conserva el formato del documento que Sicar X SÍ esperaba cuando esto se le enviaba
    directamente, ya que replicar ese cálculo (impuestos, `amountTax` como total de línea
    y no precio unitario, etc.) sigue siendo la fuente de verdad de cómo se cobra
    correctamente — ver la nota histórica sobre el bug de "precio alterado" en CLAUDE.md.

    Ya no recibe una respuesta GraphQL en vivo (`cart_data`) - todo campo por línea sale
    ahora del snapshot local ya sincronizado por `sync_task.py`/`fetch_full_details_from_sicar`
    (el `Product` de `local_products`, ya validado por `validate_cart_items` antes de esta
    llamada). `type`/`taxesIds` (el código de tipo de producto y los IDs de impuesto de
    Sicar X) se eliminaron por completo de la línea - eran artefactos exclusivos del
    documento de orden que Sicar X ya no recibe, no algo que este backend o el frontend
    usen para calcular o mostrar nada (confirmado: `amountTax` sale solo de precio ×
    cantidad, y el contrato documentado en FRONTEND_INTEGRATION.md nunca los incluye).
    """
    order_lines = []
    total = Decimal("0")

    for product_uuid, quantity in quantities.items():
        local_product = local_products.get(product_uuid)

        # Defensa en profundidad: validate_cart_items ya garantiza price > 0 para cada
        # uuid en quantities antes de que esta funcion corra, asi que esto es ahora una
        # violacion de invariante interno (no una falla de un proveedor externo) si
        # llegara a pasar - de ahi el 500, no el 502 que se usaba cuando el precio venia
        # de una respuesta en vivo de Sicar X.
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

    total_str = _format_amount(total)

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
            "products": order_lines,
            "ecOrderType": "REMOTE",
            "deliveryInfo": delivery_info,
        },
    }

