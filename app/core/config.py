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

    # Mercado Pago (Checkout Bricks) - ver CLAUDE.md, seccion "Pagos con Mercado Pago"
    MP_ACCESS_TOKEN: str  # token privado, Bearer para todas las llamadas a la API de Mercado Pago
    MP_WEBHOOK_SECRET: str  # valida x-signature en las notificaciones entrantes
    FRONTEND_BASE_URL: str  # dominio real del frontend, usado para back_urls de la preferencia
    API_BASE_URL: str  # dominio publico de esta API (Railway), usado para notification_url

    class Config:
        env_file = ".env"

settings = Settings()