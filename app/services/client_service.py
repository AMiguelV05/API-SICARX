import logging
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.client import ClientAccount
from app.models.order import Order
from app.models.password_reset import PasswordResetToken
from app.schemas.client import ClientRegister, ClientLogin, ClientUpdate
from app.core.security import (
    hash_password, verify_password, _hash_password_sync, decode_email_verification_token,
    generate_password_reset_token, hash_reset_token, PASSWORD_RESET_TOKEN_EXPIRE_MINUTES,
)
from app.services.client_notification_service import notify_verification_requested, notify_password_reset_requested
from app.services.google_auth_service import GoogleIdentity

logger = logging.getLogger(__name__)

# Hash dummy fijo para igualar el tiempo de respuesta cuando el correo no existe (evita enumerar cuentas por timing). Sincrono porque corre al importar, antes de que exista un event loop.
_DUMMY_HASH = _hash_password_sync("timing-attack-mitigation-dummy-password")

async def register_client(db: AsyncSession, data: ClientRegister) -> ClientAccount:
    email = data.email.lower()
    existing = await db.scalar(select(ClientAccount).where(ClientAccount.email == email))
    if existing:
        logger.info(f"Intento de registro con email ya existente: {email}")
        if existing.auth_provider == "google":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya existe una cuenta con ese correo, vinculada a Google. Inicia sesión con Google.")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya existe una cuenta con ese correo.")

    client = ClientAccount(
        name=data.name,
        email=email,
        phone=data.phone,
        hashed_password=await hash_password(data.password),
    )
    db.add(client)
    try:
        await db.commit()
    except IntegrityError:
        # Dos registros casi simultaneos pueden pasar ambos el select antes del commit; el segundo choca aqui contra el unique constraint.
        await db.rollback()
        logger.info(f"Intento de registro con email ya existente (deteccion tardia via IntegrityError): {email}")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya existe una cuenta con ese correo.")
    await db.refresh(client)
    await client.awaitable_attrs.addresses  # necesario para serializar ClientPublic.addresses

    logger.info(f"Cuenta de cliente creada: {client.email}")
    await notify_verification_requested(client)  # no fatal, ver client_notification_service
    return client

async def authenticate_client(db: AsyncSession, data: ClientLogin) -> ClientAccount:
    email = data.email.lower()
    client = await db.scalar(select(ClientAccount).where(ClientAccount.email == email))

    # Siempre corre verify_password (incluso sin cuenta o sin password) para que ninguna rama filtre info por timing/mensaje.
    has_password = client is not None and client.hashed_password is not None
    password_ok = await verify_password(data.password, client.hashed_password if has_password else _DUMMY_HASH)
    if not client or not password_ok:
        logger.info(f"Intento de login fallido para: {email}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Correo o contraseña incorrectos.")

    if not client.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Esta cuenta está desactivada.")

    await client.awaitable_attrs.addresses  # necesario para serializar ClientPublic.addresses

    logger.info(f"Login exitoso para: {client.email}")
    return client

async def update_client(db: AsyncSession, client: ClientAccount, data: ClientUpdate) -> ClientAccount:
    if data.new_password:
        if client.hashed_password is None:
            # Cuenta de Google sin password local; solo evitamos el crash de verify_password(..., None).
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Esta cuenta no tiene contraseña, usa el inicio de sesión con Google.")
        if not data.current_password or not await verify_password(data.current_password, client.hashed_password):
            logger.info(f"Intento de cambio de contraseña con contraseña actual incorrecta: {client.email}")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="La contraseña actual es incorrecta.")
        client.hashed_password = await hash_password(data.new_password)
        client.password_changed_at = datetime.now(timezone.utc)  # invalida sesiones previas, ver security.py

    if data.name is not None:
        client.name = data.name

    if data.phone is not None:
        client.phone = data.phone

    await db.commit()
    await db.refresh(client)
    await client.awaitable_attrs.addresses  # necesario para serializar ClientPublic.addresses

    logger.info(f"Cuenta de cliente actualizada: {client.email}")
    return client

async def get_or_create_google_client(db: AsyncSession, identity: GoogleIdentity) -> ClientAccount:
    """Resuelve la cuenta local para un identity de Google verificado. No se auto-vincula por email a una cuenta existente (el registro local no prueba propiedad del correo) - responde 409 y pide iniciar sesion con contraseña."""
    client = await db.scalar(select(ClientAccount).where(ClientAccount.google_sub == identity["sub"]))
    if client:
        if not client.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Esta cuenta está desactivada.")
        await client.awaitable_attrs.addresses
        return client

    existing_by_email = await db.scalar(select(ClientAccount).where(ClientAccount.email == identity["email"]))
    if existing_by_email:
        logger.info(f"Intento de login con Google para un correo ya registrado localmente: {identity['email']}")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya existe una cuenta con ese correo. Inicia sesión con tu contraseña.")

    # email_verified de Google puede ser false (Workspace) - si lo es, la cuenta se crea sin verificar, igual que una cuenta local.
    client = ClientAccount(
        name=identity["name"],
        email=identity["email"],
        hashed_password=None,
        auth_provider="google",
        google_sub=identity["sub"],
        is_verified=identity["email_verified"],
        email_verified_at=datetime.now(timezone.utc) if identity["email_verified"] else None,
    )
    db.add(client)
    await db.flush()
    if identity["email_verified"]:
        await link_guest_orders_by_email(db, client)
    await db.commit()
    await db.refresh(client)
    await client.awaitable_attrs.addresses

    logger.info(f"Cuenta de cliente creada via Google: {client.email}")
    if not identity["email_verified"]:
        await notify_verification_requested(client)
    return client

