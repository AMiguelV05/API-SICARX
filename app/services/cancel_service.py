import httpx
import logging
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update
from app.models.product import Product
from app.services.sicar_auth import sicar_auth
from app.core.sicar_headers import admin_app_headers
from app.core.sicar_validation import is_safe_sicar_id

CANCEL_URL = "https://api.sicarx.com/documents/v1/sale/cancel"
DOCUMENT_GRAPH_URL = "https://api.sicarx.com/document-graph/v1/graph-v2"
CANCEL_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)
logger = logging.getLogger(__name__)

async def _resolve_document_uuid(object_id: str) -> str:
    """Sicar X identifica cada documento con dos valores distintos: el `id` estilo Mongo
    que devuelve la creación de la orden (y que este API expone como OrderResponse.id),
    y un `uuid` RFC4122 separado que es el que en realidad exige /documents/v1/sale/cancel.
    Se resuelve uno a partir del otro con generatedV2(objectId), confirmado contra una
    cancelación real capturada desde app.sicarx.com."""
    if not is_safe_sicar_id(object_id):
        raise HTTPException(status_code=400, detail="El identificador del documento no es válido.")

    query = f'{{ generatedV2(objectId: "{object_id}") {{ uuid }} }}'

    async def attempt_lookup(admin_token: str):
        headers = admin_app_headers(admin_token, content_type="application/graphql")
        async with httpx.AsyncClient(timeout=CANCEL_TIMEOUT) as client:
            return await client.post(DOCUMENT_GRAPH_URL, content=query, headers=headers)

    response = await sicar_auth.request_with_retry(attempt_lookup)

    if response.status_code != 200:
        logger.error(f"Error al resolver el uuid del documento {object_id}: {response.status_code} - {response.text}")
        raise HTTPException(status_code=502, detail="No se pudo resolver el documento a cancelar en Sicar X.")

    payload = response.json()
    document_uuid = ((payload.get("data") or {}).get("generatedV2") or {}).get("uuid")
    if not document_uuid:
        raise HTTPException(status_code=404, detail="No se encontró el documento a cancelar en Sicar X.")
    return document_uuid

async def process_order_cancellation(db: AsyncSession, object_id: str, cash_register_uuid: str, products: list):
    """Cancela en Sicar usando el Token B2B del Administrador y revierte stock local.

    `object_id` es el `id` que Sicar X devolvió al crear la orden (formato Mongo), no el
    `uuid` que espera el endpoint de cancelación — se resuelve primero con generatedV2."""
    document_uuid = await _resolve_document_uuid(object_id)

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
        product_uuid = item.uuid
        quantity = float(item.quantity)

        if product_uuid and quantity > 0:
            stmt = update(Product).where(Product.sicar_uuid == product_uuid).values(stock=Product.stock + quantity)
            await db.execute(stmt)
            
    await db.commit()
    return cancel_timestamp