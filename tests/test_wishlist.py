"""Cubre wishlist_service.py - listas de favoritos por cliente. La coleccion 'Favoritos'
(is_default=true) se crea de forma perezosa en el primer PUT, ver CLAUDE.md, "Wishlist /
favoritos". add_item/remove_item son idempotentes; la coleccion default no se puede
eliminar; un cliente no puede tocar la lista/items de otro (404, no 403)."""
import uuid

import pytest
from fastapi import HTTPException

from app.models.client import ClientAccount
from app.schemas.wishlist import WishlistCollectionCreate, WishlistCollectionUpdate
from app.services import wishlist_service
from tests.conftest import make_product


def _make_client(**overrides) -> ClientAccount:
    defaults = dict(
        uuid=str(uuid.uuid4()),
        name="Cliente de prueba",
        email=f"cliente-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        is_active=True,
    )
    defaults.update(overrides)
    return ClientAccount(**defaults)


async def test_default_collection_is_created_lazily(db):
    client = _make_client()
    db.add(client)
    await db.flush()

    collections_before = await wishlist_service.list_collections(db, client)
    assert collections_before == []

    collection = await wishlist_service.get_or_create_default_collection(db, client)
    assert collection.is_default is True
    assert collection.name == wishlist_service.DEFAULT_COLLECTION_NAME

    # Segunda llamada no crea una segunda default - reusa la misma fila.
    again = await wishlist_service.get_or_create_default_collection(db, client)
    assert again.id == collection.id


async def test_add_item_is_idempotent(db):
    client = _make_client()
    product = make_product()
    db.add_all([client, product])
    await db.flush()

    collection = await wishlist_service.get_or_create_default_collection(db, client)

    await wishlist_service.add_item(db, client, collection.uuid, product.sicar_uuid)
    await wishlist_service.add_item(db, client, collection.uuid, product.sicar_uuid)  # no-op, no debe fallar

    result = await wishlist_service.list_items(db, client, collection.uuid, limit=60, offset=0)
    assert result.total == 1
    assert result.docs[0].product_uuid == product.sicar_uuid
    assert result.docs[0].available is True


async def test_remove_item_is_idempotent(db):
    client = _make_client()
    product = make_product()
    db.add_all([client, product])
    await db.flush()

    collection = await wishlist_service.get_or_create_default_collection(db, client)
    await wishlist_service.add_item(db, client, collection.uuid, product.sicar_uuid)

    await wishlist_service.remove_item(db, client, collection.uuid, product.sicar_uuid)
    await wishlist_service.remove_item(db, client, collection.uuid, product.sicar_uuid)  # ya no esta, no debe fallar

    result = await wishlist_service.list_items(db, client, collection.uuid, limit=60, offset=0)
    assert result.total == 0


async def test_unavailable_product_marked_not_available_but_row_survives(db):
    client = _make_client()
    product = make_product(is_active=False)
    db.add_all([client, product])
    await db.flush()

    collection = await wishlist_service.get_or_create_default_collection(db, client)
    # add_item exige is_active/is_deleted en el momento de guardar - se guarda mientras
    # estaba disponible y luego se desactiva, para simular el caso real.
    product.is_active = True
    await db.flush()
    await wishlist_service.add_item(db, client, collection.uuid, product.sicar_uuid)

    product.is_active = False
    await db.flush()

    result = await wishlist_service.list_items(db, client, collection.uuid, limit=60, offset=0)
    assert result.total == 1  # la fila sigue ahi
    assert result.docs[0].available is False
    assert result.docs[0].product is None


async def test_cannot_delete_default_collection(db):
    client = _make_client()
    db.add(client)
    await db.flush()

    collection = await wishlist_service.get_or_create_default_collection(db, client)

    with pytest.raises(HTTPException) as exc_info:
        await wishlist_service.delete_collection(db, client, collection.uuid)
    assert exc_info.value.status_code == 409


async def test_named_collection_can_be_created_and_deleted(db):
    client = _make_client()
    db.add(client)
    await db.flush()

    collection = await wishlist_service.create_collection(db, client, WishlistCollectionCreate(name="Cumpleaños"))
    assert collection.is_default is False
    assert collection.item_count == 0

    renamed = await wishlist_service.rename_collection(db, client, collection.uuid, WishlistCollectionUpdate(name="Cumple 2026"))
    assert renamed.name == "Cumple 2026"

    await wishlist_service.delete_collection(db, client, collection.uuid)
    remaining = await wishlist_service.list_collections(db, client)
    assert remaining == []


async def test_one_client_cannot_touch_another_clients_collection(db):
    owner = _make_client()
    intruder = _make_client()
    db.add_all([owner, intruder])
    await db.flush()

    collection = await wishlist_service.get_or_create_default_collection(db, owner)

    with pytest.raises(HTTPException) as exc_info:
        await wishlist_service.get_owned_collection(db, intruder, collection.uuid)
    assert exc_info.value.status_code == 404

    with pytest.raises(HTTPException) as exc_info:
        await wishlist_service.delete_collection(db, intruder, collection.uuid)
    assert exc_info.value.status_code == 404
