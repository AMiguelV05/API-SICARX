import logging
from fastapi import APIRouter, Depends, Body, Path, Query, status
from app.core.database import DbDep
from app.core.security import validate_api_key, CurrentClientDep
from app.schemas.wishlist import (
    WishlistCollectionCreate, WishlistCollectionUpdate, WishlistCollectionPublic,
    WishlistItemCreate, WishlistItemListResponse,
)
from app.services import wishlist_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/wishlist", tags=["Wishlist"], dependencies=[Depends(validate_api_key)])

@router.get("/collections", response_model=list[WishlistCollectionPublic], summary="Listar las listas de favoritos del cliente")
async def list_collections(client: CurrentClientDep, db: DbDep):
    """Todas las listas del cliente autenticado, incluida 'Favoritos' (la default, fija) si ya existe."""
    return await wishlist_service.list_collections(db, client)

@router.post("/collections", response_model=WishlistCollectionPublic, status_code=status.HTTP_201_CREATED, summary="Crear una lista de favoritos")
async def create_collection(client: CurrentClientDep, db: DbDep, data: WishlistCollectionCreate = Body()):
    return await wishlist_service.create_collection(db, client, data)

@router.patch("/collections/{collection_uuid}", response_model=WishlistCollectionPublic, summary="Renombrar una lista de favoritos")
async def rename_collection(client: CurrentClientDep, db: DbDep, collection_uuid: str = Path(), data: WishlistCollectionUpdate = Body()):
    """Debe pertenecer al cliente autenticado, si no responde `404`."""
    return await wishlist_service.rename_collection(db, client, collection_uuid, data)

@router.delete("/collections/{collection_uuid}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar una lista de favoritos")
async def delete_collection(client: CurrentClientDep, db: DbDep, collection_uuid: str = Path()):
    """Debe pertenecer al cliente autenticado, si no responde `404`. `409` si es la lista 'Favoritos' (no se puede eliminar)."""
    await wishlist_service.delete_collection(db, client, collection_uuid)

@router.get("/collections/{collection_uuid}/items", response_model=WishlistItemListResponse, summary="Listar productos de una lista")
async def list_collection_items(
    client: CurrentClientDep,
    db: DbDep,
    collection_uuid: str = Path(),
    limit: int = Query(default=60, ge=1, le=200, description="Cantidad de productos por página (1-200)"),
    offset: int = Query(default=0, ge=0, description="Paginación (inicio)"),
):
    """Debe pertenecer al cliente autenticado, si no responde `404`."""
    return await wishlist_service.list_items(db, client, collection_uuid, limit, offset)

@router.post("/collections/{collection_uuid}/items", status_code=status.HTTP_201_CREATED, summary="Guardar un producto en una lista")
async def add_collection_item(client: CurrentClientDep, db: DbDep, collection_uuid: str = Path(), data: WishlistItemCreate = Body()):
    """Idempotente: si el producto ya esta en la lista, no pasa nada. `404` si la lista no pertenece al cliente, o si el producto no existe/no esta disponible."""
    await wishlist_service.add_item(db, client, collection_uuid, data.product_uuid)

@router.delete("/collections/{collection_uuid}/items/{product_uuid}", status_code=status.HTTP_204_NO_CONTENT, summary="Quitar un producto de una lista")
async def remove_collection_item(client: CurrentClientDep, db: DbDep, collection_uuid: str = Path(), product_uuid: str = Path()):
    """Idempotente: si el producto no estaba en la lista, no pasa nada. `404` si la lista no pertenece al cliente."""
    await wishlist_service.remove_item(db, client, collection_uuid, product_uuid)

# --- Atajo de corazon: siempre apunta a la lista 'Favoritos' (default), creada de forma
# perezosa en el primer PUT - mismo idioma que el minteo silencioso del carrito anonimo. ---

@router.get("/favorites", response_model=WishlistItemListResponse, summary="Listar productos favoritos")
async def list_favorites(
    client: CurrentClientDep,
    db: DbDep,
    limit: int = Query(default=60, ge=1, le=200, description="Cantidad de productos por página (1-200)"),
    offset: int = Query(default=0, ge=0, description="Paginación (inicio)"),
):
    """Nunca crea la lista 'Favoritos' - si el cliente nunca guardo nada, responde vacio."""
    collection = await wishlist_service.get_default_collection_or_none(db, client)
    if collection is None:
        return WishlistItemListResponse(total=0, docs=[])
    return await wishlist_service.list_items(db, client, collection.uuid, limit, offset)

@router.put("/favorites/{product_uuid}", status_code=status.HTTP_200_OK, summary="Agregar un producto a favoritos")
async def add_favorite(client: CurrentClientDep, db: DbDep, product_uuid: str = Path()):
    """Idempotente. Crea la lista 'Favoritos' del cliente si es la primera vez que guarda algo."""
    collection = await wishlist_service.get_or_create_default_collection(db, client)
    await wishlist_service.add_item(db, client, collection.uuid, product_uuid)

@router.delete("/favorites/{product_uuid}", status_code=status.HTTP_204_NO_CONTENT, summary="Quitar un producto de favoritos")
async def remove_favorite(client: CurrentClientDep, db: DbDep, product_uuid: str = Path()):
    """Idempotente: si nunca se guardo o ya se habia quitado, no pasa nada."""
    collection = await wishlist_service.get_default_collection_or_none(db, client)
    if collection is None:
        return
    await wishlist_service.remove_item(db, client, collection.uuid, product_uuid)
