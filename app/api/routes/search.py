import logging
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import validate_api_key
from app.schemas.search import SearchFilter, SearchResponse
from app.services.catalog_service import search_products

logger = logging.getLogger(__name__)
router = APIRouter()

@router.post("/search", response_model=SearchResponse, summary="Buscar productos por sku o nombre")
async def search(
    filter_data: SearchFilter = Body(...),
    db: AsyncSession = Depends(get_db),
    _ : str = Depends(validate_api_key)
):
    """
    Busca productos cuyo `sku` o `name` contengan el texto de `q` (sin distinguir
    mayúsculas/minúsculas). Sirve desde la base de datos local, igual que `POST /catalog`.
    Admite los mismos filtros `department_uuid`/`category_uuid` que `POST /catalog`, y
    `in_stock` para mostrar solo productos con stock > 0.
    """
    try:
        result = await search_products(
            db, filter_data.q, filter_data.limit, filter_data.offset,
            department_uuid=filter_data.department_uuid,
            category_uuid=filter_data.category_uuid,
            in_stock=filter_data.in_stock,
        )
        return result
    except Exception as e:
        logger.error(f"Error al buscar productos: {str(e)}")
        raise HTTPException(status_code=400, detail="No se pudo realizar la búsqueda. Intenta nuevamente.")
