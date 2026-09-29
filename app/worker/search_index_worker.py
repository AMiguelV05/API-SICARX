"""Mantiene el indice de Typesense al dia con Postgres. Corre solo en el proceso `worker`
(registrado en sync_task.py); `api` solo consulta. Ver CLAUDE.md, "Busqueda con Typesense".

- drain_dirty (cada 30s): empuja los productos marcados por los triggers de Postgres
  (products.search_dirty_at) y limpia la marca solo si no cambio mientras tanto.
- full_rebuild (semanal, y al arrancar si hace falta): coleccion nueva completa -> cambio
  atomico del alias -> borrado de la vieja. Repara cualquier deriva. Semanal y no nocturna:
  cada reconstruccion sube de forma permanente la memoria del contenedor de Typesense
  (medido en Railway: ~0.7 -> ~1.1 GB en 3 noches), y el drain ya lo mantiene al dia.
- reconcile_synonyms (cada 5 min): repara un push de sinonimos fallido desde la API.

Todo es no-op si TYPESENSE_URL/TYPESENSE_API_KEY no estan configurados."""
import asyncio
import logging

from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.core.error_tracking import capture_exception
from app.services import search_index, synonym_service, typesense_client

logger = logging.getLogger(__name__)

DRAIN_BATCH_SIZE = 1000
DRAIN_MAX_BATCHES_PER_TICK = 20
REBUILD_PAGE_SIZE = 2000

# drain_dirty y full_rebuild no deben traslaparse: un drain durante una reconstruccion
# empujaria cambios a la coleccion VIEJA y limpiaria su marca, y la nueva (ya leida para esa
# fila) quedaria con la version anterior tras el cambio de alias.
_index_lock = asyncio.Lock()


async def drain_dirty() -> int:
    """Empuja a Typesense los productos pendientes. Devuelve cuantos se limpiaron."""
    if not typesense_client.is_enabled() or _index_lock.locked():
        return 0
    async with _index_lock:
        alias = search_index.ALIAS
        if await typesense_client.get_alias(alias) is None:
            # Sin indice todavia: la reconstruccion de arranque/semanal lo crea completo.
            return 0

        total_cleared = 0
        for _ in range(DRAIN_MAX_BATCHES_PER_TICK):
            async with AsyncSessionLocal() as session:
                rows = (await session.execute(
                    text("SELECT id, sicar_uuid, search_dirty_at, is_active, is_deleted FROM products "
                         "WHERE search_dirty_at IS NOT NULL ORDER BY search_dirty_at LIMIT :n"),
                    {"n": DRAIN_BATCH_SIZE},
                )).all()
                if not rows:
                    break

                live_ids = [r.id for r in rows if r.is_active and not r.is_deleted]
                gone = [r.sicar_uuid for r in rows if not (r.is_active and not r.is_deleted)]

                ok: set[str] = set()
                documents = await search_index.fetch_documents_by_ids(session, live_ids)
                if documents:
                    results = await typesense_client.import_documents(alias, documents)
                    for doc, result in zip(documents, results):
                        if result.get("success"):
                            ok.add(doc["id"])
                        else:
                            logger.warning(f"Typesense rechazo el producto {doc['id']}: {result.get('error')}")
                # Un producto activo que ya no devolvio documento (se desactivo entre las dos
                # consultas) sigue marcado; el siguiente tick lo trata como `gone`.
                if gone:
                    await typesense_client.delete_documents(alias, gone)
                    ok.update(gone)

                cleared = [(r.id, r.search_dirty_at) for r in rows if r.sicar_uuid in ok]
                if cleared:
                    # Compare-and-clear: solo si search_dirty_at sigue siendo el que se leyo -
                    # una escritura que cayo durante el import deja su marca nueva intacta.
                    await session.execute(
                        text("UPDATE products p SET search_dirty_at = NULL "
                             "FROM unnest(CAST(:ids AS integer[]), CAST(:ts AS timestamptz[])) AS s(id, ts) "
                             "WHERE p.id = s.id AND p.search_dirty_at = s.ts"),
                        {"ids": [c[0] for c in cleared], "ts": [c[1] for c in cleared]},
                    )
                    await session.commit()
                total_cleared += len(cleared)

                if len(rows) < DRAIN_BATCH_SIZE or not cleared:
                    # Ultimo lote, o nada se pudo limpiar (no girar en vacio sobre filas que
                    # Typesense sigue rechazando - se reintentan en el siguiente tick).
                    break

        if total_cleared:
            logger.info(f"Indice de busqueda: {total_cleared} productos actualizados en Typesense.")
        return total_cleared


