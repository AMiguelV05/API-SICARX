import logging
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import validate_api_key
from app.services.taxonomy_service import get_local_taxonomy
from app.schemas.taxonomy import TaxonomyResponse, DepartmentWithCategories, CategoryBasic

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/taxonomy", response_model=TaxonomyResponse, summary="Departamentos y categorías para filtros")
async def get_taxonomy(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(validate_api_key)
):
    """
    Devuelve los departamentos y sus categorías asociadas, para construir filtros en el
    frontend. Se sirve desde la base de datos local con refresco perezoso (cache de 24h).
    """
    departments = await get_local_taxonomy(db)
    return TaxonomyResponse(
        departments=[
            DepartmentWithCategories(
                uuid=d.uuid,
                name=d.name,
                order=d.sort_order,
                categories=[CategoryBasic(uuid=c.uuid, name=c.name) for c in d.categories]
            )
            for d in departments
        ]
    )
