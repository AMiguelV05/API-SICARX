import httpx
import json
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update
from app.models.product import Product

STORE_URL = "https://api.sicarx.com/store/"
ORDER_URL = "https://ferreteriacharly.sicarx.shop/api/cart/order"

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
        print(f"Respuesta de Sicar al crear orden: {response.status_code} - {response}")
        if response.status_code not in (200, 201):
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