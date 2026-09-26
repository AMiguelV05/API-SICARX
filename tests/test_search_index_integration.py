"""Worker de indexado contra un Typesense REAL - se saltan si TYPESENSE_URL/TYPESENSE_API_KEY
no estan definidos (localmente: `docker compose up -d typesense`, y exportar
TYPESENSE_URL=http://localhost:8108 TYPESENSE_API_KEY=dev-key; en CI hay un contenedor de
servicio, ver .github/workflows/tests.yml).

A diferencia del resto del suite, aqui los datos SI se commitean (el worker abre sus propias
sesiones via AsyncSessionLocal, igual que en produccion), asi que cada prueba borra al
final los productos que creo. Cada prueba usa un alias propio en Typesense y lo borra."""
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, text, update

from app.core import database
from app.models.product import Product
from app.services import search_index, typesense_client
from app.worker import search_index_worker
from tests.conftest import make_product

pytestmark = pytest.mark.skipif(
    not (os.environ.get("TYPESENSE_URL") and os.environ.get("TYPESENSE_API_KEY")),
    reason="Typesense no configurado (TYPESENSE_URL/TYPESENSE_API_KEY)",
)


@pytest.fixture
async def ts_env(monkeypatch):
    """Alias unico por prueba + limpieza de Typesense, de los productos commiteados y del
    pool del engine global (cada prueba corre en su propio event loop)."""
    alias = f"test_products_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(search_index, "ALIAS", alias)
    typesense_client.reset_breaker()
    created: list[str] = []
    yield created
    try:
        collection = await typesense_client.get_alias(alias)
        if collection:
            await typesense_client._request("DELETE", f"/aliases/{alias}", allow_404=True)
            await typesense_client.delete_collection(collection)
        async with database.AsyncSessionLocal() as session:
            if created:
                await session.execute(delete(Product).where(Product.sicar_uuid.in_(created)))
                await session.commit()
    finally:
        await database.engine.dispose()


async def _commit_products(created: list[str], *products: Product) -> list[Product]:
    async with database.AsyncSessionLocal() as session:
        session.add_all(products)
        await session.commit()
    created.extend(p.sicar_uuid for p in products)
    return list(products)


async def _doc(sicar_uuid: str):
    response = await typesense_client._request(
        "GET", f"/collections/{search_index.ALIAS}/documents/{sicar_uuid}", allow_404=True
    )
    return None if response.status_code == 404 else response.json()


async def _dirty(sicar_uuid: str) -> bool:
    async with database.AsyncSessionLocal() as session:
        return await session.scalar(
            select(Product.search_dirty_at.is_not(None)).where(Product.sicar_uuid == sicar_uuid)
        )


async def test_full_rebuild_indexes_active_products_and_swaps_alias(ts_env):
    active, inactive = await _commit_products(
        ts_env,
        make_product(name="Martillo de bola 16 oz", sku="MB-16", brand="Truper"),
        make_product(name="Producto oculto", is_active=False),
    )

    await search_index_worker.full_rebuild("prueba")

    collection = await typesense_client.get_alias(search_index.ALIAS)
    assert collection.startswith(search_index.collection_prefix())
    doc = await _doc(active.sicar_uuid)
    assert doc["name"] == "martillo de bola 16oz"
    assert doc["brand_lower"] == "truper"
    assert await _doc(inactive.sicar_uuid) is None
    # La reconstruccion limpia las marcas que existian al empezar.
    assert not await _dirty(active.sicar_uuid)
    # Los sinonimos sembrados por la migracion van en la coleccion nueva.
    synonyms = await typesense_client.list_synonyms(collection)
    assert len(synonyms) >= 4

    # Una segunda reconstruccion cambia el alias y borra la coleccion anterior.
    await search_index_worker.full_rebuild("prueba 2")
    second = await typesense_client.get_alias(search_index.ALIAS)
    assert second != collection
    gone = await typesense_client._request("GET", f"/collections/{collection}", allow_404=True)
    assert gone.status_code == 404


