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

    MP_ACCESS_TOKEN: str
    MP_WEBHOOK_SECRET: str  # valida x-signature en las notificaciones entrantes
    FRONTEND_BASE_URL: str  # dominio real del frontend
    API_BASE_URL: str  # dominio publico la API

    # Webhook saliente hacia el frontend (correo de confirmacion de pedido via Resend en el frontend)
    FRONTEND_WEBHOOK_SECRET: str

    GOOGLE_CLIENT_ID: str

    # Webhook saliente hacia el dashboard admin (order-cancelled/sicar-sync-failed). Optional,
    # a diferencia del resto de variables de este archivo: sin dashboard real todavia,
    # admin_notification_service simplemente no-opea (loggea INFO) si falta.
    ADMIN_DASHBOARD_BASE_URL: Optional[str] = None
    ADMIN_WEBHOOK_SECRET: Optional[str] = None

    # Llave estatica para /v1/admin/* (ver security.py::validate_admin_key). Optional igual
    # que el par de arriba, pero su ausencia hace que las rutas admin respondan 401 siempre,
    # en vez de no-opear como ADMIN_DASHBOARD_BASE_URL/ADMIN_WEBHOOK_SECRET.
    ADMIN_API_KEY: Optional[str] = None

    # Guias de envio con envia.com (ver shipping_service.py). Requeridas en AMBOS servicios
    # de Railway (api y worker) aunque solo api las use - pydantic-settings falla al importar
    # si falta cualquiera en cualquiera de los dos procesos.
    ENVIA_API_TOKEN: str  # Bearer token de la API de envios de envia.com (distinta de su API de Geocodes, que el frontend llama directo sin llave)
    # Sandbox/produccion usan dominios y tokens separados (api-test. vs api.envia.com) - un
    # token de un ambiente da 401 contra el dominio del otro. "sandbox" porque el token hoy
    # configurado es de sandbox; no cambiar hasta tener un token real de produccion.
    ENVIA_ENVIRONMENT: str = "sandbox"  # "sandbox" o "production"
    # Sin prefijo ENVIA_ORIGIN_: shipping_service.py reusa el mismo valor para origin y
    # destination (negocio exclusivo de Mexico en ambos lados).
    ENVIA_COUNTRY: str = "MX"  # ISO 3166-1 alpha-2 (envia.com lo exige asi, a diferencia de Sicar X, que usa alpha-3 "MEX")
    ENVIA_PHONE_CODE: str = "+52"
    ENVIA_ORIGIN_NAME: str
    ENVIA_ORIGIN_COMPANY: str  # razon social de la tienda para el campo "company" del origen (envia.com); el destino nunca lleva company, ver shipping_service._destination_address
    ENVIA_ORIGIN_PHONE: str
    ENVIA_ORIGIN_EMAIL: str
    ENVIA_ORIGIN_STREET: str
    ENVIA_ORIGIN_NUMBER: str
    ENVIA_ORIGIN_DISTRICT: str
    ENVIA_ORIGIN_CITY: str
    ENVIA_ORIGIN_STATE: str  # codigo corto de estado (2-3 caracteres, p. ej. "NL"), NO el nombre completo - envia.com lo exige asi, ver shipping_service._origin_address
    ENVIA_ORIGIN_ZIP_CODE: str
    # Opcional (envia.com no lo exige) - referencia de ubicacion para el origen, p. ej.
    # "Local 3, junto a la ferreteria". None si no se configura.
    ENVIA_ORIGIN_REFERENCE: Optional[str] = None

    class Config:
        env_file = ".env"

settings = Settings()