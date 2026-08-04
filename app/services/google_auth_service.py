import asyncio
import logging
from typing import TypedDict
import jwt
from jwt import PyJWKClient
from fastapi import HTTPException, status
from app.core.config import settings

logger = logging.getLogger(__name__)

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

# Instancia a nivel de modulo para reusar el cache interno de PyJWKClient entre requests.
_jwks_client = PyJWKClient(GOOGLE_JWKS_URL)

class GoogleIdentity(TypedDict):
    sub: str
    email: str
    email_verified: bool
    name: str

def _verify_google_id_token_sync(id_token: str) -> dict:
    """Sincrona a proposito: PyJWKClient usa urllib bloqueante; ver verify_google_id_token para el wrapper en threadpool."""
    signing_key = _jwks_client.get_signing_key_from_jwt(id_token)
    return jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256"],  # fijo a proposito - nunca confiar en el `alg` del propio token
        audience=settings.GOOGLE_CLIENT_ID,
        issuer=GOOGLE_ISSUERS,
        leeway=10,
    )

async def verify_google_id_token(id_token: str) -> GoogleIdentity:
    """Verifica un ID token de Google ya obtenido por el frontend; no hay intercambio servidor-a-servidor (sin GOOGLE_CLIENT_SECRET)."""
    try:
        payload = await asyncio.to_thread(_verify_google_id_token_sync, id_token)
    except jwt.PyJWTError as e:  # cubre tambien PyJWKClientError/PyJWKClientConnectionError
        logger.warning(f"Token de Google invalido o expirado: {type(e).__name__}: {e}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token de Google inválido o expirado.") from e

    if not payload.get("email"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="El token de Google no incluye un correo.")

    return GoogleIdentity(
        sub=payload["sub"],
        email=payload["email"].lower(),
        email_verified=bool(payload.get("email_verified", False)),
        name=payload.get("name") or payload["email"],
    )
