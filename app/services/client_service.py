import logging
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.client import ClientAccount
from app.schemas.client import ClientRegister, ClientLogin, ClientUpdate
from app.core.security import hash_password, verify_password

logger = logging.getLogger(__name__)

# Hash bcrypt fijo, calculado una sola vez al importar el modulo, usado solo para igualar
# el tiempo de respuesta cuando el correo no existe -- evita que se pueda distinguir "cuenta
# inexistente" de "contraseña incorrecta" midiendo cuanto tarda cada rama (verify_password,
# via bcrypt, es la parte costosa de la operacion, ~100-300ms).
_DUMMY_HASH = hash_password("timing-attack-mitigation-dummy-password")

async def register_client(db: AsyncSession, data: ClientRegister) -> ClientAccount:
    email = data.email.lower()
    existing = await db.scalar(select(ClientAccount).where(ClientAccount.email == email))
    if existing:
        logger.info(f"Intento de registro con email ya existente: {email}")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya existe una cuenta con ese correo.")

    client = ClientAccount(
        name=data.name,
        email=email,
        phone=data.phone,
        hashed_password=hash_password(data.password),
    )
    db.add(client)
    await db.commit()
    await db.refresh(client)
    await client.awaitable_attrs.addresses  # necesario para serializar ClientPublic.addresses

    logger.info(f"Cuenta de cliente creada: {client.email}")
    return client

async def authenticate_client(db: AsyncSession, data: ClientLogin) -> ClientAccount:
    email = data.email.lower()
    client = await db.scalar(select(ClientAccount).where(ClientAccount.email == email))

    # Siempre corremos verify_password, incluso si no existe la cuenta (contra el hash dummy),
    # para que ambas ramas tomen el mismo tiempo.
    password_ok = verify_password(data.password, client.hashed_password if client else _DUMMY_HASH)
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
        if not data.current_password or not verify_password(data.current_password, client.hashed_password):
            logger.info(f"Intento de cambio de contraseña con contraseña actual incorrecta: {client.email}")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="La contraseña actual es incorrecta.")
        client.hashed_password = hash_password(data.new_password)

    if data.name is not None:
        client.name = data.name

    if data.phone is not None:
        client.phone = data.phone

    await db.commit()
    await db.refresh(client)
    await client.awaitable_attrs.addresses  # necesario para serializar ClientPublic.addresses

    logger.info(f"Cuenta de cliente actualizada: {client.email}")
    return client
