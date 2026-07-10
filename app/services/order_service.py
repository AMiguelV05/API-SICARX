import httpx
import json
import logging
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update
from app.models.product import Product
from app.services.sicar_auth import sicar_auth
from app.core.sicar_headers import storefront_headers, admin_app_headers
from app.core.sicar_validation import is_safe_sicar_id

STORE_URL = "https://api.sicarx.com/store/"
ORDER_URL = "https://ferreteriacharly.sicarx.shop/api/cart/order"
DISPATCH_PAY_URL = "https://api.sicarx.com/external/v1/dispatch/pay"
GRAPH_URL = "https://api.sicarx.com/document-graph/v1/graph-v2"
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
    }}"""

    headers = storefront_headers(token, content_type="application/graphql", branch_id=branch_id)

    async with httpx.AsyncClient(timeout=SICAR_TIMEOUT) as client:
        response = await client.post(STORE_URL, content=query, headers=headers)
        if response.status_code != 200:
            logger.error(f"Error en pre-validacion de carrito en Sicar: {response.status_code} - {response.text}")
            raise HTTPException(status_code=502, detail="No se pudo validar el carrito con Sicar X. Intenta nuevamente.")
        payload = response.json()

    if "errors" in payload:
        logger.error(f"Errores GraphQL en pre-validacion de carrito: {payload['errors']}")
        raise HTTPException(status_code=502, detail="No se pudo validar el carrito con Sicar X. Intenta nuevamente.")

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

async def create_order_in_sicar(db: AsyncSession, order_payload: dict, client_token: str, branch_id: str, products_data: list):
    """Confirma la orden en Sicar y descuenta el stock localmente."""
    order_headers = storefront_headers(client_token, content_type="application/json", branch_id=branch_id)

    async with httpx.AsyncClient(timeout=SICAR_TIMEOUT) as client:
        response = await client.post(ORDER_URL, json=order_payload, headers=order_headers)
        logger.info(f"Respuesta de Sicar al crear orden: {response.status_code}")
        if response.status_code not in (200, 201):
            logger.error(f"Error confirmando la orden en Sicar: {response.status_code} - {response.text}")
            raise HTTPException(status_code=502, detail="No se pudo confirmar la orden en Sicar X. Intenta nuevamente más tarde.")

        sicar_response = response.json()

    # Descuento local de inventario
    for item in products_data:
        product_uuid = item.get("uuid")
        quantity = float(item.get("quantity", 0))

        if product_uuid and quantity > 0:
            stmt = update(Product).where(Product.sicar_uuid == product_uuid).values(stock=Product.stock - quantity)
            await db.execute(stmt)

    await db.commit()
    return sicar_response

async def pay_order_in_sicar(order_id: str, total_amount: float, cash_register_uuid: str, branch_id: str):
    """Aplica el pago a una orden existente desde sicarX directamente mediante la API REST."""

    async def attempt_payment(admin_token: str):
        headers = admin_app_headers(admin_token)

        payload = {
            "cashRegisterUuid": cash_register_uuid,
            "id": order_id,
            "payments": [
                {
                    "paymentId": "CASH",
                    "amount": total_amount
                }
            ],
            "total": total_amount
        }

        async with httpx.AsyncClient(timeout=SICAR_TIMEOUT) as client:
            return await client.post(DISPATCH_PAY_URL, json=payload, headers=headers)

    response = await sicar_auth.request_with_retry(attempt_payment)

    if response.status_code != 200:
        logger.error(f"Error al aplicar el pago a la orden {order_id}: {response.status_code} - {response.text}")
        raise HTTPException(status_code=502, detail="La orden se creó, pero falló el pago. Contacta a soporte.")

    logger.debug(f"Respuesta de pago en Sicar: {response.status_code}")

    return response.json()
