import logging
from typing import Optional
from fastapi import APIRouter, Depends, Body, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import DbDep
from app.core.security import validate_api_key, create_client_token, CurrentClientDep
from app.core.rate_limit import limiter
from app.core.cookies import clear_cart_cookie
from app.models.client import ClientAccount
from app.models.cart import Cart
from app.schemas.client import (
    ClientRegister, ClientLogin, GoogleLogin, VerifyEmailRequest, ClientAuthResponse, ClientPublic, ClientUpdate,
    ForgotPasswordRequest, ResetPasswordRequest, MessageResponse,
)
from app.services.client_service import (
    register_client, authenticate_client, update_client,
    get_or_create_google_client, verify_client_email, resend_verification_email,
    request_password_reset, reset_password,
)
from app.services.google_auth_service import verify_google_id_token
from app.services.cart_service import get_cart_response, try_merge_cart_token

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Client Auth"], dependencies=[Depends(validate_api_key)])

async def _build_auth_response(
    db: AsyncSession, client: ClientAccount, cart_token: Optional[str], response: Response
) -> ClientAuthResponse:
    """
    Compartido por /auth/register, /auth/login y /auth/google: emite el token de sesion
    y, si vino `cartToken`, fusiona ese carrito anonimo a la cuenta (tolerante - un token
    ausente/invalido no falla el login, ver try_merge_cart_token). El carrito resultante
    se devuelve inline para evitar un GET /cart aparte tras iniciar sesion.
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
    Crea una cuenta de cliente local; el correo debe ser único (`409` si ya existe).
    Devuelve un token de sesión, igual que `/auth/login`, para iniciar sesión
    automáticamente tras registrarse. Limitado a 5 intentos por minuto por IP.
    """
    client = await register_client(db, data)
    return await _build_auth_response(db, client, data.cart_token, response)

@router.post("/auth/login", response_model=ClientAuthResponse, summary="Iniciar sesión con una cuenta de cliente")
@limiter.limit("5/minute")
async def login(request: Request, response: Response, db: DbDep, data: ClientLogin = Body()):
    """
    Valida correo y contraseña contra las cuentas de cliente locales y devuelve un
    token de sesión (JWT propio, distinto del de Sicar X). `401` en credenciales
    inválidas. Limitado a 5 intentos por minuto por IP.
    """
    client = await authenticate_client(db, data)
    return await _build_auth_response(db, client, data.cart_token, response)

@router.post("/auth/google", response_model=ClientAuthResponse, summary="Iniciar sesión o registrarse con Google")
@limiter.limit("10/minute")
async def google_login(request: Request, response: Response, db: DbDep, data: GoogleLogin = Body()):
    """
    Verifica un ID token de Google (obtenido en el frontend) y resuelve la cuenta local
    — la crea si es la primera vez. Devuelve la misma forma que `/auth/login`. Si el
    correo ya tiene cuenta local con contraseña, responde `409` en vez de fusionar
    automáticamente (evita que la contraseña de quien registró ese correo primero siga
    vigente sobre lo que su dueño real ahora cree asegurado por Google) — usar la
    contraseña en ese caso. Cuentas creadas así ya quedan verificadas.
    """
    identity = await verify_google_id_token(data.id_token)
    client = await get_or_create_google_client(db, identity)
    return await _build_auth_response(db, client, data.cart_token, response)

@router.post("/auth/verify-email", response_model=ClientPublic, summary="Confirmar verificación de correo")
@limiter.limit("10/minute")
async def verify_email(request: Request, db: DbDep, data: VerifyEmailRequest = Body()):
    """
    Confirma el token enviado por correo tras `/auth/register` y marca la cuenta como
    verificada. No requiere sesión activa — el token en sí prueba propiedad del correo.
    Verificación "suave": no bloquea login ni checkout, solo se refleja en `isVerified`.
    """
    return await verify_client_email(db, data.token)

@router.post("/auth/resend-verification", status_code=status.HTTP_204_NO_CONTENT, summary="Reenviar correo de verificación")
@limiter.limit("5/minute")
async def resend_verification(request: Request, client: CurrentClientDep, db: DbDep):
    """
    Reenvía el correo de verificación a la cuenta autenticada — deliberadamente no
    acepta un correo suelto sin sesión, para no habilitar enumeración de cuentas. `400`
    si ya está verificada.
    """
    await resend_verification_email(db, client)

@router.post("/auth/forgot-password", response_model=MessageResponse, summary="Solicitar recuperación de contraseña")
@limiter.limit("5/minute")
async def forgot_password(request: Request, db: DbDep, data: ForgotPasswordRequest = Body()):
    """
    Dispara el correo de recuperación de contraseña si el correo corresponde a una cuenta
    local activa — responde exactamente igual sin importar si la cuenta existe, para no
    habilitar enumeración (ver `request_password_reset`). Limitado a 5 intentos por minuto
    por IP.
    """
    await request_password_reset(db, data.email)
    return MessageResponse(detail="Si el correo existe, se enviará un enlace de recuperación.")

@router.post("/auth/reset-password", response_model=ClientAuthResponse, summary="Confirmar recuperación de contraseña")
@limiter.limit("10/minute")
async def reset_password_route(request: Request, response: Response, db: DbDep, data: ResetPasswordRequest = Body()):
    """
    Confirma el token enviado por correo tras `/auth/forgot-password`, establece la nueva
    contraseña y devuelve una sesión nueva (mismo shape que `/auth/login`) — el usuario
    queda logueado sin un paso extra. `400` si el token es inválido, ya fue usado, o venció
    (vigencia 30 min). Invalida cualquier sesión previa de la cuenta. Limitado a 10 intentos
    por minuto por IP.
    """
    client = await reset_password(db, data.token, data.new_password)
    return await _build_auth_response(db, client, None, response)

@router.get("/auth/me", response_model=ClientPublic, summary="Obtener datos de la cuenta del cliente")
async def get_me(client: CurrentClientDep):
    await client.awaitable_attrs.addresses  # necesario para serializar ClientPublic.addresses
    return client

@router.patch("/auth/me", response_model=ClientPublic, summary="Editar datos de la cuenta del cliente")
@limiter.limit("10/minute")
async def update_me(request: Request, client: CurrentClientDep, db: DbDep, data: ClientUpdate = Body()):
    """
    Actualiza nombre, teléfono y/o contraseña de la cuenta autenticada — todos los
    campos son opcionales. Cambiar la contraseña requiere `current_password` junto con
    `new_password`; `401` si no coincide. Limitado a 10 llamadas por minuto por IP.
    """
    updated = await update_client(db, client, data)
    return updated
