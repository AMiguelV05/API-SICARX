import asyncio
import logging
import random
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = {502, 503, 504}
RETRYABLE_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.NetworkError,
)


async def request_with_backoff(
    request_func: Callable[[], Awaitable[httpx.Response]],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    retryable_statuses: set[int] = RETRYABLE_STATUSES,
    context: str = "",
) -> httpx.Response:
    """Reintenta `request_func` ante errores de red (timeout/conexion) y respuestas con
    status en `retryable_statuses`, con backoff exponencial + jitter. Al agotar los
    intentos por excepcion de red, relanza la ultima excepcion (el except del llamador la
    sigue capturando igual que hoy). Al agotar los intentos por status reintentable,
    devuelve la ultima respuesta tal cual - el llamador sigue usando raise_upstream_error
    exactamente como ya lo hace, sin cambiar la forma de manejo de error en cada sitio."""
    last_exc: Exception | None = None
    last_response: httpx.Response | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = await request_func()
        except RETRYABLE_EXCEPTIONS as e:
            last_exc = e
            if attempt == max_attempts:
                logger.error(f"{context}: fallo de red tras {attempt} intento(s): {type(e).__name__}: {e}")
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, base_delay)
            logger.warning(f"{context}: error de red en intento {attempt}/{max_attempts} ({type(e).__name__}: {e}), reintentando en {delay:.1f}s")
            await asyncio.sleep(delay)
            continue

        last_response = response
        if response.status_code not in retryable_statuses or attempt == max_attempts:
            return response

        delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, base_delay)
        logger.warning(f"{context}: respuesta {response.status_code} en intento {attempt}/{max_attempts}, reintentando en {delay:.1f}s")
        await asyncio.sleep(delay)

    if last_response is not None:
        return last_response
    raise last_exc  # pragma: no cover - inalcanzable dado el bucle de arriba
