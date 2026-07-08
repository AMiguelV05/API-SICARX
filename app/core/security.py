import logging
from fastapi import Security, HTTPException, status
from fastapi.security.api_key import APIKeyHeader
from app.core.config import settings

logger = logging.getLogger(__name__)
# Definimos que buscaremos la clave en la cabecera 'x-api-key'
API_KEY_NAME = "x-api-key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

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
        
    if api_key != settings.X_API_KEY:
        logger.error("Acceso denegado: API Key invalida o expirada.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: API Key invalida o expirada."
        )
        
    return api_key