async def test_drain_pushes_changes_and_removes_deactivated(ts_env):
    keep, drop = await _commit_products(
        ts_env,
        make_product(name="Llave española 10 mm"),
        make_product(name="Pinza de presión 10"),
    )
    await search_index_worker.full_rebuild("prueba")

    async with database.AsyncSessionLocal() as session:
        await session.execute(update(Product).where(Product.sicar_uuid == keep.sicar_uuid).values(name="Llave combinada 10 mm"))
        await session.execute(update(Product).where(Product.sicar_uuid == drop.sicar_uuid).values(is_active=False))
        await session.commit()
    assert await _dirty(keep.sicar_uuid) and await _dirty(drop.sicar_uuid)

    cleared = await search_index_worker.drain_dirty()

    assert cleared == 2
    assert (await _doc(keep.sicar_uuid))["name"] == "llave combinada 10mm"
    assert await _doc(drop.sicar_uuid) is None
    assert not await _dirty(keep.sicar_uuid) and not await _dirty(drop.sicar_uuid)


async def test_write_during_import_keeps_its_dirty_flag(ts_env, monkeypatch):
    """Compare-and-clear: si el producto cambia mientras el worker lo esta empujando, la
    marca nueva no se pierde - el siguiente drain lo vuelve a empujar."""
    (product,) = await _commit_products(ts_env, make_product(name="Taladro 1/2 20 V"))
    await search_index_worker.full_rebuild("prueba")
    async with database.AsyncSessionLocal() as session:
        await session.execute(update(Product).where(Product.sicar_uuid == product.sicar_uuid).values(name="Taladro 1/2 20 V v2"))
        await session.commit()

    original_import = typesense_client.import_documents

    async def import_with_concurrent_write(collection, documents):
        # Otra transaccion (otro momento -> now() distinto) cambia el producto a mitad del import.
        async with database.AsyncSessionLocal() as session:
            await session.execute(update(Product).where(Product.sicar_uuid == product.sicar_uuid).values(name="Taladro 1/2 20 V v3"))
            await session.commit()
        return await original_import(collection, documents)

    monkeypatch.setattr(typesense_client, "import_documents", import_with_concurrent_write)
    await search_index_worker.drain_dirty()
    assert await _dirty(product.sicar_uuid), "la marca de la escritura concurrente se perdio"

    monkeypatch.setattr(typesense_client, "import_documents", original_import)
    await search_index_worker.drain_dirty()
    assert not await _dirty(product.sicar_uuid)
    assert (await _doc(product.sicar_uuid))["name"] == "taladro 1/2 20v v3"


async def test_drain_without_index_is_a_noop(ts_env):
    (product,) = await _commit_products(ts_env, make_product(name="Cinta de aislar"))
    assert await search_index_worker.drain_dirty() == 0
    assert await _dirty(product.sicar_uuid)


async def test_ensure_index_on_startup_rebuilds_only_when_missing(ts_env):
    await _commit_products(ts_env, make_product(name="Foco LED 10W"))
    await search_index_worker.ensure_index_on_startup()
    first = await typesense_client.get_alias(search_index.ALIAS)
    assert first is not None

    await search_index_worker.ensure_index_on_startup()
    assert await typesense_client.get_alias(search_index.ALIAS) == first


async def test_reconcile_removes_synonym_deleted_from_table(ts_env):
    await search_index_worker.full_rebuild("prueba")
    alias = search_index.ALIAS
    await typesense_client.upsert_synonym(alias, "huerfano-de-prueba", {"synonyms": ["foo", "bar"]})

    await search_index_worker.reconcile_synonyms()

    ids = {s["id"] for s in await typesense_client.list_synonyms(alias)}
    assert "huerfano-de-prueba" not in ids
    async with database.AsyncSessionLocal() as session:
        table_ids = set((await session.execute(text("SELECT uuid FROM search_synonyms"))).scalars())
    assert ids == table_ids
