import logging
from fastapi import APIRouter, Depends, HTTPException, Body, status
from app.core.database import DbDep
from app.core.security import validate_api_key
from app.schemas.search import SearchFilter, SearchResponse
from app.services.catalog_service import search_products

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Search"], dependencies=[Depends(validate_api_key)])

@router.post("/search", response_model=SearchResponse, summary="Buscar productos por sku o nombre")
async def search(db: DbDep, filter_data: SearchFilter = Body()):
    """
    Busca productos cuyo `sku` o `name` contengan `q` (case-insensitive), desde la base
    de datos local. Admite los mismos filtros que `POST /products`.
    """
    try:
        result = await search_products(
            db, filter_data.q, filter_data.limit, filter_data.offset,
            department_uuid=filter_data.department_uuid,
            category_uuid=filter_data.category_uuid,
            taxonomy_uuid=filter_data.taxonomy_uuid,
            vehicle_uuid=filter_data.vehicle_uuid,
            in_stock=filter_data.in_stock,
            sort_by=filter_data.sort_by,
        )
        return result
    except Exception as e:
        logger.error(f"Error al buscar productos: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Ocurrió un error interno al realizar la búsqueda. Intenta más tarde.")
