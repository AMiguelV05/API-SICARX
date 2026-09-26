"""Sinonimos del buscador: tabla search_synonyms (fuente de verdad) <-> Typesense (copia).

Esta primera parte es el lado de lectura/reconciliacion, usado por el worker de indexado
(search_index_worker). El CRUD de /v1/admin/search/synonyms vive en este mismo modulo para
compartir `to_typesense` - ver CLAUDE.md, "Busqueda con Typesense"."""
import logging
import re
import uuid as uuid_lib
from typing import Any

from fastapi import HTTPException
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_user import AdminUser
from app.models.search_synonym import SearchSynonym
from app.services import search_index, typesense_client
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


# --- CRUD de /v1/admin/search/synonyms -------------------------------------------------------
# Postgres primero (commit en la ruta), Typesense despues y best-effort: una falla nunca
# revierte el guardado - la reconcilia scheduled_synonyms_job del worker en <= 5 minutos.

MAX_WORD_LENGTH = 50


def _clean_words(words: list[str]) -> list[str]:
    """Normaliza, descarta vacios y colapsa repetidos conservando el orden."""
    cleaned = [normalize_synonym_word(w) for w in words]
    return list(dict.fromkeys(w for w in cleaned if w))


def _validate(root: str | None, words: list[str]) -> None:
    for w in ([root] if root else []) + words:
        if len(w) > MAX_WORD_LENGTH:
            raise HTTPException(status_code=422, detail=f"'{w}' excede {MAX_WORD_LENGTH} caracteres.")
    if root is not None:
        if root in words:
            raise HTTPException(status_code=422, detail=f"La raiz '{root}' no puede repetirse en su propia lista de sinonimos.")
        if not words:
            raise HTTPException(status_code=422, detail="Un sinonimo de una direccion necesita al menos una palabra ademas de la raiz.")
    elif len(words) < 2:
        raise HTTPException(status_code=422, detail="Un sinonimo multi-direccional necesita al menos dos palabras distintas.")


async def _ensure_not_duplicate(db: AsyncSession, root: str | None, words: list[str], exclude_id: int | None = None) -> None:
    """Misma raiz y el mismo conjunto de palabras (sin importar el orden) = duplicado."""
    stmt = select(SearchSynonym.id).where(
        SearchSynonym.root.is_(None) if root is None else SearchSynonym.root == root,
        SearchSynonym.synonyms.contains(words),
        func.jsonb_array_length(SearchSynonym.synonyms) == len(words),
    )
    if exclude_id is not None:
        stmt = stmt.where(SearchSynonym.id != exclude_id)
    if await db.scalar(stmt.limit(1)):
        raise HTTPException(status_code=409, detail="Ya existe un sinonimo identico (misma raiz y mismas palabras).")


async def list_synonym_entries(db: AsyncSession, q: str | None, limit: int, offset: int) -> tuple[int, list[SearchSynonym]]:
    stmt = select(SearchSynonym)
    if q:
        pattern = f"%{normalize_synonym_word(q)}%"
        stmt = stmt.where(or_(SearchSynonym.root.ilike(pattern), cast(SearchSynonym.synonyms, String).ilike(pattern)))
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await db.execute(
        stmt.order_by(SearchSynonym.updated_at.desc(), SearchSynonym.id.desc()).limit(limit).offset(offset)
    )).scalars().all()
    return total, list(rows)


async def get_synonym_entry(db: AsyncSession, synonym_uuid: str) -> SearchSynonym:
    row = await db.scalar(select(SearchSynonym).where(SearchSynonym.uuid == synonym_uuid))
    if row is None:
        raise HTTPException(status_code=404, detail="Sinonimo no encontrado.")
    return row


async def create_synonym_entry(db: AsyncSession, admin: AdminUser, root: str | None, words: list[str]) -> SearchSynonym:
    clean_root = normalize_synonym_word(root) if root is not None else None
    clean_root = clean_root or None
    clean_words = _clean_words(words)
    _validate(clean_root, clean_words)
    await _ensure_not_duplicate(db, clean_root, clean_words)
    row = SearchSynonym(uuid=str(uuid_lib.uuid4()), root=clean_root, synonyms=clean_words, updated_by_admin_id=admin.id)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def update_synonym_entry(db: AsyncSession, admin: AdminUser, synonym_uuid: str, fields: dict[str, Any]) -> tuple[SearchSynonym, dict[str, Any]]:
    """`fields` viene de model_dump(exclude_unset=True). Devuelve (fila, estado anterior)."""
    row = await get_synonym_entry(db, synonym_uuid)
    before = {"root": row.root, "synonyms": list(row.synonyms)}
    root = row.root
    if "root" in fields:
        root = (normalize_synonym_word(fields["root"]) or None) if fields["root"] is not None else None
    words = _clean_words(fields["synonyms"]) if fields.get("synonyms") is not None else list(row.synonyms)
    _validate(root, words)
    await _ensure_not_duplicate(db, root, words, exclude_id=row.id)
    row.root, row.synonyms, row.updated_by_admin_id = root, words, admin.id
    row.updated_at = func.now()
    await db.flush()
    await db.refresh(row)
    return row, before


async def delete_synonym_entry(db: AsyncSession, synonym_uuid: str) -> dict[str, Any]:
    """Borrado real. Devuelve lo borrado, para la bitacora."""
    row = await get_synonym_entry(db, synonym_uuid)
    snapshot = {"root": row.root, "synonyms": list(row.synonyms)}
    await db.delete(row)
    await db.flush()
    return snapshot


async def push_to_search(row: SearchSynonym) -> bool:
    """Best-effort, despues del commit. False (y el cambio queda para la reconciliacion) si
    Typesense no esta configurado, esta caido o no hay indice todavia."""
    if not typesense_client.is_available():
        return False
    try:
        await typesense_client.upsert_synonym(search_index.ALIAS, row.uuid, to_typesense(row))
        return True
    except typesense_client.TypesenseError as e:
        logger.warning(f"No se pudo aplicar el sinonimo {row.uuid} en Typesense (se reconcilia despues): {e}")
        return False


async def remove_from_search(synonym_uuid: str) -> bool:
    if not typesense_client.is_available():
        return False
    try:
        await typesense_client.delete_synonym(search_index.ALIAS, synonym_uuid)
        return True
    except typesense_client.TypesenseError as e:
        logger.warning(f"No se pudo borrar el sinonimo {synonym_uuid} de Typesense (se reconcilia despues): {e}")
        return False
