import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update
from app.models.product import Product
from app.services.sicar_auth import sicar_auth

CANCEL_URL = "https://api.sicarx.com/documents/v1/sale/cancel"

async def process_order_cancellation(db: AsyncSession, document_uuid: str, cash_register_uuid: str, products: list):
    """Cancela en Sicar usando el Token B2B del Administrador y revierte stock local."""
    
    async def attempt_cancel(admin_token: str):
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Authorization": admin_token, 
            "Origin": "https://app.sicarx.com",
            "Referer": "https://app.sicarx.com/"
        }
        sicar_payload = {"cashRegisterUuid": cash_register_uuid, "uuid": document_uuid}
        
        async with httpx.AsyncClient() as client:
            return await client.post(CANCEL_URL, json=sicar_payload, headers=headers)

    # 1. Obtenemos el token de administrador desde nuestra caché
    current_token = await sicar_auth.get_token()
    response = await attempt_cancel(current_token)

    # 2. Si el token caducó, FastAPI va por uno nuevo a AWS Lambda
    if response.status_code == 401:
        print("Token administrativo expirado. Solicitando renovación a AWS...")
        new_token = await sicar_auth.refresh_token()
        response = await attempt_cancel(new_token)

    if response.status_code != 200:
        raise Exception(f"Sicar X rechazó la cancelación: {response.text}")

    cancel_timestamp = response.text 

    # 3. Restauración local en PostgreSQL
    for item in products:
        product_uuid = item.get("uuid")
        quantity = float(item.get("quantity", 0))

        if product_uuid and quantity > 0:
            stmt = update(Product).where(Product.sicar_uuid == product_uuid).values(stock=Product.stock + quantity)
            await db.execute(stmt)
            
    await db.commit()
    return cancel_timestamp