import httpx
import logging
from fastapi import HTTPException
from app.models.order import Order
from app.services.sicar_auth import sicar_auth
from app.core.sicar_headers import admin_app_headers
from app.core.sicar_validation import is_safe_sicar_id
from app.core.upstream_errors import raise_upstream_error

CANCEL_URL = "https://api.sicarx.com/documents/v1/sale/cancel"
DOCUMENT_GRAPH_URL = "https://api.sicarx.com/document-graph/v1/graph-v2"
DISPATCH_URL = "https://api.sicarx.com/external/v1/dispatch"
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
        raise_upstream_error(response, f"Error al resolver el uuid del documento {object_id}", "No se pudo resolver el documento a cancelar en Sicar X.")

    payload = response.json()
    document_uuid = ((payload.get("data") or {}).get("generatedV2") or {}).get("uuid")
    if not document_uuid:
        raise HTTPException(status_code=404, detail="No se encontró el documento a cancelar en Sicar X.")
    return document_uuid

async def process_order_cancellation(order: Order, cash_register_uuid: str) -> str:
    """Cancela en Sicar usando el Token B2B del Administrador.

    Llamada UNICAMENTE por app/worker/sicar_sync_worker.py, nunca desde una ruta de la
    API - la cancelacion local (status, restauracion de stock, notificaciones) ya paso
    y ya se confirmo al cliente antes de que esta funcion siquiera se invoque; ver
    order_history_service.prepare_local_cancellation. Este modulo ya no toca stock ni
    hace ningun commit - eso vive enteramente del lado local-first.

    `order.sicar_document_uuid` (distinto de `order.sicar_order_id`, el "id" estilo
    Mongo) se resuelve una sola vez via generatedV2 y se cachea en el objeto - el
    llamador es responsable de persistir esa mutacion (junto con el resultado de esta
    llamada) en su propio commit."""
    if not order.sicar_document_uuid:
        order.sicar_document_uuid = await _resolve_document_uuid(order.sicar_order_id)

    async def attempt_cancel(admin_token: str):
        headers = admin_app_headers(admin_token)
        sicar_payload = {"cashRegisterUuid": cash_register_uuid, "uuid": order.sicar_document_uuid}

        async with httpx.AsyncClient(timeout=CANCEL_TIMEOUT) as client:
            return await client.post(CANCEL_URL, json=sicar_payload, headers=headers)

    response = await sicar_auth.request_with_retry(attempt_cancel)

    if response.status_code != 200:
        raise_upstream_error(response, f"Sicar X rechazo la cancelación del documento {order.sicar_document_uuid}", "Sicar X rechazó la cancelación del pedido.")

    return response.text

async def advance_dispatch_status(order: Order, dispatch_status: str) -> None:
    """Avanza el dispatchStatus de una orden en Sicar X (p. ej. PENDING_ACCEPTANCE -> PENDING
    al aceptar un pedido). Mutacion real capturada en vivo (agent-browser contra
    app.sicarx.com, tablero de despacho) el 2026-07-29 - PUT /external/v1/dispatch con el
    admin token, usando `documentId` = order.sicar_order_id (el "id" estilo Mongo, NO el
    uuid RFC4122 que exige la cancelacion - esta mutacion no necesita _resolve_document_uuid
    en absoluto). El mismo endpoint acepta cualquier valor de la secuencia
    PENDING/PREPARING/COMPLETE/DISPATCHED, confirmado probando la secuencia completa en vivo;
    un valor fuera de esa vocabulario (se probo "FINISHED") responde 409.

    `deliveryInfo` debe reenviarse completo en cada llamada (Sicar X no lo trata como un
    PATCH parcial) - se reconstruye aqui desde `order.delivery_info`, el mismo snapshot que
    ya se guarda en create_local_order. `deliveryManId`/`deliveryMan` van siempre en null:
    son un concepto propio de Sicar X (repartidor interno) confirmado en la captura pero
    nunca poblado en las ordenes de esta integracion - no representan lo mismo que
    Order.delivery_company (mensajeria externa asignada via /admin, ver admin_service.py),
    asi que no se mapean entre si."""
    delivery_info = order.delivery_info or {}
    contact_info = delivery_info.get("contactInfo") or {}

    payload = {
        "documentId": order.sicar_order_id,
        "warehouseId": None,
        "dispatchStatus": dispatch_status,
        "deliveryInfo": {
            "contactInfo": {
                "name": contact_info.get("name"),
                "phone": contact_info.get("phone"),
                "email": contact_info.get("email"),
                "address": contact_info.get("address"),
            },
            "deliveryType": delivery_info.get("deliveryType"),
            "deliveryManId": None,
            "deliveryMan": None,
        },
    }

    async def attempt_advance(admin_token: str):
        headers = admin_app_headers(admin_token)
        async with httpx.AsyncClient(timeout=CANCEL_TIMEOUT) as client:
            return await client.put(DISPATCH_URL, json=payload, headers=headers)

    response = await sicar_auth.request_with_retry(attempt_advance)

    if response.status_code != 200:
        raise_upstream_error(
            response,
            f"Sicar X rechazo el avance de dispatchStatus a '{dispatch_status}' para el documento {order.sicar_order_id}",
            "Sicar X rechazó el avance del estado de despacho del pedido.",
        )