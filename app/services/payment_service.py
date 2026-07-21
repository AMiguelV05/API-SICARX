import hashlib
import hmac
import logging

import httpx
from fastapi import HTTPException, Request

from app.core.config import settings
from app.models.order import Order

PREFERENCES_URL = "https://api.mercadopago.com/checkout/preferences"
PAYMENTS_URL = "https://api.mercadopago.com/v1/payments"
MP_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)

logger = logging.getLogger(__name__)

def _mp_headers(idempotency_key: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {settings.MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key
    return headers

async def create_preference(order: Order) -> dict | None:
    """Crea una preferencia de Mercado Pago para habilitar la opcion de pago con
    cuenta/wallet en el Payment Brick (initialization.preferenceId, ver
    default_rendering.md). Un solo item agregado (no uno por producto) - SICAR permite
    cantidades fraccionarias (productos por peso) que el schema de items de MP no
    soporta bien, y el desglose por producto es puramente cosmetico en la pagina de MP.

    No fatal: si falla, la orden sigue soportando tarjeta/OXXO sin la opcion de wallet -
    mismo patron que otros pasos de enriquecimiento no criticos en este codebase (p. ej.
    _parse_sicar_date en order_history_service.py)."""
    payload = {
        "items": [
            {
                "title": f"Pedido Ferretería Charly #{order.uuid}",
                "quantity": 1,
                "currency_id": "MXN",
                "unit_price": float(order.total),
            }
        ],
        "external_reference": order.uuid,
        "notification_url": f"{settings.API_BASE_URL.rstrip('/')}/v1/payments/webhook",
        "back_urls": {
            "success": f"{settings.FRONTEND_BASE_URL.rstrip('/')}/checkout/success",
            "failure": f"{settings.FRONTEND_BASE_URL.rstrip('/')}/checkout/failure",
            "pending": f"{settings.FRONTEND_BASE_URL.rstrip('/')}/checkout/pending",
        },
        "auto_return": "approved",
    }

    try:
        async with httpx.AsyncClient(timeout=MP_TIMEOUT) as client:
            response = await client.post(PREFERENCES_URL, json=payload, headers=_mp_headers())
        if response.status_code not in (200, 201):
            logger.error(f"Mercado Pago rechazo la creacion de preferencia para la orden {order.uuid}: {response.status_code} - {response.text}")
            return None
        return response.json()
    except httpx.HTTPError as e:
        logger.error(f"Error de red creando preferencia de Mercado Pago para la orden {order.uuid}: {e}")
        return None

async def create_payment(order: Order, submit_data: dict) -> dict:
    """Cobra via Mercado Pago (POST /v1/payments). `transaction_amount` siempre se toma
    de `order.total` (ya persistido en Postgres), nunca de submit_data - mismo principio
    de "no confiar en el precio que manda el cliente" que build_order_payload aplica en
    order_service.py. Usa order.uuid como X-Idempotency-Key para que un submit repetido
    del frontend deduplique del lado de Mercado Pago en vez de cobrar dos veces."""
    payload = {
        "transaction_amount": float(order.total),
        "description": f"Pedido Ferretería Charly #{order.uuid}",
        "payment_method_id": submit_data.get("paymentMethodId"),
        "external_reference": order.uuid,
        "notification_url": f"{settings.API_BASE_URL.rstrip('/')}/v1/payments/webhook",
        "payer": {
            "email": (submit_data.get("payer") or {}).get("email"),
        },
    }

    token = submit_data.get("token")
    if token:
        payload["token"] = token
    issuer_id = submit_data.get("issuerId")
    if issuer_id:
        payload["issuer_id"] = issuer_id
    installments = submit_data.get("installments")
    if installments:
        payload["installments"] = installments

    identification = (submit_data.get("payer") or {}).get("identification") or {}
    if identification.get("number"):
        payload["payer"]["identification"] = {
            "type": identification.get("type"),
            "number": identification.get("number"),
        }

    async with httpx.AsyncClient(timeout=MP_TIMEOUT) as client:
        response = await client.post(PAYMENTS_URL, json=payload, headers=_mp_headers(idempotency_key=order.uuid))

    if response.status_code not in (200, 201):
        logger.error(f"Mercado Pago rechazo el cobro de la orden {order.uuid}: {response.status_code} - {response.text}")
        raise HTTPException(status_code=502, detail="No se pudo procesar el pago con Mercado Pago. Intenta nuevamente.")

    return response.json()

async def get_payment(payment_id: str) -> dict:
    """Consulta el estado autoritativo de un pago. Los webhooks solo avisan que ALGO
    cambio - nunca hay que confiar en el cuerpo de la notificacion, siempre se re-consulta
    aqui (practica recomendada por Mercado Pago)."""
    async with httpx.AsyncClient(timeout=MP_TIMEOUT) as client:
        response = await client.get(f"{PAYMENTS_URL}/{payment_id}", headers=_mp_headers())

    if response.status_code != 200:
        logger.error(f"Error al consultar el pago {payment_id} en Mercado Pago: {response.status_code} - {response.text}")
        raise HTTPException(status_code=502, detail="No se pudo consultar el estado del pago en Mercado Pago.")

    return response.json()

async def refund_payment(payment_id: str) -> dict:
    """Reembolsa un pago ya aprobado - usado por /orders/{id}/cancel cuando el pago de
    la orden a cancelar ya se habia capturado."""
    async with httpx.AsyncClient(timeout=MP_TIMEOUT) as client:
        response = await client.post(f"{PAYMENTS_URL}/{payment_id}/refunds", headers=_mp_headers())

    if response.status_code not in (200, 201):
        logger.error(f"Error al reembolsar el pago {payment_id} en Mercado Pago: {response.status_code} - {response.text}")
        raise HTTPException(status_code=502, detail="No se pudo reembolsar el pago en Mercado Pago.")

    return response.json()

async def cancel_payment(payment_id: str) -> dict:
    """Cancela un pago aun pendiente/en proceso (no capturado) - mas barato que un
    reembolso, usado por /orders/{id}/cancel cuando el pago de la orden a cancelar
    sigue pendiente (p. ej. esperando pago en OXXO)."""
    async with httpx.AsyncClient(timeout=MP_TIMEOUT) as client:
        response = await client.put(f"{PAYMENTS_URL}/{payment_id}", json={"status": "cancelled"}, headers=_mp_headers())

    if response.status_code not in (200, 201):
        logger.error(f"Error al cancelar el pago {payment_id} en Mercado Pago: {response.status_code} - {response.text}")
        raise HTTPException(status_code=502, detail="No se pudo cancelar el pago pendiente en Mercado Pago.")

    return response.json()

async def verify_webhook_signature(request: Request) -> bool:
    """Valida x-signature/x-request-id contra MP_WEBHOOK_SECRET.

    NOTA IMPORTANTE: el manifest exacto ya no aparece verbatim en la documentacion
    publica actual de Mercado Pago (empuja a usar su SDK oficial, que es sincrono y por
    eso no se usa en este proyecto async - ver payment_service module docstring). El
    formato de abajo (`id:{data.id};request-id:{x-request-id};ts:{ts};`, HMAC-SHA256,
    comparacion contra el segmento `v1`) esta confirmado por multiples fuentes de la
    comunidad y por discusiones de los SDKs oficiales de Mercado Pago, pero DEBE
    verificarse contra una notificacion real (el simulador de webhooks del dashboard de
    Mercado Pago) antes de confiar en produccion.
    """
    x_signature = request.headers.get("x-signature")
    x_request_id = request.headers.get("x-request-id")
    if not x_signature or not x_request_id:
        return False

    parts = dict(p.split("=", 1) for p in x_signature.split(",") if "=" in p)
    ts = parts.get("ts")
    v1 = parts.get("v1")
    if not ts or not v1:
        return False

    data_id = request.query_params.get("data.id") or request.query_params.get("id")
    if not data_id:
        return False

    manifest = f"id:{data_id.lower()};request-id:{x_request_id};ts:{ts};"
    expected = hmac.new(settings.MP_WEBHOOK_SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()

    return hmac.compare_digest(expected, v1)
