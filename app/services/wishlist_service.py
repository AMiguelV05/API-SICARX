import logging
from fastapi import HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.client import ClientAccount
from app.models.product import Product
from app.models.wishlist import WishlistCollection, WishlistItem
from app.schemas.products import ProductBasic
from app.schemas.wishlist import (
    WishlistCollectionCreate, WishlistCollectionUpdate, WishlistCollectionPublic,
    WishlistItemPublic, WishlistItemListResponse,
)

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION_NAME = "Favoritos"

def _collection_to_public(collection: WishlistCollection, item_count: int) -> WishlistCollectionPublic:
    return WishlistCollectionPublic(
        uuid=collection.uuid,
        name=collection.name,
        is_default=collection.is_default,
        item_count=item_count,
        created_at=collection.created_at,
    )

async def get_owned_collection(db: AsyncSession, client: ClientAccount, collection_uuid: str) -> WishlistCollection:
    """404 (no 403) si la lista no existe o no pertenece al cliente - mismo patron que address_service.get_owned_address."""
    collection = await db.scalar(
        select(WishlistCollection).where(
            WishlistCollection.uuid == collection_uuid,
            WishlistCollection.client_account_id == client.id,
        )
    )
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lista de favoritos no encontrada.")
    return collection

async def get_default_collection_or_none(db: AsyncSession, client: ClientAccount) -> WishlistCollection | None:
    """Para GET /wishlist/favorites - un GET nunca crea fila, mismo idioma que
    cart_service.get_cart_response con carrito ausente."""
    return await db.scalar(
        select(WishlistCollection).where(
            WishlistCollection.client_account_id == client.id,
            WishlistCollection.is_default == True,
        )
    )

async def get_or_create_default_collection(db: AsyncSession, client: ClientAccount) -> WishlistCollection:
    """Crea la coleccion 'Favoritos' de forma perezosa en el primer uso - mismo idioma que
    el carrito anonimo de cart_service (silent-mint-on-first-use). El indice unico parcial
    en el modelo es la red de seguridad real contra una carrera, no esta funcion."""
    collection = await db.scalar(
        select(WishlistCollection).where(
            WishlistCollection.client_account_id == client.id,
            WishlistCollection.is_default == True,
        )
    )
    if collection:
        return collection

    collection = WishlistCollection(client_account_id=client.id, name=DEFAULT_COLLECTION_NAME, is_default=True)
    db.add(collection)
    await db.commit()
    await db.refresh(collection)
    logger.info(f"Lista de favoritos default creada para cliente {client.email}.")
    return collection

async def list_collections(db: AsyncSession, client: ClientAccount) -> list[WishlistCollectionPublic]:
    """Sin paginar - acotado por naturaleza (un cliente no tendra cientos de listas), mismo
    criterio que GET /admin/categories/by-product/{uuid}."""
    result = await db.execute(
        select(WishlistCollection, func.count(WishlistItem.id))
        .outerjoin(WishlistItem, WishlistItem.collection_id == WishlistCollection.id)
        .where(WishlistCollection.client_account_id == client.id)
        .group_by(WishlistCollection.id)
        .order_by(WishlistCollection.is_default.desc(), WishlistCollection.created_at.asc())
    )
    return [_collection_to_public(collection, count) for collection, count in result.all()]

async def create_collection(db: AsyncSession, client: ClientAccount, data: WishlistCollectionCreate) -> WishlistCollectionPublic:
    collection = WishlistCollection(client_account_id=client.id, name=data.name, is_default=False)
    db.add(collection)
    await db.commit()
    await db.refresh(collection)
    logger.info(f"Lista de favoritos '{collection.name}' creada por cliente {client.email}.")
    return _collection_to_public(collection, 0)

