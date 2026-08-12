import logging
from typing import NoReturn
import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

def raise_upstream_error(response: httpx.Response, log_context: str, user_message: str, status_code: int = 502) -> NoReturn:
    """Loguea la respuesta completa y levanta un HTTPException con `user_message` mas,
    si lo hay, un mensaje legible del upstream entre parentesis (solo `message`/`error`/
    `detail` de un body JSON, truncado a 300 chars) - nunca `response.text` crudo, que
    podria traer HTML o detalles internos de Sicar."""
    request_id = response.headers.get("x-request-id")
    request_id_suffix = f" (x-request-id: {request_id})" if request_id else ""
    logger.error(f"{log_context}: {response.status_code}{request_id_suffix} - {response.text}")

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
