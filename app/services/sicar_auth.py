import httpx
from fastapi import HTTPException
from app.core.config import settings

ACCOUNT_LAMBDA_URL = "https://7ew5wkc4jsnbb6ph2bd2r4o5540hqlea.lambda-url.us-east-1.on.aws/login/v1/account"
LOGIN_LAMBDA_URL = "https://7ew5wkc4jsnbb6ph2bd2r4o5540hqlea.lambda-url.us-east-1.on.aws/login/v1/login"

class SicarAuthManager:
    """Gestor centralizado para la autenticación B2B con Sicar X"""
    
    def __init__(self):
        # Inicia con el token configurado en las variables de entorno
        self._current_token = settings.SICAR_TOKEN

    async def get_token(self) -> str:
        # Devuelve el token administrativo activo en memoria.
        return self._current_token

    async def refresh_token(self) -> str:
        # Fuerza un inicio de sesión con Sicar, actualiza la caché y devuelve el nuevo token.
        device_id = "fastapi-backend-85fadd7d"
        account_payload = {
            "deviceType": "Web",
            "deviceId": device_id,
            "deviceAlias": "FastAPI Auto-Login Server",
            "email": settings.SICAR_ADMIN_EMAIL,
            "password": settings.SICAR_ADMIN_PASSWORD
        }
        
        async with httpx.AsyncClient() as client:
            account_response = await client.post(ACCOUNT_LAMBDA_URL, json=account_payload)
            
            if account_response.status_code != 200:
                print(f"Error fatal en Auto-Login: {account_response.text}")
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
            login_response = await client.post(LOGIN_LAMBDA_URL, json=login_payload)

            if login_response.status_code != 200:
                print(f"Error fatal en Auto-Login: {login_response.text}")
                raise HTTPException(
                    status_code=500, 
                    detail="Fallo crítico: El backend no pudo autenticarse con los servidores centrales de Sicar X."
                )

            final_jwt = login_response.headers.get("Cauth") or login_response.headers.get("cauth")
            
            if not final_jwt:
                print("Headers de respuesta de login:", login_response.headers)
                raise HTTPException(
                    status_code=500, 
                    detail="Fallo crítico: El backend no pudo obtener el token final de Sicar X."
                )

            self._current_token = final_jwt
            print("Token administrativo de Sicar X actualizado exitosamente.")
            return self._current_token
        
# Instancia global
sicar_auth = SicarAuthManager()