import logging
from fastapi import APIRouter, Depends, Body, Path, Query, status
from app.core.database import DbDep
from app.core.security import validate_api_key, CurrentClientDep
from app.schemas.review import ReviewCreate, ReviewUpdate, ReviewPublic, ReviewListResponse, HelpfulVoteResponse, SortBy
from app.services import review_service

logger = logging.getLogger(__name__)
# Sin prefix comun: las rutas viven bajo /products/{uuid}/reviews, /reviews/{uuid} y
# /auth/me/reviews - no comparten un segmento inicial estatico (a diferencia de la mayoria
# de los routers de este proyecto), asi que cada una declara su path completo.
router = APIRouter(tags=["Product Reviews"], dependencies=[Depends(validate_api_key)])

@router.get("/products/{product_uuid}/reviews", response_model=ReviewListResponse, summary="Listar reseñas de un producto")
async def list_reviews(
    db: DbDep,
    product_uuid: str = Path(),
    limit: int = Query(default=60, ge=1, le=200, description="Cantidad de reseñas por pagina (1-200)"),
    offset: int = Query(default=0, ge=0, description="Paginacion (inicio)"),
    sort_by: SortBy = Query(default="newest", alias="sortBy", description="newest, highest_rating, lowest_rating o most_helpful"),
):
    """
    Reseñas publicas (no ocultas) de un producto, con el promedio/desglose por estrella ya
    calculado. `404` si el producto no existe (sin importar su estado - una reseña de un
    producto despues desactivado/eliminado se sigue pudiendo consultar).
    """
    return await review_service.list_product_reviews(db, product_uuid, limit, offset, sort_by)

@router.post("/products/{product_uuid}/reviews", response_model=ReviewPublic, status_code=status.HTTP_201_CREATED, summary="Crear una reseña")
async def create_review(client: CurrentClientDep, db: DbDep, product_uuid: str = Path(), data: ReviewCreate = Body()):
    """
    Crea una reseña para el producto indicado (`404` si no existe o no esta disponible).
    `409` si el cliente autenticado ya reseñó este producto - para volver a opinar, usar
    `PATCH /reviews/{uuid}` sobre la reseña existente. `isVerifiedPurchase` se calcula una
    sola vez aqui a partir de las ordenes PAID del cliente.
    """
    return await review_service.create_review(db, client, product_uuid, data)

@router.patch("/reviews/{review_uuid}", response_model=ReviewPublic, summary="Editar la propia reseña")
async def update_review(client: CurrentClientDep, db: DbDep, review_uuid: str = Path(), data: ReviewUpdate = Body()):
    """Actualiza una reseña propia (debe pertenecer al cliente autenticado, si no responde `404`). Todos los campos son opcionales."""
    return await review_service.update_review(db, client, review_uuid, data)

@router.delete("/reviews/{review_uuid}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar la propia reseña")
async def delete_review(client: CurrentClientDep, db: DbDep, review_uuid: str = Path()):
    """Elimina una reseña propia (debe pertenecer al cliente autenticado, si no responde `404`)."""
    await review_service.delete_review(db, client, review_uuid)

@router.put("/reviews/{review_uuid}/helpful", response_model=HelpfulVoteResponse, summary="Marcar una reseña como útil")
async def mark_review_helpful(client: CurrentClientDep, db: DbDep, review_uuid: str = Path()):
    """Idempotente: si el cliente ya la habia marcado, no cambia nada. `404` si la reseña no existe o esta oculta."""
    return await review_service.mark_helpful(db, client, review_uuid)

@router.delete("/reviews/{review_uuid}/helpful", response_model=HelpfulVoteResponse, summary="Quitar el propio voto de útil")
async def unmark_review_helpful(client: CurrentClientDep, db: DbDep, review_uuid: str = Path()):
    """Idempotente: si el cliente no la habia marcado, no cambia nada. `404` si la reseña no existe o esta oculta."""
    return await review_service.unmark_helpful(db, client, review_uuid)

@router.get("/auth/me/reviews", response_model=ReviewListResponse, summary="Listar las propias reseñas")
async def list_my_reviews(
    client: CurrentClientDep,
    db: DbDep,
    limit: int = Query(default=60, ge=1, le=200, description="Cantidad de reseñas por pagina (1-200)"),
    offset: int = Query(default=0, ge=0, description="Paginacion (inicio)"),
):
    """Historial de reseñas del cliente autenticado, mas recientes primero - incluye las que un admin haya ocultado (solo el propio autor las sigue viendo aqui)."""
    return await review_service.list_client_reviews(db, client, limit, offset)
