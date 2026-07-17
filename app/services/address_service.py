import logging
from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.client import ClientAccount, ClientAddress
from app.schemas.client import ClientAddressCreate, ClientAddressUpdate

logger = logging.getLogger(__name__)

async def _get_owned_address(db: AsyncSession, client: ClientAccount, address_uuid: str) -> ClientAddress:
    address = await db.scalar(
        select(ClientAddress).where(
            ClientAddress.uuid == address_uuid,
            ClientAddress.client_account_id == client.id,
        )
    )
    if not address:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dirección no encontrada.")
    return address

async def _clear_existing_default(db: AsyncSession, client_id: int, exclude_uuid: str = None):
    stmt = update(ClientAddress).where(
        ClientAddress.client_account_id == client_id,
        ClientAddress.is_default == True,
    )
    if exclude_uuid:
        stmt = stmt.where(ClientAddress.uuid != exclude_uuid)
    await db.execute(stmt.values(is_default=False))

async def create_address(db: AsyncSession, client: ClientAccount, data: ClientAddressCreate) -> ClientAddress:
    if data.is_default:
        await _clear_existing_default(db, client.id)

    address = ClientAddress(client_account_id=client.id, **data.model_dump())
    db.add(address)
    await db.commit()
    await db.refresh(address)

    logger.info(f"Dirección creada para cliente {client.email}")
    return address

async def update_address(db: AsyncSession, client: ClientAccount, address_uuid: str, data: ClientAddressUpdate) -> ClientAddress:
    address = await _get_owned_address(db, client, address_uuid)

    if data.is_default:
        await _clear_existing_default(db, client.id, exclude_uuid=address_uuid)

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(address, field, value)

    await db.commit()
    await db.refresh(address)

    logger.info(f"Dirección {address_uuid} actualizada para cliente {client.email}")
    return address

async def delete_address(db: AsyncSession, client: ClientAccount, address_uuid: str) -> None:
    address = await _get_owned_address(db, client, address_uuid)
    await db.delete(address)
    await db.commit()

    logger.info(f"Dirección {address_uuid} eliminada para cliente {client.email}")
