import json
import logging
import time
from typing import Optional

from app.core.config import settings
from app.core.webhook_client import send_signed_webhook
from app.models.client import ClientAccount
from app.core.security import create_email_verification_token, PASSWORD_RESET_TOKEN_EXPIRE_MINUTES
from app.core.webhook_signing import sign_hmac_sha256

VERIFICATION_REQUESTED_WEBHOOK_PATH = "/api/webhooks/verification-requested"
PASSWORD_RESET_REQUESTED_WEBHOOK_PATH = "/api/webhooks/password-reset-requested"

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
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": ts,
            "X-Webhook-Signature": signature,
        }
        await send_signed_webhook(url, raw_body, headers, f"Notificacion de verificacion de correo para {client.email}")
    except Exception as e:
        logger.error(f"Error inesperado preparando la notificacion de verificacion de correo para {client.email}: {type(e).__name__}: {e!r}")

async def notify_password_reset_requested(client: ClientAccount, token: Optional[str], has_password: bool) -> None:
    """Avisa al frontend que debe enviar el correo de recuperacion de password (el frontend
    lo manda con su propio Resend, igual que verification-requested). No fatal, sin
    reintento. `token`/`has_password=False` cuando la cuenta es solo-Google - el frontend
    debe renderizar un template distinto ("inicia sesion con Google") en vez del link de
    reset, ya que no hay token que mandar."""
    try:
        body = {
            "clientUuid": client.uuid,
            "clientName": client.name,
            "clientEmail": client.email,
            "hasPassword": has_password,
            "resetToken": token,
            "expiresInMinutes": PASSWORD_RESET_TOKEN_EXPIRE_MINUTES if has_password else None,
        }

        raw_body = json.dumps(body, separators=(",", ":")).encode()
        ts = str(int(time.time()))
        signature = sign_hmac_sha256(settings.FRONTEND_WEBHOOK_SECRET, f"{ts}.".encode() + raw_body)

        url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}{PASSWORD_RESET_REQUESTED_WEBHOOK_PATH}"
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": ts,
            "X-Webhook-Signature": signature,
        }
        await send_signed_webhook(url, raw_body, headers, f"Notificacion de recuperacion de password para {client.email}")
    except Exception as e:
        logger.error(f"Error inesperado preparando la notificacion de recuperacion de password para {client.email}: {type(e).__name__}: {e!r}")
