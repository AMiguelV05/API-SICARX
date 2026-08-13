import asyncio
import hashlib
import logging
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Annotated
import bcrypt
import jwt
from fastapi import Security, HTTPException, status, Header, Depends, Request
from fastapi.security.api_key import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import get_client_ip
from app.models.client import ClientAccount

logger = logging.getLogger(__name__)
# Definimos que buscaremos la clave en la cabecera 'x-api-key'
API_KEY_NAME = "x-api-key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

CLIENT_JWT_ALGORITHM = "HS256"

async def validate_api_key(api_key: str = Security(api_key_header)):
    if not api_key:
        logger.error("Falta la cabecera de autenticacion x-api-key.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta la cabecera de autenticación x-api-key."
        )

    if not secrets.compare_digest(api_key, settings.X_API_KEY):
        logger.error("Acceso denegado: API Key invalida o expirada.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Acceso denegado: API Key invalida o expirada."
        )

    return api_key

# Throttle en memoria para /v1/admin/* por IP, independiente del limiter de slowapi -
# decorar cada ruta admin individualmente seria un diff grande para una sola dependencia.
# Mismo caveat que ese limiter: solo valido mientras `api` corra como una sola instancia.
_ADMIN_KEY_WINDOW_SECONDS = 60
_ADMIN_KEY_MAX_ATTEMPTS = 30
_admin_key_attempts: dict[str, list[float]] = defaultdict(list)

def _admin_key_rate_limited(client_ip: str) -> bool:
    now = time.monotonic()
    window_start = now - _ADMIN_KEY_WINDOW_SECONDS
    attempts = [t for t in _admin_key_attempts[client_ip] if t > window_start]
    attempts.append(now)
    _admin_key_attempts[client_ip] = attempts
    return len(attempts) > _ADMIN_KEY_MAX_ATTEMPTS

async def validate_admin_key(request: Request, x_admin_key: str = Header(None, alias="X-Admin-Key")):
    """
    Dependencia para el router /v1/admin/*. `ADMIN_API_KEY` es Optional (ver config.py)
    - mientras no se configure, rechaza toda peticion sin importar la cabecera. Tambien
    throttlea por IP (ver `_admin_key_rate_limited`), a diferencia de x-api-key.
    """
    client_ip = get_client_ip(request)
    if _admin_key_rate_limited(client_ip):
        logger.error("Acceso denegado a ruta admin: demasiados intentos desde %s.", client_ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Demasiados intentos, intenta de nuevo en un minuto."
        )

    if not settings.ADMIN_API_KEY or not x_admin_key or not secrets.compare_digest(x_admin_key, settings.ADMIN_API_KEY):
        logger.error("Acceso denegado a ruta admin: X-Admin-Key invalida, ausente, o ADMIN_API_KEY sin configurar.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Acceso denegado."
        )
    return x_admin_key

def _hash_password_sync(password: str) -> str:
    """Genera el hash bcrypt de una contraseña en texto plano. Sincrona a proposito: solo
    debe llamarse directamente en contexto sync (p. ej. el bootstrap de _DUMMY_HASH al
    importar el modulo, antes de que exista un event loop) - en cualquier otro caso usar
    `hash_password`, que la corre en un threadpool."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def _verify_password_sync(password: str, hashed_password: str) -> bool:
    """Verifica una contraseña en texto plano contra su hash bcrypt. Sincrona a proposito,
    ver `_hash_password_sync` - usar `verify_password` fuera de contexto sync."""
    return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))

async def hash_password(password: str) -> str:
    """Genera el hash bcrypt de una contraseña en texto plano, sin bloquear el event loop
    (bcrypt tarda ~100-300ms, corrida en un threadpool via asyncio.to_thread)."""
    return await asyncio.to_thread(_hash_password_sync, password)

async def verify_password(password: str, hashed_password: str) -> bool:
    """Verifica una contraseña en texto plano contra su hash bcrypt, sin bloquear el event
    loop - ver `hash_password`."""
    return await asyncio.to_thread(_verify_password_sync, password, hashed_password)

def create_client_token(client_uuid: str) -> str:
    """Genera el JWT de sesión para una cuenta de cliente registrada localmente. Lleva
    `iat` explícito (PyJWT no lo agrega solo) para que _resolve_client_from_token pueda
    invalidar tokens emitidos antes del último cambio de password de la cuenta."""
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.CLIENT_JWT_EXPIRE_MINUTES)
    payload = {"sub": client_uuid, "exp": expire, "iat": now}
    return jwt.encode(payload, settings.CLIENT_JWT_SECRET, algorithm=CLIENT_JWT_ALGORITHM)

EMAIL_VERIFICATION_EXPIRE_MINUTES = 60 * 24  # 24 horas

def create_email_verification_token(client_uuid: str) -> str:
    """Token de un solo proposito para POST /auth/verify-email. Reusa CLIENT_JWT_SECRET
    (si ese secreto se filtra ya se pueden forjar sesiones directamente, es estrictamente
    peor) pero lleva un claim `purpose` que _resolve_client_from_token rechaza, para que
    nunca sirva como token de sesion."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=EMAIL_VERIFICATION_EXPIRE_MINUTES)
    payload = {"sub": client_uuid, "exp": expire, "purpose": "email_verify"}
    return jwt.encode(payload, settings.CLIENT_JWT_SECRET, algorithm=CLIENT_JWT_ALGORITHM)

