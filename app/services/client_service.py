import logging
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.client import ClientAccount
from app.schemas.client import ClientRegister, ClientLogin, ClientUpdate
from app.core.security import hash_password, verify_password

logger = logging.getLogger(__name__)

async def register_client(db: AsyncSession, data: ClientRegister) -> ClientAccount:
    existing = await db.scalar(select(ClientAccount).where(ClientAccount.email == data.email))
    if existing:
        logger.info(f"Intento de registro con email ya existente: {data.email}")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya existe una cuenta con ese correo.")

    client = ClientAccount(
        name=data.name,
        email=data.email,
        phone=data.phone,
        hashed_password=hash_password(data.password),
    )
    db.add(client)
    await db.commit()
    await db.refresh(client)

    logger.info(f"Cuenta de cliente creada: {client.email}")
    return client

async def authenticate_client(db: AsyncSession, data: ClientLogin) -> ClientAccount:
    client = await db.scalar(select(ClientAccount).where(ClientAccount.email == data.email))

    if not client or not verify_password(data.password, client.hashed_password):
        logger.info(f"Intento de login fallido para: {data.email}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Correo o contraseña incorrectos.")

    if not client.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Esta cuenta está desactivada.")

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

    logger.info(f"Cuenta de cliente actualizada: {client.email}")
    return client
