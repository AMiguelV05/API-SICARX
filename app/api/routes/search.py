import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

from app.core.database import DbDep
from app.core.security import validate_api_key
from app.schemas.search import SearchFilter, SearchResponse, SuggestResponse
from app.services import search_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Search"], dependencies=[Depends(validate_api_key)])


@router.post("/search", response_model=SearchResponse, summary="Buscar productos por sku o nombre")
async def search(db: DbDep, filter_data: SearchFilter = Body()):
    """
    Busca productos por nombre, SKU o marca con el motor de busqueda (Typesense): tolera
    errores de tipeo, plurales, acentos, unidades escritas juntas o separadas (`10mm` /
    `10 mm`) y sinonimos administrados en `/admin/search/synonyms`; un SKU exacto siempre
    aparece primero. Precio y stock siempre vienen de la base de datos local. Si el motor no
    esta disponible, responde igual con la busqueda de la base de datos. Admite los mismos
    filtros que `POST /products`.
    """
    try:
        return await search_service.search(db, filter_data)
    except Exception as e:
        logger.error(f"Error al buscar productos: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Ocurrió un error interno al realizar la búsqueda. Intenta más tarde.")


@router.get("/search/suggest", response_model=SuggestResponse, summary="Sugerencias mientras se escribe")
async def suggest(
    db: DbDep,
    q: Annotated[str, Query(min_length=1, max_length=100, description="Texto escrito hasta ahora")],
    limit: Annotated[int, Query(ge=1, le=10, description="Cantidad maxima de productos sugeridos")] = 6,
):
    """
    Autocompletado: hasta `limit` productos y hasta 4 marcas que coinciden con lo escrito, en
    una sola llamada. Pensado para llamarse con debounce (~200 ms) desde el buscador. Si el
    motor de busqueda no esta disponible, devuelve productos de la base de datos y `brands`
    vacio.
    """
    try:
        return await search_service.suggest(db, q, limit)
    except Exception as e:
        logger.error(f"Error al obtener sugerencias de busqueda: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Ocurrió un error interno al obtener sugerencias. Intenta más tarde.")
