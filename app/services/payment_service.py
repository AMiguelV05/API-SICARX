import logging

import httpx
from fastapi import Request

from app.core.config import settings
from app.models.order import Order
from app.core.upstream_errors import raise_upstream_error
from app.core.webhook_signing import verify_hmac_sha256

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
    """Crea una preferencia de MP para habilitar pago con wallet en el Payment Brick. Un solo item agregado (no uno por producto) - MP no soporta bien cantidades fraccionarias. No fatal: si falla, la orden sigue soportando tarjeta/OXXO."""
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
    """Cobra via MP. `transaction_amount` siempre viene de `order.total` en Postgres, nunca de submit_data. Usa order.uuid como X-Idempotency-Key para deduplicar reintentos.

    `payer.email` es requerido por MP para pagos con tarjeta - si el Brick no lo trae en
    `formData` (p. ej. solo se paso en `initialization.payer.email` y el Brick no lo
    reenvia), MP responde un 500 `internal_error` opaco en vez de un 400 claro. Por eso
    aqui se cae de vuelta al correo de contacto del checkout / de la cuenta, mismo
    fallback que `order_notification_service.notify_order_confirmed`, en vez de mandar
    `payer.email: null`.
    """
    payer_email = (submit_data.get("payer") or {}).get("email")
    if not payer_email:
        contact_email = ((order.delivery_info or {}).get("contactInfo") or {}).get("email")
        client_account = await order.awaitable_attrs.client_account
        payer_email = contact_email or (client_account.email if client_account else None)

    payload = {
        "transaction_amount": float(order.total),
        "description": f"Pedido Ferretería Charly #{order.uuid}",
        "payment_method_id": submit_data.get("paymentMethodId"),
        "external_reference": order.uuid,
        "notification_url": f"{settings.API_BASE_URL.rstrip('/')}/v1/payments/webhook",
        "payer": {
            "email": payer_email,
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
        raise_upstream_error(response, f"Mercado Pago rechazo el cobro de la orden {order.uuid}", "No se pudo procesar el pago con Mercado Pago. Intenta nuevamente.")

    return response.json()

async def get_payment(payment_id: str) -> dict:
    """Consulta el estado autoritativo de un pago; los webhooks solo avisan que algo cambio, nunca se confia en su cuerpo."""
    async with httpx.AsyncClient(timeout=MP_TIMEOUT) as client:
        response = await client.get(f"{PAYMENTS_URL}/{payment_id}", headers=_mp_headers())

    if response.status_code != 200:
        raise_upstream_error(response, f"Error al consultar el pago {payment_id} en Mercado Pago", "No se pudo consultar el estado del pago en Mercado Pago.")

    return response.json()

async def refund_payment(payment_id: str) -> dict:
    """Reembolsa un pago aprobado. MP exige X-Idempotency-Key en este endpoint (400 sin el header) - se usa el propio payment_id para deduplicar reintentos."""
    async with httpx.AsyncClient(timeout=MP_TIMEOUT) as client:
        response = await client.post(f"{PAYMENTS_URL}/{payment_id}/refunds", headers=_mp_headers(idempotency_key=payment_id))

    if response.status_code not in (200, 201):
        raise_upstream_error(response, f"Error al reembolsar el pago {payment_id} en Mercado Pago", "No se pudo reembolsar el pago en Mercado Pago.")

    return response.json()

async def cancel_payment(payment_id: str) -> dict:
    """Cancela un pago pendiente/en proceso (no capturado) - mas barato que un reembolso."""
    async with httpx.AsyncClient(timeout=MP_TIMEOUT) as client:
        response = await client.put(f"{PAYMENTS_URL}/{payment_id}", json={"status": "cancelled"}, headers=_mp_headers())

    if response.status_code not in (200, 201):
        raise_upstream_error(response, f"Error al cancelar el pago {payment_id} en Mercado Pago", "No se pudo cancelar el pago pendiente en Mercado Pago.")

    return response.json()

async def verify_mercadopago_webhook_signature(request: Request) -> bool:
    """Valida x-signature/x-request-id contra MP_WEBHOOK_SECRET (esquema distinto al webhook saliente propio de este backend, ver notify_order_confirmed). El formato del manifest esta confirmado por fuentes de la comunidad pero no verificado contra una notificacion real - verificar con el simulador de MP antes de confiar en produccion."""
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
    return verify_hmac_sha256(settings.MP_WEBHOOK_SECRET, manifest.encode(), v1)
