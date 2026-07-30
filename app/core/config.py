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

    # Guias de envio con envia.com (ver app/services/shipping_service.py y POST
    # /admin/orders/{uuid}/shipping/{quote,generate}). Requeridas (no Optional) igual que
    # MP_ACCESS_TOKEN y el resto de integraciones de pago/paqueteria de este archivo - deben
    # configurarse en AMBOS servicios de Railway (api y worker) aunque solo api las use,
    # porque pydantic-settings falla al importar si falta cualquiera en cualquiera de los dos.
    ENVIA_API_TOKEN: str  # Bearer token de la API de envios de envia.com (distinta de su API de Geocodes, que el frontend llama directo sin llave)
    # `country`/`phone_code` del objeto origin/destination de envia.com - no llevan prefijo
    # ENVIA_ORIGIN_ porque shipping_service.py los reusa igual para origin y destination
    # (negocio exclusivo de Mexico en ambos lados). Con default porque el valor correcto
    # ya se conoce hoy (Mexico) - a diferencia del resto de variables ENVIA_*, que no
    # tienen un default razonable y por eso son obligatorias sin valor por defecto.
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