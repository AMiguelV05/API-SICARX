import logging
from typing import NoReturn
import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

def raise_upstream_error(response: httpx.Response, log_context: str, user_message: str, status_code: int = 502) -> NoReturn:
    """Registra la respuesta completa de Sicar X/Mercado Pago (como ya se hacia en cada
    sitio antes de esto) y levanta un HTTPException cuyo `detail` es `user_message` (el
    mismo texto generico que ya se devolvia) mas, si el cuerpo de la respuesta trae un
    mensaje legible, ese mensaje entre parentesis. Nunca se filtra `response.text` crudo
    al cliente (podria traer HTML, stack traces o nombres de campos internos de Sicar) -
    solo el valor de una de las claves `message`/`error`/`detail` si el cuerpo es JSON,
    truncado a 300 caracteres. El texto completo sigue yendo integro al log."""
    logger.error(f"{log_context}: {response.status_code} - {response.text}")

    upstream_message = None
    try:
        payload = response.json()
        if isinstance(payload, dict):
            upstream_message = payload.get("message") or payload.get("error") or payload.get("detail")
            if not upstream_message and isinstance(payload.get("errors"), list) and payload["errors"]:
                first_error = payload["errors"][0]
                if isinstance(first_error, dict):
                    upstream_message = first_error.get("message")
    except Exception:
        pass

    detail = user_message
    if isinstance(upstream_message, str) and upstream_message.strip():
        detail = f"{user_message} (Sicar/MP respondió: {upstream_message.strip()[:300]})"

    raise HTTPException(status_code=status_code, detail=detail)
