import logging
from typing import Optional
from fastapi import APIRouter, Depends, Body, Path, Query, status
from app.core.database import DbDep
from app.core.security import validate_admin_key
from app.schemas.review import AdminReviewPublic, AdminReviewListResponse, AdminReviewModerateRequest, AdminReviewReplyRequest
from app.services import review_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/reviews", tags=["Admin - Reviews"], dependencies=[Depends(validate_admin_key)])

@router.get("", response_model=AdminReviewListResponse, summary="Buscar/listar reseñas")
async def admin_list_reviews(
    db: DbDep,
    product_uuid: Optional[str] = Query(default=None, alias="productUuid"),
    product_sku: Optional[str] = Query(default=None, alias="productSku", description="Coincidencia exacta (sin distinguir mayusculas) contra Product.sku"),
    client_email: Optional[str] = Query(default=None, alias="clientEmail"),
    client_uuid: Optional[str] = Query(default=None, alias="clientUuid"),
    rating: Optional[int] = Query(default=None, ge=1, le=5),
    is_hidden: Optional[bool] = Query(default=None, alias="isHidden"),
    has_reply: Optional[bool] = Query(default=None, alias="hasReply"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Listado admin-wide de reseñas (no solo de un producto), filtrable y paginado - por `productUuid` o `productSku` (coincidencia exacta) para buscar las reseñas de un producto en particular. Incluye ocultas salvo que se filtre `isHidden=false` explicitamente."""
    return await review_service.admin_list_reviews(
        db,
        product_uuid=product_uuid,
        product_sku=product_sku,
        client_email=client_email,
        client_uuid=client_uuid,
        rating=rating,
        is_hidden=is_hidden,
        has_reply=has_reply,
        limit=limit,
        offset=offset,
    )

@router.patch("/{review_uuid}", response_model=AdminReviewPublic, summary="Ocultar/mostrar una reseña")
async def admin_moderate_review(db: DbDep, review_uuid: str = Path(), data: AdminReviewModerateRequest = Body()):
    """Actualizacion parcial (`exclude_unset`): oculta o vuelve a mostrar una reseña. Ocultarla la excluye de inmediato del promedio/desglose publico del producto."""
    return await review_service.admin_moderate_review(db, review_uuid, data)

@router.delete("/{review_uuid}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar permanentemente una reseña")
async def admin_delete_review(db: DbDep, review_uuid: str = Path()):
    """Borrado real (no soft-delete) - para contenido genuinamente abusivo/ilegal. Distinto de ocultar (`PATCH`), que es reversible."""
    await review_service.admin_delete_review(db, review_uuid)

@router.put("/{review_uuid}/reply", response_model=AdminReviewPublic, summary="Crear o reemplazar la respuesta oficial")
async def admin_reply_to_review(db: DbDep, review_uuid: str = Path(), data: AdminReviewReplyRequest = Body()):
    """Una sola respuesta oficial por reseña (no un hilo) - una llamada repetida reemplaza la respuesta anterior."""
    return await review_service.admin_reply_to_review(db, review_uuid, data)

@router.delete("/{review_uuid}/reply", response_model=AdminReviewPublic, summary="Eliminar la respuesta oficial")
async def admin_delete_reply(db: DbDep, review_uuid: str = Path()):
    return await review_service.admin_delete_reply(db, review_uuid)
