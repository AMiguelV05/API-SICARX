import asyncio
import httpx
import logging
from fastapi import HTTPException
from app.core.config import settings
from app.core.retry import request_with_backoff

ACCOUNT_LAMBDA_URL = "https://7ew5wkc4jsnbb6ph2bd2r4o5540hqlea.lambda-url.us-east-1.on.aws/login/v1/account"
LOGIN_LAMBDA_URL = "https://7ew5wkc4jsnbb6ph2bd2r4o5540hqlea.lambda-url.us-east-1.on.aws/login/v1/login"
AUTH_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
logger = logging.getLogger(__name__)

class SicarAuthManager:
    """Gestor centralizado para la autenticación B2B con Sicar X"""

    def __init__(self):
        self._current_token = settings.SICAR_TOKEN
        self._refresh_lock = asyncio.Lock()

    async def get_token(self) -> str:
        return self._current_token

    async def refresh_token(self, stale_token: str = None) -> str:
        """Refresca el token con Sicar X; si `stale_token` ya no coincide con el cache, otra corrutina ya lo refresco y se evita un login duplicado."""
        async with self._refresh_lock:
            if stale_token is not None and stale_token != self._current_token:
                return self._current_token

            device_id = "fastapi-backend-85fadd7d"
            account_payload = {
                "deviceType": "Web",
                "deviceId": device_id,
                "deviceAlias": "FastAPI Auto-Login Server",
                "email": settings.SICAR_ADMIN_EMAIL,
                "password": settings.SICAR_ADMIN_PASSWORD
            }

            try:
                async with httpx.AsyncClient(timeout=AUTH_TIMEOUT) as client:
                    async def _post_account():
                        return await client.post(ACCOUNT_LAMBDA_URL, json=account_payload)

                    account_response = await request_with_backoff(
                        _post_account, max_attempts=2, base_delay=0.5, context="Sicar X auto-login (account)"
                    )

                    if account_response.status_code != 200:
                        logger.error(f"Error fatal en Auto-Login: {account_response.text}")
                        raise HTTPException(
                            status_code=500,
                            detail="Fallo crítico: El backend no pudo autenticarse con los servidores centrales de Sicar X."
                        )
                    initial_jwt = account_response.json().get("jwt")

                    login_payload = {
                        "branchId": 151456,
                        "deviceId": device_id,
                        "deviceAlias": "FastAPI Auto-Login Server",
                        "fcmId": None,
                        "deviceType": "Web",
                        "jwt": initial_jwt
                    }

                    async def _post_login():
                        return await client.post(LOGIN_LAMBDA_URL, json=login_payload)

                    login_response = await request_with_backoff(
                        _post_login, max_attempts=2, base_delay=0.5, context="Sicar X auto-login (login)"
                    )

                    if login_response.status_code != 200:
                        logger.error(f"Error fatal en Auto-Login: {login_response.text}")
                        raise HTTPException(
                            status_code=500,
                            detail="Fallo crítico: El backend no pudo autenticarse con los servidores centrales de Sicar X."
                        )

                    final_jwt = login_response.headers.get("Cauth") or login_response.headers.get("cauth")

                    if not final_jwt:
                        logger.error(f"Headers de respuesta de login faltantes: {login_response.headers}")
                        raise HTTPException(
                            status_code=500,
                            detail="Fallo crítico: El backend no pudo obtener el token final de Sicar X."
                        )
            except httpx.HTTPError as e:
                # request_with_backoff agoto sus reintentos por error de red (antes esto se
                # propagaba crudo en vez de convertirse en el mismo HTTPException(500) que ya
                # producen las ramas de status-code de arriba).
                logger.error(f"Error de red fatal en Auto-Login tras reintentos: {type(e).__name__}: {e}")
                raise HTTPException(
                    status_code=500,
                    detail="Fallo crítico: El backend no pudo autenticarse con los servidores centrales de Sicar X."
                )

            self._current_token = final_jwt
            logger.info("Token administrativo de Sicar X actualizado exitosamente.")
            return self._current_token

    async def request_with_retry(self, request_func):
        """Reintenta una vez con token refrescado si Sicar X responde 401 (refresh deduplicado entre corrutinas)."""
        token = await self.get_token()
        response = await request_func(token)

        if response.status_code == 401:
            logger.warning("Token administrativo expirado. Solicitando renovacion a AWS...")
            token = await self.refresh_token(stale_token=token)
            response = await request_func(token)

        return response

sicar_auth = SicarAuthManager()