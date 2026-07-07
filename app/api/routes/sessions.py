import logging
from fastapi import APIRouter, HTTPException, Header, Depends
from app.services.session_service import get_or_refresh_customer_session
from app.core.security import validate_api_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/session", tags=["Session"])

@router.post("/init")
async def initialize_or_refresh_session(
    authorization: str = Header(None, description="Token JWT actual del cliente (opcional)"),
    _ : str = Depends(validate_api_key)
):
    """
    Endpoint para que el frontend solicite una nueva sesión o refresque la actual.
    """
    try:
        if authorization:
            logger.info("Solicitando refresco de sesión existente.")
        else:
            logger.info("Solicitando inicialización de una nueva sesión.")

        session_data = await get_or_refresh_customer_session(authorization)
        return session_data
    except Exception as e:
        logger.error(f"Error al inicializar o refrescar la sesión: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))