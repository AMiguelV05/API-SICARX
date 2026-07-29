from typing import Optional
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str
    X_API_KEY: str
    SICAR_ADMIN_EMAIL: str
    SICAR_ADMIN_PASSWORD: str
    SICAR_TOKEN: str
    SICAR_PRICE_LIST_ID: str
    CASH_REGISTER_UUID: str
    CLIENT_JWT_SECRET: str
    CLIENT_JWT_EXPIRE_MINUTES: int = 10080  # 7 dias
    ENVIRONMENT: str = "production"  # "development" habilita cookies validas sobre HTTP local

    # Mercado Pago (Checkout Bricks)
    MP_ACCESS_TOKEN: str 
    MP_WEBHOOK_SECRET: str  # valida x-signature en las notificaciones entrantes
    FRONTEND_BASE_URL: str  # dominio real del frontend
    API_BASE_URL: str  # dominio publico la API

    # Webhook saliente hacia el frontend (correo de confirmacion de pedido via Resend en
    # el frontend) - ver CLAUDE.md, seccion "Payments with Mercado Pago", y FRONTEND_INTEGRATION.md
    FRONTEND_WEBHOOK_SECRET: str

    # Login con Google (verificacion de ID token, ver app/services/google_auth_service.py) -
    # no es secreto (va embebido en el JS del frontend para Google Identity Services), solo
    # se usa aqui para validar el claim `aud` del token. No requiere GOOGLE_CLIENT_SECRET:
    # este backend nunca intercambia tokens con Google directamente.
    GOOGLE_CLIENT_ID: str

    # Webhook saliente hacia el dashboard admin (order-cancelled/sicar-sync-failed, ver
    # app/services/admin_notification_service.py). Configurado en produccion desde 2026-07-29
    # apuntando al mismo dominio que FRONTEND_BASE_URL (https://ferreteriacharly.com/) - el
    # panel admin vive dentro de la misma app Next.js, no es un servicio separado. Siguen
    # siendo Optional (a diferencia de TODAS las demas variables de este archivo, que no
    # son "New required var alert") para que un ambiente local/de pruebas sin dashboard real
    # (o sin ese valor en su .env) no rompa el arranque de pydantic-settings -
    # admin_notification_service se queda callado (solo loggea a INFO) si cualquiera falta.
    ADMIN_DASHBOARD_BASE_URL: Optional[str] = None
    ADMIN_WEBHOOK_SECRET: Optional[str] = None

    # Llave estatica para /v1/admin/* (ver app/core/security.py::validate_admin_key). Igual
    # de opcional que el par de arriba - no existe todavia un dashboard admin real que la
    # necesite, asi que mientras no se configure, las rutas admin simplemente responden 401
    # (a diferencia de ADMIN_DASHBOARD_BASE_URL/ADMIN_WEBHOOK_SECRET, cuya ausencia hace que
    # el envio de notificaciones sea un no-op silencioso en vez de bloquear nada).
    ADMIN_API_KEY: Optional[str] = None

    class Config:
        env_file = ".env"

settings = Settings()