async def link_guest_orders_by_email(db: AsyncSession, client: ClientAccount) -> int:
    """Vincula retroactivamente los pedidos de invitado (client_account_id IS NULL) cuyo
    guest_email coincide (ambos ya en minuscula, ver routes/orders.py y register_client
    arriba) con el correo de esta cuenta YA VERIFICADA. Debe llamarse dentro de la misma
    transaccion que el commit que marca is_verified=True (o la creacion de una cuenta de
    Google ya verificada) - deliberadamente NUNCA disparado por un registro/login sin
    verificar, para que nadie pueda reclamar el historial de pedidos de otra persona
    registrandose con su correo antes que ella (ver CLAUDE.md/plan de checkout de
    invitado). No hace commit - el llamador es responsable de un unico commit."""
    result = await db.execute(
        update(Order)
        .where(Order.client_account_id.is_(None), Order.guest_email == client.email)
        .values(client_account_id=client.id, guest_email=None)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount:
        logger.info(f"{result.rowcount} pedido(s) de invitado vinculado(s) a la cuenta {client.email} tras verificacion de correo.")
    return result.rowcount or 0

async def verify_client_email(db: AsyncSession, token: str) -> ClientAccount:
    client_uuid = decode_email_verification_token(token)
    client = await db.scalar(select(ClientAccount).where(ClientAccount.uuid == client_uuid))
    if not client or not client.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Cuenta no encontrada o desactivada.")

    if not client.is_verified:
        client.is_verified = True
        client.email_verified_at = datetime.now(timezone.utc)
        await link_guest_orders_by_email(db, client)
        await db.commit()
        await db.refresh(client)
        logger.info(f"Correo verificado: {client.email}")

    await client.awaitable_attrs.addresses
    return client

async def resend_verification_email(db: AsyncSession, client: ClientAccount) -> None:
    if client.is_verified:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Esta cuenta ya está verificada.")
    await notify_verification_requested(client)

async def request_password_reset(db: AsyncSession, email: str) -> None:
    """Nunca revela si la cuenta existe - la ruta siempre responde igual sin importar el
    resultado de esta funcion (ver routes/auth.py). Si la cuenta es de Google (sin password
    local), no se genera token; en su lugar se notifica al frontend con has_password=False
    para que le explique al dueño real de ese correo por que no hay enlace de recuperacion -
    la diferenciacion solo llega al correo, nunca a la respuesta HTTP, que es donde
    "rechazar cuentas Google" se implementa sin habilitar enumeracion."""
    email = email.lower()
    client = await db.scalar(select(ClientAccount).where(ClientAccount.email == email))
    if not client or not client.is_active:
        return

    if client.hashed_password is None:
        await notify_password_reset_requested(client, token=None, has_password=False)
        return

    # Invalida cualquier token previo sin usar de esta cuenta - solo el enlace mas reciente
    # debe funcionar.
    await db.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.client_account_id == client.id, PasswordResetToken.used_at.is_(None))
        .values(used_at=datetime.now(timezone.utc))
    )

    plain_token = generate_password_reset_token()
    db.add(PasswordResetToken(
        client_account_id=client.id,
        token_hash=hash_reset_token(plain_token),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=PASSWORD_RESET_TOKEN_EXPIRE_MINUTES),
    ))
    await db.commit()

    logger.info(f"Token de recuperacion de password generado para: {client.email}")
    await notify_password_reset_requested(client, token=plain_token, has_password=True)

async def reset_password(db: AsyncSession, token: str, new_password: str) -> ClientAccount:
    token_hash = hash_reset_token(token)
    reset_token = await db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash))

    now = datetime.now(timezone.utc)
    if not reset_token or reset_token.used_at is not None or reset_token.expires_at < now:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Token inválido o expirado.")

    client = await db.scalar(select(ClientAccount).where(ClientAccount.id == reset_token.client_account_id))
    if not client or not client.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Token inválido o expirado.")

    client.hashed_password = await hash_password(new_password)
    client.password_changed_at = now
    reset_token.used_at = now

    await db.commit()
    await db.refresh(client)
    await client.awaitable_attrs.addresses  # necesario para serializar ClientPublic.addresses

    logger.info(f"Password restablecido via token de recuperación: {client.email}")
    return client
