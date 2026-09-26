"""Cliente HTTP minimo para Typesense - httpx plano, no el SDK oficial (`typesense`), que es
sincrono (requests); mismo motivo por el que el resto de integraciones de este backend
(Sicar X, Mercado Pago, envia.com) usan httpx directo.

Cortacircuitos en proceso: tras un error de red, timeout o 5xx, Typesense se da por caido
durante BREAKER_SECONDS - asi, con Typesense abajo, cada peticion de busqueda no paga el
timeout antes de caer a Postgres (search_service hace ese fallback). Ver CLAUDE.md,
"Busqueda con Typesense"."""
import json
import logging
import time
from typing import Any, Iterable

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Consultas: cortas a proposito - una busqueda lenta cae a Postgres en vez de colgar al cliente.
QUERY_TIMEOUT = httpx.Timeout(connect=1.0, read=2.0, write=5.0, pool=1.0)
# Importaciones/reconstrucciones del worker: lotes de miles de documentos.
BULK_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=5.0)
BREAKER_SECONDS = 30
# Tope de ids por filter_by en un borrado por lote (la URL crece con cada id).
DELETE_CHUNK = 200

_down_until: float = 0.0
# Solo para pruebas: un httpx.MockTransport en lugar de la red real.
_transport: httpx.AsyncBaseTransport | None = None


class TypesenseError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def is_enabled() -> bool:
    return bool(settings.TYPESENSE_URL and settings.TYPESENSE_API_KEY)


def is_available() -> bool:
    """Configurado y con el cortacircuitos cerrado."""
    return is_enabled() and time.monotonic() >= _down_until


def _trip(reason: str) -> None:
    global _down_until
    _down_until = time.monotonic() + BREAKER_SECONDS
    logger.warning(f"Typesense marcado como caido por {BREAKER_SECONDS}s: {reason}")


def reset_breaker() -> None:
    global _down_until
    _down_until = 0.0


def _client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.TYPESENSE_URL.rstrip("/"),
        headers={"X-TYPESENSE-API-KEY": settings.TYPESENSE_API_KEY},
        timeout=timeout,
        transport=_transport,
    )


async def _request(method: str, path: str, *, timeout: httpx.Timeout = QUERY_TIMEOUT,
                   allow_404: bool = False, **kwargs) -> httpx.Response:
    """Una peticion a Typesense. Error de red/timeout/5xx -> abre el cortacircuitos y lanza
    TypesenseError. 404 -> se devuelve tal cual si allow_404 (el llamador decide), si no
    TypesenseError. Otro 4xx -> TypesenseError con el mensaje real de Typesense."""
    if not is_enabled():
        raise TypesenseError("Typesense no esta configurado (TYPESENSE_URL/TYPESENSE_API_KEY).")
    try:
        async with _client(timeout) as client:
            response = await client.request(method, path, **kwargs)
    except httpx.HTTPError as e:
        _trip(f"{type(e).__name__} en {method} {path}")
        raise TypesenseError(f"Error de red con Typesense ({type(e).__name__}): {e!r}") from e

    if response.status_code >= 500:
        _trip(f"HTTP {response.status_code} en {method} {path}")
        raise TypesenseError(f"Typesense respondio {response.status_code}: {response.text[:300]}", response.status_code)
    if response.status_code == 404 and allow_404:
        return response
    if response.status_code >= 400:
        raise TypesenseError(f"Typesense rechazo {method} {path}: {response.status_code} {response.text[:300]}", response.status_code)
    return response


# --- Busqueda ---------------------------------------------------------------------------

async def search(collection: str, params: dict[str, Any]) -> dict[str, Any]:
    response = await _request("GET", f"/collections/{collection}/documents/search", params=params)
    return response.json()


async def multi_search(searches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Varias busquedas en una ida y vuelta. Cada resultado puede traer su propio `error`
    (Typesense responde 200 aunque una busqueda individual falle) - se convierte en
    TypesenseError para que el llamador haga fallback igual que ante cualquier otro error."""
    response = await _request("POST", "/multi_search", json={"searches": searches})
    results = response.json().get("results", [])
    for result in results:
        if "error" in result:
            raise TypesenseError(f"Typesense rechazo una busqueda de multi_search: {result.get('error')}", result.get("code"))
    return results


# --- Documentos -------------------------------------------------------------------------

async def import_documents(collection: str, documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Upsert por lote (JSONL). Devuelve un resultado por documento, en el mismo orden -
    Typesense responde 200 aunque algunos documentos individuales fallen."""
    if not documents:
        return []
    body = "\n".join(json.dumps(doc, ensure_ascii=False) for doc in documents)
    response = await _request(
        "POST", f"/collections/{collection}/documents/import", timeout=BULK_TIMEOUT,
        params={"action": "upsert"}, content=body.encode("utf-8"),
        headers={"Content-Type": "text/plain"},
    )
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


async def delete_documents(collection: str, ids: Iterable[str]) -> int:
    """Borra por id. Ids que ya no existan en Typesense no son error."""
    ids = list(ids)
    deleted = 0
    for i in range(0, len(ids), DELETE_CHUNK):
        chunk = ids[i:i + DELETE_CHUNK]
        filter_by = "id:[" + ",".join(f"`{_strip_backticks(x)}`" for x in chunk) + "]"
        response = await _request(
            "DELETE", f"/collections/{collection}/documents", timeout=BULK_TIMEOUT,
            params={"filter_by": filter_by, "batch_size": len(chunk)}, allow_404=True,
        )
        if response.status_code != 404:
            deleted += response.json().get("num_deleted", 0)
    return deleted


# --- Colecciones, alias, stopwords --------------------------------------------------------

async def create_collection(schema: dict[str, Any]) -> None:
    await _request("POST", "/collections", json=schema)


async def delete_collection(name: str) -> None:
    await _request("DELETE", f"/collections/{name}", allow_404=True)


async def get_alias(name: str) -> str | None:
    """Nombre de la coleccion fisica a la que apunta el alias, o None si no existe."""
    response = await _request("GET", f"/aliases/{name}", allow_404=True)
    if response.status_code == 404:
        return None
    return response.json().get("collection_name")


async def upsert_alias(name: str, collection: str) -> None:
    await _request("PUT", f"/aliases/{name}", json={"collection_name": collection})


async def upsert_stopwords(stopwords_id: str, words: list[str]) -> None:
    await _request("PUT", f"/stopwords/{stopwords_id}", json={"stopwords": words})


# --- Sinonimos ----------------------------------------------------------------------------
# Aceptan el alias como `collection` (verificado en la Fase 0).

async def list_synonyms(collection: str) -> list[dict[str, Any]]:
    response = await _request("GET", f"/collections/{collection}/synonyms")
    return response.json().get("synonyms", [])


async def upsert_synonym(collection: str, synonym_id: str, body: dict[str, Any]) -> None:
    await _request("PUT", f"/collections/{collection}/synonyms/{synonym_id}", json=body)


async def delete_synonym(collection: str, synonym_id: str) -> None:
    await _request("DELETE", f"/collections/{collection}/synonyms/{synonym_id}", allow_404=True)


def _strip_backticks(value: str) -> str:
    """Typesense no tiene forma de escapar un backtick dentro de un valor entre backticks;
    los valores que pasan por aqui (uuids, ids) nunca los traen, pero se quitan por si acaso."""
    return value.replace("`", "")
