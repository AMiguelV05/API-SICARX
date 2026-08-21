import logging

import httpx

from app.core.error_tracking import capture_exception
from app.core.retry import request_with_backoff

WEBHOOK_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

logger = logging.getLogger(__name__)


async def send_signed_webhook(url: str, raw_body: bytes, headers: dict, log_context: str, *, max_attempts: int = 3) -> None:
    """Nucleo HTTP compartido por los 5 notificadores salientes (frontend/admin) de este
    backend - cada uno sigue preparando y firmando su propio body, esta funcion solo envia.
    Antes cada notificador redefinia su propia constante WEBHOOK_TIMEOUT identica y hacia un
    solo intento sin reintento alguno.

    Reintenta con backoff ante error de red/5xx del receptor (`request_with_backoff`) -
    sigue sin existir un outbox persistente para estos webhooks (gap aceptado, documentado
    en CLAUDE.md), asi que agotar los reintentos todavia significa perder la notificacion,
    solo que ahora un blip transitorio ya no basta para perderla.

    Distingue una falla de red/HTTP ya esperada tras agotar reintentos (logueada como
    warning, no se reporta a Sentry - es el gap aceptado, no un bug) de cualquier otra
    excepcion (logueada como error y reportada via capture_exception) - antes ninguno de los
    5 sitios llamaba capture_exception, asi que un bug real de este lado (no un problema de
    red) quedaba invisible fuera de app.log incluso con SENTRY_DSN configurado."""
    try:
        async def attempt():
            async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as client:
                return await client.post(url, content=raw_body, headers=headers)

        response = await request_with_backoff(attempt, max_attempts=max_attempts, context=log_context)
        if response.status_code not in (200, 201, 202):
            logger.error(f"{log_context}: rechazado por el receptor: {response.status_code} - {response.text}")
            return
        logger.info(f"{log_context}: enviado.")
    except httpx.HTTPError as e:
        logger.warning(f"{log_context}: fallo de red tras agotar reintentos: {type(e).__name__}: {e!r}")
    except Exception as e:
        logger.error(f"{log_context}: error inesperado enviando el webhook: {type(e).__name__}: {e!r}")
        capture_exception(e, log_context=log_context, url=url)