async def full_rebuild(reason: str) -> None:
    """Coleccion nueva con todo el catalogo activo -> sinonimos -> cambio de alias -> borra
    la vieja. Si algo falla antes del cambio de alias, la coleccion nueva se borra y el
    alias sigue apuntando a la anterior (la busqueda no se entera)."""
    if not typesense_client.is_enabled():
        return
    async with _index_lock:
        alias = search_index.ALIAS
        new_name = search_index.new_collection_name()
        logger.info(f"Reconstruyendo el indice de busqueda ({reason}) en {new_name}...")

        async with AsyncSessionLocal() as session:
            # clock_timestamp(), no now(): now() es el inicio de la transaccion.
            started = await session.scalar(text("SELECT clock_timestamp()"))

        await typesense_client.create_collection(search_index.build_schema(new_name))
        try:
            await typesense_client.upsert_stopwords(search_index.STOPWORDS_ID, search_index.STOPWORDS)
            indexed, failed, after_id = 0, 0, 0
            while True:
                async with AsyncSessionLocal() as session:
                    documents, after_id = await search_index.fetch_document_page(session, after_id, REBUILD_PAGE_SIZE)
                if not documents:
                    break
                results = await typesense_client.import_documents(new_name, documents)
                bad = [r for r in results if not r.get("success")]
                failed += len(bad)
                indexed += len(documents) - len(bad)
                if bad:
                    logger.warning(f"Reconstruccion: Typesense rechazo {len(bad)} productos, p. ej. {bad[0].get('error')}")

            async with AsyncSessionLocal() as session:
                await synonym_service.apply_synonyms(session, new_name)

            previous = await typesense_client.get_alias(alias)
            await typesense_client.upsert_alias(alias, new_name)
        except Exception:
            await typesense_client.delete_collection(new_name)
            raise

        if previous and previous != new_name:
            await typesense_client.delete_collection(previous)

        # Lo que se marco durante la reconstruccion (search_dirty_at > started) conserva su
        # marca y lo empuja el siguiente drain.
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("UPDATE products SET search_dirty_at = NULL WHERE search_dirty_at <= :started"),
                {"started": started},
            )
            await session.commit()

        logger.info(f"Indice de busqueda reconstruido ({reason}): {indexed} productos, {failed} rechazados, alias {alias} -> {new_name}.")


async def reconcile_synonyms() -> None:
    if not typesense_client.is_enabled():
        return
    if await typesense_client.get_alias(search_index.ALIAS) is None:
        return
    async with AsyncSessionLocal() as session:
        await synonym_service.apply_synonyms(session, search_index.ALIAS)


async def ensure_index_on_startup() -> None:
    """Reconstruye si el alias no existe (primer despliegue, volumen de Typesense perdido) o
    apunta a otra SCHEMA_VERSION; si no, solo reaplica stopwords y sinonimos."""
    if not typesense_client.is_enabled():
        logger.info("Typesense no configurado: el indice de busqueda queda deshabilitado.")
        return
    current = await typesense_client.get_alias(search_index.ALIAS)
    if current is None or not current.startswith(search_index.collection_prefix()):
        await full_rebuild("startup" if current is None else f"schema nuevo (antes {current})")
        return
    await typesense_client.upsert_stopwords(search_index.STOPWORDS_ID, search_index.STOPWORDS)
    await reconcile_synonyms()


# --- Envoltorios para el scheduler: nunca propagan, reportan y siguen ---------------------

async def scheduled_drain_job() -> None:
    try:
        await drain_dirty()
    except Exception as e:
        logger.error(f"Fallo al drenar el indice de busqueda: {e!r}")
        capture_exception(e, job="search_index_drain")


async def scheduled_rebuild_job() -> None:
    try:
        await full_rebuild("semanal")
    except Exception as e:
        logger.error(f"Fallo la reconstruccion semanal del indice de busqueda: {e!r}")
        capture_exception(e, job="search_index_rebuild")


async def scheduled_synonyms_job() -> None:
    try:
        await reconcile_synonyms()
    except Exception as e:
        logger.error(f"Fallo al reconciliar los sinonimos de busqueda: {e!r}")
        capture_exception(e, job="search_synonyms_reconcile")
