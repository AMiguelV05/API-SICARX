import logging
from fastapi import APIRouter, Depends, Body, Request
from app.core.database import DbDep
from app.core.security import validate_api_key, create_client_token, CurrentClientDep
from app.core.rate_limit import limiter
from app.schemas.client import ClientRegister, ClientLogin, ClientAuthResponse, ClientPublic, ClientUpdate
from app.services.client_service import register_client, authenticate_client, update_client

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Client Auth"], dependencies=[Depends(validate_api_key)])

@router.post("/auth/register", response_model=ClientAuthResponse, summary="Registrar una nueva cuenta de cliente")
@limiter.limit("5/minute")
async def register(request: Request, db: DbDep, data: ClientRegister = Body()):
    """
    Crea una cuenta de cliente local (nombre, correo, teléfono opcional, contraseña).
    El correo debe ser único. Devuelve un token de sesión, igual que `/auth/login`,
    para iniciar sesión automáticamente tras registrarse. Limitado a 5 intentos por
    minuto por IP para dificultar el registro masivo/spam de cuentas.
    """
    client = await register_client(db, data)
    token = create_client_token(client.uuid)
    return ClientAuthResponse(token=token, client=client)

@router.post("/auth/login", response_model=ClientAuthResponse, summary="Iniciar sesión con una cuenta de cliente")
@limiter.limit("5/minute")
async def login(request: Request, db: DbDep, data: ClientLogin = Body()):
    """
    Valida correo y contraseña contra las cuentas de cliente locales y devuelve
    un token de sesión (JWT propio de esta API, distinto del token de sesión de Sicar X).
    Limitado a 5 intentos por minuto por IP para dificultar fuerza bruta.
    """
    client = await authenticate_client(db, data)
    token = create_client_token(client.uuid)
    return ClientAuthResponse(token=token, client=client)

@router.get("/auth/me", response_model=ClientPublic, summary="Obtener datos de la cuenta del cliente")
async def get_me(client: CurrentClientDep):
    """
    Devuelve nombre, correo y teléfono de la cuenta del cliente autenticado — pensado
    para poblar una pantalla de "Mi cuenta". Requiere el token de `/auth/register` o
    `/auth/login` en el header `Authorization` (distinto del token de sesión de Sicar X).
    """
    await client.awaitable_attrs.addresses  # necesario para serializar ClientPublic.addresses
    return client

@router.patch("/auth/me", response_model=ClientPublic, summary="Editar datos de la cuenta del cliente")
@limiter.limit("10/minute")
async def update_me(request: Request, client: CurrentClientDep, db: DbDep, data: ClientUpdate = Body()):
    """
    Actualiza nombre, teléfono y/o contraseña de la cuenta autenticada. Todos los campos
    son opcionales — solo se cambia lo que se envía. Para cambiar la contraseña hay que
    enviar `current_password` (la actual) junto con `new_password`; si `current_password`
    no coincide, responde `401`. Limitado a 10 llamadas por minuto por IP (cubre también
    intentos repetidos de adivinar `current_password`).
    """
    updated = await update_client(db, client, data)
    return updated