async def rename_collection(db: AsyncSession, client: ClientAccount, collection_uuid: str, data: WishlistCollectionUpdate) -> WishlistCollectionPublic:
    collection = await get_owned_collection(db, client, collection_uuid)

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(collection, field, value)

    await db.commit()
    await db.refresh(collection)

    item_count = await db.scalar(select(func.count()).select_from(WishlistItem).where(WishlistItem.collection_id == collection.id))
    logger.info(f"Lista de favoritos {collection_uuid} renombrada por cliente {client.email}.")
    return _collection_to_public(collection, item_count or 0)

async def delete_collection(db: AsyncSession, client: ClientAccount, collection_uuid: str) -> None:
    collection = await get_owned_collection(db, client, collection_uuid)
    if collection.is_default:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No se puede eliminar la lista 'Favoritos'.")

    await db.delete(collection)
    await db.commit()
    logger.info(f"Lista de favoritos {collection_uuid} eliminada por cliente {client.email}.")

async def _get_wishlistable_product(db: AsyncSession, product_uuid: str) -> Product:
    """Mismo filtro de visibilidad que order_service.validate_cart_items/review_service._get_reviewable_product."""
    product = await db.scalar(
        select(Product).where(
            Product.sicar_uuid == product_uuid,
            Product.is_deleted == False,
            Product.is_active == True,
        )
    )
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado.")
    return product

async def add_item(db: AsyncSession, client: ClientAccount, collection_uuid: str, product_uuid: str) -> None:
    """Idempotente: ON CONFLICT DO NOTHING sobre la UniqueConstraint - guardar un producto
    ya guardado no es error, mismo idioma que vehicle_service.assign_products_to_model_years."""
    collection = await get_owned_collection(db, client, collection_uuid)
    product = await _get_wishlistable_product(db, product_uuid)

    stmt = insert(WishlistItem).values(collection_id=collection.id, product_id=product.id)
    stmt = stmt.on_conflict_do_nothing(constraint="ux_wishlist_items_collection_product")
    await db.execute(stmt)
    await db.commit()
    logger.info(f"Producto {product_uuid} guardado en lista {collection_uuid} por cliente {client.email}.")

async def remove_item(db: AsyncSession, client: ClientAccount, collection_uuid: str, product_uuid: str) -> None:
    """Idempotente: sin fila que borrar es un no-op, mismo idioma que PATCH /cart/items."""
    collection = await get_owned_collection(db, client, collection_uuid)
    product = await db.scalar(select(Product).where(Product.sicar_uuid == product_uuid))
    if not product:
        return

    item = await db.scalar(
        select(WishlistItem).where(WishlistItem.collection_id == collection.id, WishlistItem.product_id == product.id)
    )
    if item:
        await db.delete(item)
        await db.commit()
        logger.info(f"Producto {product_uuid} quitado de lista {collection_uuid} por cliente {client.email}.")

async def list_items(db: AsyncSession, client: ClientAccount, collection_uuid: str, limit: int, offset: int) -> WishlistItemListResponse:
    """Enriquecimiento en vivo - mismo espiritu que cart_service._enrich: la fila
    WishlistItem nunca se borra sola porque el producto se desactivo/elimino, la respuesta
    solo marca available=false y omite el detalle, en vez de perder en silencio algo que el
    cliente guardo explicitamente."""
    collection = await get_owned_collection(db, client, collection_uuid)

    total = await db.scalar(select(func.count()).select_from(WishlistItem).where(WishlistItem.collection_id == collection.id))

    result = await db.execute(
        select(WishlistItem)
        .options(selectinload(WishlistItem.product))
        .where(WishlistItem.collection_id == collection.id)
        .order_by(WishlistItem.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    items = list(result.scalars().all())

    docs = []
    for item in items:
        product = item.product
        available = product is not None and product.is_active and not product.is_deleted
        docs.append(WishlistItemPublic(
            productUuid=product.sicar_uuid,
            addedAt=item.created_at,
            available=available,
            product=ProductBasic.model_validate(product) if available else None,
        ))

    return WishlistItemListResponse(total=total or 0, docs=docs)
