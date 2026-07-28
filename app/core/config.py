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

    # Webhook saliente hacia un futuro dashboard admin (order-cancelled, ver
    # app/services/admin_notification_service.py) - a diferencia de TODAS las demas
    # variables de este archivo, estas dos son deliberadamente OPCIONALES (no "New
    # required var alert" de CLAUDE.md): el dashboard admin todavia no existe, asi que
    # no hay un valor real que darles hoy. admin_notification_service se queda callado
    # (solo loggea a INFO) si cualquiera de las dos falta, en vez de romper el arranque
    # de pydantic-settings para api/worker por infraestructura que aun no existe.
    ADMIN_DASHBOARD_BASE_URL: Optional[str] = None
    ADMIN_WEBHOOK_SECRET: Optional[str] = None

    class Config:
        env_file = ".env"

settings = Settings()