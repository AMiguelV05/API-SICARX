import logging
import secrets
from datetime import datetime, timedelta, timezone
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

def hash_password(password: str) -> str:
    """Genera el hash bcrypt de una contraseña en texto plano."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(password: str, hashed_password: str) -> bool:
    """Verifica una contraseña en texto plano contra su hash bcrypt."""
    return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))

def create_client_token(client_uuid: str) -> str:
    """Genera el JWT de sesión para una cuenta de cliente registrada localmente."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.CLIENT_JWT_EXPIRE_MINUTES)
    payload = {"sub": client_uuid, "exp": expire}
    return jwt.encode(payload, settings.CLIENT_JWT_SECRET, algorithm=CLIENT_JWT_ALGORITHM)

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

    token = authorization.replace("Bearer ", "").replace("bearer ", "").strip()

    try:
        payload = jwt.decode(token, settings.CLIENT_JWT_SECRET, algorithms=[CLIENT_JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="La sesión expiró, inicia sesión nuevamente.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido.")

    client = await db.scalar(select(ClientAccount).where(ClientAccount.uuid == payload.get("sub")))
    if not client or not client.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Cuenta no encontrada o desactivada.")

    return client