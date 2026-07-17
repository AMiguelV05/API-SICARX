import logging
from typing import List
from fastapi import APIRouter, Depends, Body, Path, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import validate_api_key, get_current_client
from app.models.client import ClientAccount
from app.schemas.client import ClientAddressCreate, ClientAddressUpdate, ClientAddressPublic
from app.services.address_service import create_address, update_address, delete_address

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/me/addresses", tags=["Client Addresses"])

@router.get("", response_model=List[ClientAddressPublic], summary="Listar direcciones guardadas del cliente")
async def list_addresses(
    client: ClientAccount = Depends(get_current_client),
    _: str = Depends(validate_api_key)
):
    """Devuelve las direcciones guardadas de la cuenta autenticada."""
    return await client.awaitable_attrs.addresses

@router.post("", response_model=ClientAddressPublic, status_code=status.HTTP_201_CREATED, summary="Agregar una dirección")
async def add_address(
    data: ClientAddressCreate = Body(...),
    client: ClientAccount = Depends(get_current_client),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(validate_api_key)
):
    """
    Crea una nueva dirección para la cuenta autenticada. Si `is_default` es `true`,
    cualquier otra dirección marcada como default para este cliente se desmarca
    automáticamente (solo puede haber una).
    """
    return await create_address(db, client, data)

@router.patch("/{address_uuid}", response_model=ClientAddressPublic, summary="Editar una dirección")
async def edit_address(
    data: ClientAddressUpdate = Body(...),
    address_uuid: str = Path(...),
    client: ClientAccount = Depends(get_current_client),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(validate_api_key)
):
    """
    Actualiza una dirección existente (debe pertenecer al cliente autenticado, si no
    responde `404`). Todos los campos son opcionales — solo se cambia lo que se envíe.
    """
    return await update_address(db, client, address_uuid, data)

@router.delete("/{address_uuid}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar una dirección")
async def remove_address(
    address_uuid: str = Path(...),
    client: ClientAccount = Depends(get_current_client),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(validate_api_key)
):
    """Elimina una dirección de la cuenta autenticada (debe pertenecerle, si no responde `404`)."""
    await delete_address(db, client, address_uuid)
