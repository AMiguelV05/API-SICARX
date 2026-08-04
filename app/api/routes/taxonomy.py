import logging
from fastapi import APIRouter, Depends
from app.core.database import DbDep
from app.core.security import validate_api_key
from app.services.taxonomy_service import get_category_tree
from app.schemas.taxonomy import TaxonomyResponse, CategoryNode

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Taxonomy"], dependencies=[Depends(validate_api_key)])


def _to_schema(node) -> CategoryNode:
    return CategoryNode(
        uuid=node.uuid,
        name=node.name,
        slug=node.slug,
        children=[_to_schema(child) for child in node.children],
    )


@router.get("/taxonomy", response_model=TaxonomyResponse, summary="Arbol de categorias para navegacion/filtros")
async def get_taxonomy(db: DbDep):
    """Arbol completo de categorias (PIM propio, ver CLAUDE.md), siempre desde Postgres."""
    roots = await get_category_tree(db)
    return TaxonomyResponse(categories=[_to_schema(node) for node in roots])
