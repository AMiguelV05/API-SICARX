"""Sinonimos del buscador: tabla search_synonyms (fuente de verdad) <-> Typesense (copia).

Esta primera parte es el lado de lectura/reconciliacion, usado por el worker de indexado
(search_index_worker). El CRUD de /v1/admin/search/synonyms vive en este mismo modulo para
compartir `to_typesense` - ver CLAUDE.md, "Busqueda con Typesense"."""
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.search_synonym import SearchSynonym
from app.services import typesense_client
from app.services.search_index import normalize_search_text

logger = logging.getLogger(__name__)

_SPACES_RE = re.compile(r"\s+")


def normalize_synonym_word(word: str) -> str:
    """Misma normalizacion que nombres y consultas (los sinonimos se aplican sobre la
    consulta ya normalizada), mas recorte y colapso de espacios internos. Una frase de
    varias palabras (`llave allen`) se permite."""
    return _SPACES_RE.sub(" ", normalize_search_text(word)).strip()


def to_typesense(row: SearchSynonym) -> dict[str, Any]:
    body: dict[str, Any] = {"synonyms": list(row.synonyms)}
    if row.root:
        body["root"] = row.root
    return body


async def apply_synonyms(session: AsyncSession, collection: str) -> tuple[int, int]:
    """Deja los sinonimos de `collection` (nombre fisico o alias) identicos a la tabla:
    upsert de cada fila y borrado de cualquier sinonimo de Typesense cuyo id ya no este en
    la tabla. Devuelve (subidos, borrados). Barato: decenas/cientos de filas."""
    rows = (await session.execute(select(SearchSynonym))).scalars().all()
    wanted = {row.uuid: to_typesense(row) for row in rows}

    for synonym_id, body in wanted.items():
        await typesense_client.upsert_synonym(collection, synonym_id, body)

    existing = await typesense_client.list_synonyms(collection)
    stale = [s["id"] for s in existing if s.get("id") not in wanted]
    for synonym_id in stale:
        await typesense_client.delete_synonym(collection, synonym_id)

    if stale:
        logger.info(f"Sinonimos reconciliados en {collection}: {len(wanted)} subidos, {len(stale)} borrados.")
    return len(wanted), len(stale)
