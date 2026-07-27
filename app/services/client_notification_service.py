import json
import logging
import time

import httpx

from app.core.config import settings
from app.models.client import ClientAccount
from app.core.security import create_email_verification_token
from app.core.webhook_signing import sign_hmac_sha256

VERIFICATION_REQUESTED_WEBHOOK_PATH = "/api/webhooks/verification-requested"
WEBHOOK_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

logger = logging.getLogger(__name__)

async def notify_verification_requested(client: ClientAccount) -> None:
    """Avisa al frontend (via webhook firmado) que hay que enviar un correo de
    verificacion, para que el frontend lo envie con su propio template de react-email y
    su propia cuenta de Resend - mismo patron que order_notification_service.
    notify_order_confirmed (unico otro ejemplo de webhook saliente en este codebase), ver
    FRONTEND_INTEGRATION.md para el contrato completo. No fatal si falla, sin reintento
    automatico - mismas razones que notify_order_confirmed.

    Nombrado "verification-requested" (no "email-verification") a proposito: el evento es
    "hay que mandar un correo de verificacion", no "el correo ya se verifico" - evita
    confusion con un futuro evento "verified".

    Esta firma (headers X-Webhook-*) es un esquema propio de este backend, sin relacion
    con el esquema de Mercado Pago (x-signature/x-request-id, ver
    payment_service.verify_mercadopago_webhook_signature) - no confundir ambos."""
    try:
        token = create_email_verification_token(client.uuid)
        body = {
            "clientUuid": client.uuid,
            "clientName": client.name,
            "clientEmail": client.email,
            "token": token,
        }

        raw_body = json.dumps(body, separators=(",", ":")).encode()
        ts = str(int(time.time()))
        signature = sign_hmac_sha256(settings.FRONTEND_WEBHOOK_SECRET, f"{ts}.".encode() + raw_body)

        url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}{VERIFICATION_REQUESTED_WEBHOOK_PATH}"
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as http_client:
            response = await http_client.post(
                url,
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Timestamp": ts,
                    "X-Webhook-Signature": signature,
                },
            )
        if response.status_code not in (200, 201, 202):
            logger.error(f"Frontend rechazo la notificacion de verificacion para {client.email}: {response.status_code} - {response.text}")
            return
        logger.info(f"Notificacion de verificacion de correo enviada al frontend para {client.email}.")
    except Exception as e:
        logger.error(f"Error inesperado notificando verificacion de correo para {client.email}: {type(e).__name__}: {e!r}")
