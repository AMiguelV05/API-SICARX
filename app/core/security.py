import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated
import bcrypt
import jwt
from fastapi import Security, HTTPException, status, Header, Depends
from fastapi.security.api_key import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.database import get_db
from app.models.client import ClientAccount

logger = logging.getLogger(__name__)
# Definimos que buscaremos la clave en la cabecera 'x-api-key'
API_KEY_NAME = "x-api-key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

CLIENT_JWT_ALGORITHM = "HS256"

async def validate_api_key(api_key: str = Security(api_key_header)):
    """
    Dependencia para validar que la petición provenga de nuestro
    frontend independiente utilizando la llave secreta.
    """
    if not api_key:
        logger.error("Falta la cabecera de autenticacion x-api-key.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Falta la cabecera de autenticación x-api-key."
        )
        
    if not secrets.compare_digest(api_key, settings.X_API_KEY):
        logger.error("Acceso denegado: API Key invalida o expirada.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: API Key invalida o expirada."
        )

    return api_key

async def validate_admin_key(x_admin_key: str = Header(None, alias="X-Admin-Key")):
    """
    Dependencia para el router /v1/admin/*. `ADMIN_API_KEY` es Optional (ver
    app/core/config.py) porque todavia no existe un dashboard admin real - mientras no se
    configure, `settings.ADMIN_API_KEY` es None y esta dependencia rechaza toda peticion
    sin importar la cabecera, en vez de intentar comparar contra un valor que no existe.
    """
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
    """Genera el JWT de sesión para una cuenta de cliente registrada localmente."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.CLIENT_JWT_EXPIRE_MINUTES)
    payload = {"sub": client_uuid, "exp": expire}
    return jwt.encode(payload, settings.CLIENT_JWT_SECRET, algorithm=CLIENT_JWT_ALGORITHM)

EMAIL_VERIFICATION_EXPIRE_MINUTES = 60 * 24  # 24 horas

def create_email_verification_token(client_uuid: str) -> str:
    """Token de un solo proposito para confirmar propiedad de un correo (POST
    /auth/verify-email). Reusa CLIENT_JWT_SECRET (no un secreto nuevo - si CLIENT_JWT_SECRET
    se filtra, ya se pueden forjar tokens de sesion directamente, que es estrictamente peor)
    pero lleva un claim `purpose` que _resolve_client_from_token rechaza explicitamente, para
    que este token nunca sirva como token de sesion aunque se filtre."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=EMAIL_VERIFICATION_EXPIRE_MINUTES)
    payload = {"sub": client_uuid, "exp": expire, "purpose": "email_verify"}
    return jwt.encode(payload, settings.CLIENT_JWT_SECRET, algorithm=CLIENT_JWT_ALGORITHM)

def decode_email_verification_token(token: str) -> str:
    """Inverso de create_email_verification_token. A diferencia de
    _resolve_client_from_token (que rechaza CUALQUIER `purpose`, por compatibilidad con
    tokens de sesion ya emitidos antes de que este claim existiera), aqui se exige
    explicitamente purpose == "email_verify" - todo token de este tipo es nuevo, no hay
    compatibilidad hacia atras que preservar."""
    try:
        payload = jwt.decode(token, settings.CLIENT_JWT_SECRET, algorithms=[CLIENT_JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="El enlace de verificación expiró, solicita uno nuevo.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Enlace de verificación inválido.")

    if payload.get("purpose") != "email_verify":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Enlace de verificación inválido.")

    return payload.get("sub")

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

    # Los tokens de un solo proposito (p. ej. verificacion de correo, ver
    # create_email_verification_token) se firman con el mismo CLIENT_JWT_SECRET pero
    # llevan un claim `purpose` que un token de sesion real nunca tiene - si esta presente,
    # se rechaza aqui para que un enlace de verificacion filtrado no sirva tambien como
    # token de sesion valido (misma firma, mismo sub/exp).
    if payload.get("purpose") is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido.")

    client = await db.scalar(select(ClientAccount).where(ClientAccount.uuid == payload.get("sub")))
    if not client or not client.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Cuenta no encontrada o desactivada.")

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
    """
    Igual que `get_current_client`, pero toma el token de la cabecera `X-Client-Token`
    en vez de `Authorization`. Usada únicamente en `/orders` y `/cancel`, donde
    `Authorization` ya está ocupada por el token de sesión de Sicar X.
    """
    if not x_client_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No se proporcionó el token de la cuenta de cliente (X-Client-Token).")

    return await _resolve_client_from_token(x_client_token, db)

async def get_optional_client_header(
    x_client_token: str = Header(None, alias="X-Client-Token", description="Token JWT de la cuenta de cliente (opcional)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Como `get_current_client_header`, pero devuelve `None` si la cabecera esta ausente (para
    rutas donde el cliente puede ser anonimo, p. ej. el carrito) en vez de 401. Si la cabecera
    SI viene pero es invalida/expirada, sigue respondiendo 401 - no se baja silenciosamente a
    anonimo, el cliente afirmo explicitamente estar logueado.
    """
    if not x_client_token:
        return None
    return await _resolve_client_from_token(x_client_token, db)

CurrentClientDep = Annotated[ClientAccount, Depends(get_current_client)]
CurrentClientHeaderDep = Annotated[ClientAccount, Depends(get_current_client_header)]
OptionalClientHeaderDep = Annotated[ClientAccount | None, Depends(get_optional_client_header)]