import httpx
import json
import logging
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
from fastapi import HTTPException
from app.core.sicar_headers import storefront_headers
from app.core.sicar_validation import is_safe_sicar_id
from app.core.upstream_errors import raise_upstream_error

STORE_URL = "https://api.sicarx.com/store/"
SICAR_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)

logger = logging.getLogger(__name__)

async def validate_cart_items(uuids: list, requested_quantities: dict, token: str, branch_id: str, price_list_uuid: str):
    """Validación de stock y precios usando el token del cliente web"""
    if not is_safe_sicar_id(price_list_uuid) or not all(is_safe_sicar_id(u) for u in uuids):
        raise HTTPException(status_code=400, detail="Uno o más identificadores de producto o de lista de precios no son válidos.")
    safe_price_list_uuid = price_list_uuid
    safe_uuids = uuids

    graphql_uuids = json.dumps(safe_uuids)

    query = f"""{{
        products(uuids:{graphql_uuids}, branchId: {branch_id}, priceListId: "{safe_price_list_uuid}") {{
            available
            stock
            lot
            uuid
            type
            priceList {{
                netPrice1
                price1
                saleTaxes
                iso
                productUuid
                priceListUuid
            }}
        }}
        stockForProducts(uuids: {graphql_uuids}) {{
            uuid
            stock
        }}
        content {{
            units {{
                uuid
                shortName
            }}
        }}
    }}"""

    headers = storefront_headers(token, content_type="application/graphql", branch_id=branch_id)

    async with httpx.AsyncClient(timeout=SICAR_TIMEOUT) as client:
        response = await client.post(STORE_URL, content=query, headers=headers)
        if response.status_code != 200:
            raise_upstream_error(response, "Error en pre-validacion de carrito en Sicar", "No se pudo validar el carrito con Sicar X. Intenta nuevamente.")
        payload = response.json()

    if "errors" in payload:
        raise_upstream_error(response, "Errores GraphQL en pre-validacion de carrito", "No se pudo validar el carrito con Sicar X. Intenta nuevamente.")

    data = payload.get("data", payload)
    products = data.get("products") or []
    stock_for_products = data.get("stockForProducts") or []
    products_by_uuid = {p.get("uuid"): p for p in products if isinstance(p, dict)}
    stock_by_uuid = {s.get("uuid"): s.get("stock") for s in stock_for_products if isinstance(s, dict)}

    # Verificamos disponibilidad y stock suficiente para cada producto solicitado.
    insufficient = []
    for product_uuid in safe_uuids:
        requested_qty = requested_quantities.get(product_uuid, 0)
        product_info = products_by_uuid.get(product_uuid)

        if not product_info:
            insufficient.append(product_uuid)
            continue

        if product_info.get("available") is False:
            insufficient.append(product_uuid)
            continue

        available_stock = stock_by_uuid.get(product_uuid, product_info.get("stock"))
        if available_stock is not None and requested_qty > float(available_stock):
            insufficient.append(product_uuid)

    if insufficient:
        logger.warning(f"Carrito rechazado por falta de disponibilidad: {insufficient}")
        raise HTTPException(
            status_code=409,
            detail=f"Los siguientes productos no tienen disponibilidad suficiente: {', '.join(insufficient)}"
        )

    return data

def _to_decimal(value) -> Decimal:
    """Convierte a Decimal via str() para no heredar el error de representación binaria de float."""
    return Decimal(str(value))

def _format_amount(value) -> str:
    return str(_to_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def _format_quantity(value) -> str:
    qty = float(value)
    return str(int(qty)) if qty == int(qty) else str(qty)

def build_order_payload(
    cart_data: dict,
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
    """
    products_by_uuid = {p.get("uuid"): p for p in (cart_data.get("products") or []) if isinstance(p, dict)}
    units_by_uuid = {
        u.get("uuid"): u.get("shortName")
        for u in (cart_data.get("content") or {}).get("units") or []
        if isinstance(u, dict)
    }

    order_lines = []
    total = Decimal("0")

    for product_uuid, quantity in quantities.items():
        sicar_info = products_by_uuid.get(product_uuid) or {}
        local_product = local_products.get(product_uuid)
        price_list = sicar_info.get("priceList") or {}

        net_price = price_list.get("netPrice1")
        if net_price is None:
            logger.error(f"Sicar no devolvio precio para el producto {product_uuid}.")
            raise HTTPException(status_code=502, detail="No se pudo obtener el precio de uno o más productos.")

        net_price_decimal = _to_decimal(net_price)
        quantity_decimal = _to_decimal(quantity)
        line_total_decimal = net_price_decimal * quantity_decimal
        total += line_total_decimal
        sales_unit_uuid = local_product.sales_unit_uuid if local_product else None

        order_lines.append({
            "uuid": product_uuid,
            "type": sicar_info.get("type", 0),
            "sku": local_product.sku if local_product else "",
            "description": local_product.name if local_product else "",
            "quantity": _format_quantity(quantity),
            "unit": units_by_uuid.get(sales_unit_uuid, "PZA"),
            "priceBaseTax": _format_amount(net_price_decimal),
            "priceTax": _format_amount(net_price_decimal),
            "amountTax": _format_amount(line_total_decimal),
            "taxesIds": price_list.get("saleTaxes") or [],
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

