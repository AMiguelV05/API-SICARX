import logging
from fastapi import APIRouter, HTTPException, Header, Depends
from app.services.session_service import get_or_refresh_customer_session
from app.core.security import validate_api_key
from app.schemas.session import SessionResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/session", tags=["Session"])

@router.post("/init", response_model=SessionResponse, summary="Iniciar o refrescar sesión de cliente")
async def initialize_or_refresh_session(
    authorization: str = Header(None, description="Token JWT actual del cliente (opcional)"),
    _ : str = Depends(validate_api_key)
):
    """
    Sin `Authorization`: crea una sesión anónima nueva haciendo scraping de la cookie
    `tmpStore` del storefront de Sicar X. Con `Authorization`: valida y refresca el JWT
    existente contra `/api/ecommerce/config`. El `token` devuelto es el que el frontend
    debe reenviar como `Authorization` en `POST /orders` — nunca el token admin/B2B.
    """
    try:
        if authorization:
            logger.info("Solicitando refresco de sesion existente.")
        else:
            logger.info("Solicitando inicializacion de una nueva sesion.")

        session_data = await get_or_refresh_customer_session(authorization)
        return session_data
    except Exception as e:
        logger.error(f"Error al inicializar o refrescar la sesion: {str(e)}")
        raise HTTPException(status_code=400, detail="No se pudo inicializar ni refrescar la sesión. Intenta nuevamente.")