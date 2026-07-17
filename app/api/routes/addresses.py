import logging
from typing import List
from fastapi import APIRouter, Depends, Body, Path, status
from app.core.database import DbDep
from app.core.security import validate_api_key, CurrentClientDep
from app.schemas.client import ClientAddressCreate, ClientAddressUpdate, ClientAddressPublic
from app.services.address_service import create_address, update_address, delete_address

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/me/addresses", tags=["Client Addresses"], dependencies=[Depends(validate_api_key)])

@router.get("", response_model=List[ClientAddressPublic], summary="Listar direcciones guardadas del cliente")
async def list_addresses(client: CurrentClientDep):
    """Devuelve las direcciones guardadas de la cuenta autenticada."""
    return await client.awaitable_attrs.addresses

@router.post("", response_model=ClientAddressPublic, status_code=status.HTTP_201_CREATED, summary="Agregar una dirección")
async def add_address(client: CurrentClientDep, db: DbDep, data: ClientAddressCreate = Body()):
    """
    Crea una nueva dirección para la cuenta autenticada. Si `is_default` es `true`,
    cualquier otra dirección marcada como default para este cliente se desmarca
    automáticamente (solo puede haber una).
    """
    return await create_address(db, client, data)

@router.patch("/{address_uuid}", response_model=ClientAddressPublic, summary="Editar una dirección")
async def edit_address(client: CurrentClientDep, db: DbDep, address_uuid: str = Path(), data: ClientAddressUpdate = Body()):
    """
    Actualiza una dirección existente (debe pertenecer al cliente autenticado, si no
    responde `404`). Todos los campos son opcionales — solo se cambia lo que se envíe.
    """
    return await update_address(db, client, address_uuid, data)

@router.delete("/{address_uuid}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar una dirección")
async def remove_address(client: CurrentClientDep, db: DbDep, address_uuid: str = Path()):
    """Elimina una dirección de la cuenta autenticada (debe pertenecerle, si no responde `404`)."""
    await delete_address(db, client, address_uuid)
