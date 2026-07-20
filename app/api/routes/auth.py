import logging
from typing import Optional
from fastapi import APIRouter, Depends, Body, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import DbDep
from app.core.security import validate_api_key, create_client_token, CurrentClientDep
from app.core.rate_limit import limiter
from app.core.cookies import clear_cart_cookie
from app.models.client import ClientAccount
from app.models.cart import Cart
from app.schemas.client import ClientRegister, ClientLogin, ClientAuthResponse, ClientPublic, ClientUpdate
from app.services.client_service import register_client, authenticate_client, update_client
from app.services.cart_service import get_cart_response, try_merge_cart_token

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Client Auth"], dependencies=[Depends(validate_api_key)])

async def _build_auth_response(
    db: AsyncSession, client: ClientAccount, cart_token: Optional[str], response: Response
) -> ClientAuthResponse:
    """
    Compartido por /auth/register y /auth/login: emite el token de sesion y, si vino un
    cartToken de un carrito anonimo, lo fusiona a la cuenta en la misma llamada (tolerante -
    un token ausente/invalido no falla el login/registro, ver try_merge_cart_token). El
    carrito resultante (fusionado o no) se devuelve inline para que el frontend no necesite
    un GET /cart aparte para hidratar su UI tras iniciar sesion.
    """
    token = create_client_token(client.uuid)
    merged_cart = await try_merge_cart_token(db, client, cart_token)
    if merged_cart is not None:
        clear_cart_cookie(response)  # la cookie del carrito anonimo ya consumido queda obsoleta
        cart = merged_cart
    else:
        cart = await db.scalar(select(Cart).where(Cart.client_account_id == client.id))
    cart_response = await get_cart_response(db, cart)
    return ClientAuthResponse(token=token, client=client, cart=cart_response)

@router.post("/auth/register", response_model=ClientAuthResponse, summary="Registrar una nueva cuenta de cliente")
@limiter.limit("5/minute")
async def register(request: Request, response: Response, db: DbDep, data: ClientRegister = Body()):
    """
    Crea una cuenta de cliente local (nombre, correo, teléfono opcional, contraseña).
    El correo debe ser único. Devuelve un token de sesión, igual que `/auth/login`,
    para iniciar sesión automáticamente tras registrarse. Si se envía `cartToken` (el
    de un carrito anónimo previo), se fusiona a la cuenta en la misma llamada y el
    carrito resultante viene incluido en la respuesta. Limitado a 5 intentos por
    minuto por IP para dificultar el registro masivo/spam de cuentas.
    """
    client = await register_client(db, data)
    return await _build_auth_response(db, client, data.cart_token, response)

@router.post("/auth/login", response_model=ClientAuthResponse, summary="Iniciar sesión con una cuenta de cliente")
@limiter.limit("5/minute")
async def login(request: Request, response: Response, db: DbDep, data: ClientLogin = Body()):
    """
    Valida correo y contraseña contra las cuentas de cliente locales y devuelve
    un token de sesión (JWT propio de esta API, distinto del token de sesión de Sicar X).
    Si se envía `cartToken` (el de un carrito anónimo previo), se fusiona a la cuenta en
    la misma llamada y el carrito resultante viene incluido en la respuesta - un token
    ausente o que ya no resuelve simplemente se ignora, nunca hace fallar el login.
    Limitado a 5 intentos por minuto por IP para dificultar fuerza bruta.
    """
    client = await authenticate_client(db, data)
    return await _build_auth_response(db, client, data.cart_token, response)

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
