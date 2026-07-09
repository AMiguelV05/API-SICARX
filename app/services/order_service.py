import httpx
import json
import logging
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update
from app.models.product import Product
from app.services.sicar_auth import sicar_auth

STORE_URL = "https://api.sicarx.com/store/"
ORDER_URL = "https://ferreteriacharly.sicarx.shop/api/cart/order"
DISPATCH_PAY_URL = "https://api.sicarx.com/external/v1/dispatch/pay"
GRAPH_URL = "https://api.sicarx.com/document-graph/v1/graph-v2"

logger = logging.getLogger(__name__)

async def validate_cart_items(uuids: list, token: str, branch_id: str, price_list_uuid: str):
    """Validación de stock y precios usando el token del cliente web"""
    graphql_uuids = json.dumps(uuids)
    
    query = f"""{{
        products(uuids:{graphql_uuids}, branchId: {branch_id}, priceListId: "{price_list_uuid}") {{
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

    headers = {
        "Content-Type": "application/graphql",
        "Accept": "application/json, text/plain, */*",
        "Authorization": token,
        "Origin": "https://ferreteriacharly.sicarx.shop",
        "Referer": "https://ferreteriacharly.sicarx.shop/",
        "x-branch-id": str(branch_id),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(STORE_URL, content=query, headers=headers)
        if response.status_code != 200:
            logger.error(f"Error en pre-validacion de carrito en Sicar\n{response}")
            raise HTTPException(status_code=400, detail="Error en pre-validación de carrito en Sicar")
        return response.json()

async def create_order_in_sicar(db: AsyncSession, order_payload: dict, client_token: str, branch_id: str, products_data: list):
    """Confirma la orden en Sicar y descuenta el stock localmente."""
    order_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Authorization": client_token,
        "Origin": "https://ferreteriacharly.sicarx.shop",
        "Referer": "https://ferreteriacharly.sicarx.shop/",
        "x-branch-id": str(branch_id),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(ORDER_URL, json=order_payload, headers=order_headers)
        logger.info(f"Respuesta de Sicar al crear orden: {response.status_code} - {response}")
        if response.status_code not in (200, 201):
            logger.error(f"Error confirmando la orden: {response.text}")
            raise Exception(f"Error confirmando la orden: {response.text}")
        
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
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Authorization": admin_token,
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": "https://app.sicarx.com",
            "Referer": "https://app.sicarx.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0"
        }

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
    
        async with httpx.AsyncClient() as client:
            return await client.post(DISPATCH_PAY_URL, json=payload, headers=headers)
        
    current_token = await sicar_auth.get_token()
    response = await attempt_payment(current_token)

    if response.status_code == 401:
        logger.warning("Token administrativo expirado. Solicitando renovacion a AWS")
        new_token = await sicar_auth.refresh_token()
        response = await attempt_payment(new_token)
    
    if response.status_code != 200:
        logger.error(f"Error al aplicar el pago a la orden {order_id}: {response.text}")
        raise Exception(f"La orden se creó, pero falló el pago: {response.text}")

    logger.debug(f"Respuesta de pago en Sicar: {response.status_code} - {response.text}")
            
    return response.json()