def decode_email_verification_token(token: str) -> str:
    """Inverso de create_email_verification_token. A diferencia de
    _resolve_client_from_token (que rechaza CUALQUIER `purpose`, por compatibilidad con
    tokens de sesion previos), aqui se exige purpose == "email_verify" explicitamente -
    todo token de este tipo es nuevo, sin compatibilidad que preservar."""
    try:
        payload = jwt.decode(token, settings.CLIENT_JWT_SECRET, algorithms=[CLIENT_JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="El enlace de verificación expiró, solicita uno nuevo.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Enlace de verificación inválido.")

    if payload.get("purpose") != "email_verify":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Enlace de verificación inválido.")

    return payload.get("sub")

PASSWORD_RESET_TOKEN_EXPIRE_MINUTES = 30  # corto a proposito, mas sensible que verificar correo

def generate_password_reset_token() -> str:
    """Token de un solo uso para POST /auth/reset-password. A diferencia de
    create_email_verification_token, NO es un JWT - se persiste (hasheado, ver
    hash_reset_token) en password_reset_tokens para poder marcarlo usado/invalidarlo,
    algo que un JWT stateless no permite. 256 bits de entropia, suficiente sin necesidad
    de un hash lento tipo bcrypt para guardarlo."""
    return secrets.token_urlsafe(32)

def hash_reset_token(token: str) -> str:
    """Hash determinista (no bcrypt) del token de reset, para buscarlo en
    password_reset_tokens.token_hash sin persistir el valor en texto plano."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

async def _resolve_client_from_token(token: str, db: AsyncSession) -> ClientAccount:
    """
    Decodifica un JWT de cuenta de cliente (emitido por `create_client_token`) y
    carga la cuenta correspondiente desde la base de datos local. Compartido por
    `get_current_client` y `get_current_client_header`, que solo difieren en de
    qué cabecera toman el token.
    """
    clean_token = token.replace("Bearer ", "").replace("bearer ", "").strip()

    try:
        payload = jwt.decode(clean_token, settings.CLIENT_JWT_SECRET, algorithms=[CLIENT_JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="La sesión expiró, inicia sesión nuevamente.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido.")

    # Tokens de un solo proposito (p. ej. verificacion de correo) llevan un claim `purpose`
    # que un token de sesion real nunca tiene - si esta presente, se rechaza para que un
    # enlace de verificacion filtrado no sirva tambien como token de sesion.
    if payload.get("purpose") is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido.")

    client = await db.scalar(select(ClientAccount).where(ClientAccount.uuid == payload.get("sub")))
    if not client or not client.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Cuenta no encontrada o desactivada.")

    # Un cambio de password (reset o PATCH /auth/me) invalida cualquier sesion emitida
    # antes de ese momento. Tokens sin `iat` (emitidos por una version anterior de
    # create_client_token, antes de que este claim existiera) se tratan como "siempre
    # anteriores" - solo importa si esta cuenta en particular ya cambio su password.
    # `password_changed_at` se trunca a segundo completo antes de comparar: `iat` de un JWT
    # solo tiene resolucion de segundos (PyJWT trunca el datetime), asi que un token recien
    # emitido en el MISMO segundo que el cambio (el caso real de /auth/reset-password, que
    # loguea automaticamente justo despues de cambiar la password) podia compararse como
    # "anterior" por los microsegundos de mas que trae `password_changed_at` y quedar
    # rechazado de inmediato - bug confirmado en pruebas manuales antes de este ajuste.
    if client.password_changed_at is not None:
        issued_at = payload.get("iat")
        changed_at_floor = client.password_changed_at.replace(microsecond=0)
        if issued_at is None or datetime.fromtimestamp(issued_at, tz=timezone.utc) < changed_at_floor:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="La sesión expiró, inicia sesión nuevamente.")

    return client

async def get_current_client(
    authorization: str = Header(None, alias="Authorization", description="Token JWT de la cuenta de cliente"),
    db: AsyncSession = Depends(get_db),
):
    """
    Dependencia para rutas protegidas por cuenta de cliente (distinto del token de
    sesión de Sicar X). Decodifica el JWT emitido por `create_client_token` y carga
    la cuenta correspondiente desde la base de datos local.
    """
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No se proporcionó el token de la cuenta.")

    return await _resolve_client_from_token(authorization, db)

async def get_current_client_header(
    x_client_token: str = Header(None, alias="X-Client-Token", description="Token JWT de la cuenta de cliente"),
    db: AsyncSession = Depends(get_db),
):
    """Igual que `get_current_client`, pero toma el token de `X-Client-Token` en vez de
    `Authorization`. Usada en `/orders` y `/cancel` por razones históricas (ver CLAUDE.md)."""
    if not x_client_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No se proporcionó el token de la cuenta de cliente (X-Client-Token).")

    return await _resolve_client_from_token(x_client_token, db)

async def get_optional_client_header(
    x_client_token: str = Header(None, alias="X-Client-Token", description="Token JWT de la cuenta de cliente (opcional)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Como `get_current_client_header`, pero devuelve `None` si la cabecera esta ausente
    (rutas con cliente anonimo, p. ej. el carrito) en vez de 401 - si SI viene pero es
    invalida/expirada, sigue respondiendo 401 en vez de bajar a anonimo en silencio.
    """
    if not x_client_token:
        return None
    return await _resolve_client_from_token(x_client_token, db)

CurrentClientDep = Annotated[ClientAccount, Depends(get_current_client)]
CurrentClientHeaderDep = Annotated[ClientAccount, Depends(get_current_client_header)]
OptionalClientHeaderDep = Annotated[ClientAccount | None, Depends(get_optional_client_header)]