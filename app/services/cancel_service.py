import httpx
import logging
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update
from app.models.product import Product
from app.services.sicar_auth import sicar_auth
from app.core.sicar_headers import admin_app_headers

CANCEL_URL = "https://api.sicarx.com/documents/v1/sale/cancel"
CANCEL_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)
logger = logging.getLogger(__name__)

async def process_order_cancellation(db: AsyncSession, document_uuid: str, cash_register_uuid: str, products: list):
    """Cancela en Sicar usando el Token B2B del Administrador y revierte stock local."""

    async def attempt_cancel(admin_token: str):
        headers = admin_app_headers(admin_token)
        sicar_payload = {"cashRegisterUuid": cash_register_uuid, "uuid": document_uuid}

        async with httpx.AsyncClient(timeout=CANCEL_TIMEOUT) as client:
            return await client.post(CANCEL_URL, json=sicar_payload, headers=headers)

    response = await sicar_auth.request_with_retry(attempt_cancel)

    if response.status_code != 200:
        logger.error(f"Sicar X rechazo la cancelación del documento {document_uuid}: {response.status_code} - {response.text}")
        raise HTTPException(status_code=502, detail="Sicar X rechazó la cancelación del pedido.")

    cancel_timestamp = response.text 

    # Restauración local en Postgres
    for item in products:
        product_uuid = item.get("uuid")
        quantity = float(item.get("quantity", 0))

        if product_uuid and quantity > 0:
            stmt = update(Product).where(Product.sicar_uuid == product_uuid).values(stock=Product.stock + quantity)
            await db.execute(stmt)
            
    await db.commit()
    return cancel_timestamp