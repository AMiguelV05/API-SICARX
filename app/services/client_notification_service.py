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
    """Avisa al frontend que debe enviar el correo de verificacion (el frontend lo manda con su propio Resend). No fatal, sin reintento. Nombrado "requested", no "email-verification", para no confundirse con un futuro evento "verified"."""
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
