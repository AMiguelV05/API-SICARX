import logging
from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import select, update, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.product import Product
from app.models.client import ClientAccount
from app.models.order import Order
from app.models.review import ProductReview, ReviewHelpfulVote
from app.schemas.review import (
    ReviewCreate, ReviewUpdate, ReviewPublic, ReviewListResponse,
    AdminReviewPublic, AdminReviewListResponse, AdminReviewModerateRequest, AdminReviewReplyRequest,
    HelpfulVoteResponse, SortBy,
)

logger = logging.getLogger(__name__)

_ALL_RATINGS = (1, 2, 3, 4, 5)

def _review_to_public(review: ProductReview, *, product_uuid: str, client_name: str) -> ReviewPublic:
    return ReviewPublic(
        uuid=review.uuid,
        product_uuid=product_uuid,
        client_name=client_name,
        rating=review.rating,
        comment=review.comment,
        is_verified_purchase=review.is_verified_purchase,
        is_hidden=review.is_hidden,
        helpful_count=review.helpful_count,
        admin_reply=review.admin_reply,
        admin_reply_at=review.admin_reply_at,
        created_at=review.created_at,
        updated_at=review.updated_at,
    )

def _review_to_admin_public(review: ProductReview) -> AdminReviewPublic:
    """A diferencia de _review_to_public, asume product/client_account ya cargados
    (selectinload) en la fila - pensada para el listado admin, que cruza muchos productos/clientes a la vez."""
    base = _review_to_public(review, product_uuid=review.product.sicar_uuid, client_name=review.client_account.name)
    return AdminReviewPublic(
        **base.model_dump(),
        client_uuid=review.client_account.uuid,
        client_email=review.client_account.email,
        hidden_reason=review.hidden_reason,
    )

def _capture_relations(review: ProductReview) -> dict:
    """Congela los campos derivados de product/client_account ANTES de un db.refresh() -
    ver comentario en update_review sobre por que no se pueden releer despues de uno."""
    return {
        "product_uuid": review.product.sicar_uuid,
        "client_uuid": review.client_account.uuid,
        "client_email": review.client_account.email,
        "client_name": review.client_account.name,
    }

def _admin_public_from_snapshot(review: ProductReview, snapshot: dict) -> AdminReviewPublic:
    base = _review_to_public(review, product_uuid=snapshot["product_uuid"], client_name=snapshot["client_name"])
    return AdminReviewPublic(
        **base.model_dump(),
        client_uuid=snapshot["client_uuid"],
        client_email=snapshot["client_email"],
        hidden_reason=review.hidden_reason,
    )

async def _get_reviewable_product(db: AsyncSession, product_uuid: str) -> Product:
    """Para crear una reseña - mismo filtro de visibilidad que GET /products/{uuid}."""
    product = await db.scalar(
        select(Product).where(
            Product.sicar_uuid == product_uuid,
            Product.is_deleted == False,
            Product.is_active == True,
        )
    )
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado.")
    return product

async def _get_product_for_reviews(db: AsyncSession, product_uuid: str) -> Product:
    """Para listar reseñas - sin filtro de is_deleted/is_active (a diferencia de
    _get_reviewable_product): las reseñas de un producto que despues se desactivo/elimino
    del catalogo se siguen pudiendo consultar."""
    product = await db.scalar(select(Product).where(Product.sicar_uuid == product_uuid))
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado.")
    return product

async def _check_verified_purchase(db: AsyncSession, client_account_id: int, product_sicar_uuid: str) -> bool:
    """Python-side, sobre las ordenes PAID (no eliminadas) del cliente - Order.items es JSON
    plano (no JSONB), no hay containment de BD que aprovechar aqui (ver CLAUDE.md)."""
    orders = await db.scalars(
        select(Order).where(
            Order.client_account_id == client_account_id,
            Order.status == "PAID",
            Order.deleted_at.is_(None),
        )
    )
    for order in orders:
        for item in (order.items or []):
            if item.get("uuid") == product_sicar_uuid:
                return True
    return False

async def recompute_product_rating(db: AsyncSession, product_id: int) -> None:
    """Recalculo COMPLETO (no delta incremental) de average_rating/reviews_count sobre las
    reseñas no ocultas del producto - evita la clase de bug de deriva que ya se dio con
    reserved/stock (ver CLAUDE.md, "Reserva local de stock"). El volumen de reseñas por
    producto es chico, un recalculo completo es barato y nunca puede desviarse. No hace
    commit - responsabilidad del llamador."""
    avg_rating, count = (
        await db.execute(
            select(func.avg(ProductReview.rating), func.count(ProductReview.id))
            .where(ProductReview.product_id == product_id, ProductReview.is_hidden == False)
        )
    ).one()
    await db.execute(
        update(Product).where(Product.id == product_id).values(average_rating=avg_rating, reviews_count=count or 0)
    )

async def create_review(db: AsyncSession, client: ClientAccount, product_uuid: str, data: ReviewCreate) -> ReviewPublic:
    product = await _get_reviewable_product(db, product_uuid)

    existing = await db.scalar(
        select(ProductReview).where(
            ProductReview.product_id == product.id,
            ProductReview.client_account_id == client.id,
        )
    )
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya reseñaste este producto. Edita tu reseña existente en vez de crear otra.")

    is_verified = await _check_verified_purchase(db, client.id, product.sicar_uuid)

    review = ProductReview(
        product_id=product.id,
        client_account_id=client.id,
        rating=data.rating,
        comment=data.comment,
        is_verified_purchase=is_verified,
    )
    db.add(review)
    await db.flush()
    await recompute_product_rating(db, product.id)
    await db.commit()
    await db.refresh(review)

    logger.info(f"Reseña {review.uuid} creada por cliente {client.email} para producto {product.sicar_uuid} (verified_purchase={is_verified}).")
    return _review_to_public(review, product_uuid=product.sicar_uuid, client_name=client.name)

async def get_owned_review(db: AsyncSession, client: ClientAccount, review_uuid: str) -> ProductReview:
    """404 (no 403) si la reseña no existe o no pertenece al cliente - para no filtrar
    existencia, mismo patron que address_service.get_owned_address."""
    review = await db.scalar(
        select(ProductReview)
        .options(selectinload(ProductReview.product))
        .where(ProductReview.uuid == review_uuid, ProductReview.client_account_id == client.id)
    )
    if not review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reseña no encontrada.")
    return review

async def update_review(db: AsyncSession, client: ClientAccount, review_uuid: str, data: ReviewUpdate) -> ReviewPublic:
    review = await get_owned_review(db, client, review_uuid)
    # Capturado ANTES de refresh(): refresh() expira relaciones ya cargadas (selectinload
    # incluido), y volver a tocarlas despues dispararia un lazy-load sincrono -> MissingGreenlet
    # bajo el ORM async (ver CLAUDE.md, "addresses deliberadamente no usa lazy=selectin").
    product_uuid = review.product.sicar_uuid

    changes = data.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(review, field, value)

    if "rating" in changes:
        await recompute_product_rating(db, review.product_id)

    await db.commit()
    await db.refresh(review)

    logger.info(f"Reseña {review_uuid} actualizada por cliente {client.email}.")
    return _review_to_public(review, product_uuid=product_uuid, client_name=client.name)

async def delete_review(db: AsyncSession, client: ClientAccount, review_uuid: str) -> None:
    review = await get_owned_review(db, client, review_uuid)
    product_id = review.product_id

    await db.delete(review)
    await db.flush()
    await recompute_product_rating(db, product_id)
    await db.commit()

    logger.info(f"Reseña {review_uuid} eliminada por cliente {client.email}.")

async def list_product_reviews(db: AsyncSession, product_uuid: str, limit: int, offset: int, sort_by: SortBy) -> ReviewListResponse:
    product = await _get_product_for_reviews(db, product_uuid)

    base_filter = (ProductReview.product_id == product.id, ProductReview.is_hidden == False)

    total = await db.scalar(select(func.count()).select_from(ProductReview).where(*base_filter))

    order_by = {
        "newest": (ProductReview.created_at.desc(),),
        "highest_rating": (ProductReview.rating.desc(), ProductReview.created_at.desc()),
        "lowest_rating": (ProductReview.rating.asc(), ProductReview.created_at.desc()),
        "most_helpful": (ProductReview.helpful_count.desc(), ProductReview.created_at.desc()),
    }[sort_by]

    result = await db.execute(
        select(ProductReview)
        .options(selectinload(ProductReview.client_account))
        .where(*base_filter)
        .order_by(*order_by)
        .limit(limit)
        .offset(offset)
    )
    reviews = list(result.scalars().all())

    breakdown_rows = await db.execute(
        select(ProductReview.rating, func.count(ProductReview.id))
        .where(*base_filter)
        .group_by(ProductReview.rating)
    )
    rating_breakdown = {r: 0 for r in _ALL_RATINGS}
    rating_breakdown.update({rating: count for rating, count in breakdown_rows.all()})

    return ReviewListResponse(
        total=total or 0,
        docs=[_review_to_public(r, product_uuid=product.sicar_uuid, client_name=r.client_account.name) for r in reviews],
        average_rating=float(product.average_rating) if product.average_rating is not None else None,
        reviews_count=product.reviews_count,
        rating_breakdown=rating_breakdown,
    )

async def list_client_reviews(db: AsyncSession, client: ClientAccount, limit: int, offset: int) -> ReviewListResponse:
    """Historial propio de reseñas - incluye las ocultas (el autor siempre las ve, solo se
    ocultan de la vista publica), mismo espiritu que list_client_orders."""
    base_filter = (ProductReview.client_account_id == client.id,)

    total = await db.scalar(select(func.count()).select_from(ProductReview).where(*base_filter))
    result = await db.execute(
        select(ProductReview)
        .options(selectinload(ProductReview.product))
        .where(*base_filter)
        .order_by(ProductReview.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    reviews = list(result.scalars().all())

    return ReviewListResponse(
        total=total or 0,
        docs=[_review_to_public(r, product_uuid=r.product.sicar_uuid, client_name=client.name) for r in reviews],
        average_rating=None,
        reviews_count=total or 0,
        rating_breakdown={r: 0 for r in _ALL_RATINGS},
    )

async def _get_visible_review(db: AsyncSession, review_uuid: str) -> ProductReview:
    review = await db.scalar(
        select(ProductReview)
        .options(selectinload(ProductReview.product), selectinload(ProductReview.client_account))
        .where(ProductReview.uuid == review_uuid, ProductReview.is_hidden == False)
    )
    if not review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reseña no encontrada.")
    return review

async def mark_helpful(db: AsyncSession, client: ClientAccount, review_uuid: str) -> HelpfulVoteResponse:
    """Idempotente: si el cliente ya la habia marcado, no-op (no duplica el voto ni el conteo)."""
    review = await _get_visible_review(db, review_uuid)

    existing_vote = await db.scalar(
        select(ReviewHelpfulVote).where(
            ReviewHelpfulVote.review_id == review.id, ReviewHelpfulVote.client_account_id == client.id
        )
    )
    if not existing_vote:
        db.add(ReviewHelpfulVote(review_id=review.id, client_account_id=client.id))
        review.helpful_count += 1
        await db.commit()
        await db.refresh(review)

    return HelpfulVoteResponse(review_uuid=review.uuid, helpful_count=review.helpful_count, marked_by_me=True)

async def unmark_helpful(db: AsyncSession, client: ClientAccount, review_uuid: str) -> HelpfulVoteResponse:
    """Idempotente: si el cliente no la habia marcado, no-op."""
    review = await _get_visible_review(db, review_uuid)

    existing_vote = await db.scalar(
        select(ReviewHelpfulVote).where(
            ReviewHelpfulVote.review_id == review.id, ReviewHelpfulVote.client_account_id == client.id
        )
    )
    if existing_vote:
        await db.delete(existing_vote)
        review.helpful_count = max(review.helpful_count - 1, 0)
        await db.commit()
        await db.refresh(review)

    return HelpfulVoteResponse(review_uuid=review.uuid, helpful_count=review.helpful_count, marked_by_me=False)

# --- Admin -----------------------------------------------------------------

async def admin_list_reviews(
    db: AsyncSession, *, product_uuid: str | None, product_sku: str | None, client_email: str | None, client_uuid: str | None,
    rating: int | None, is_hidden: bool | None, has_reply: bool | None, limit: int, offset: int,
) -> AdminReviewListResponse:
    query = select(ProductReview).join(ProductReview.product).join(ProductReview.client_account)
    count_query = select(func.count()).select_from(ProductReview).join(ProductReview.product).join(ProductReview.client_account)

    filters = []
    if product_uuid is not None:
        filters.append(Product.sicar_uuid == product_uuid)
    if product_sku is not None:
        # Case-insensitive exacto (mismo patron que bulk_import_service._resolve_products)
        # - no substring: un admin buscando por SKU ya sabe el codigo exacto del producto.
        filters.append(func.upper(Product.sku) == product_sku.upper())
    if client_email is not None:
        filters.append(ClientAccount.email == client_email.lower())
    if client_uuid is not None:
        filters.append(ClientAccount.uuid == client_uuid)
    if rating is not None:
        filters.append(ProductReview.rating == rating)
    if is_hidden is not None:
        filters.append(ProductReview.is_hidden == is_hidden)
    if has_reply is not None:
        filters.append(ProductReview.admin_reply.isnot(None) if has_reply else ProductReview.admin_reply.is_(None))

    if filters:
        query = query.where(*filters)
        count_query = count_query.where(*filters)

    total = await db.scalar(count_query)
    result = await db.execute(
        query.options(selectinload(ProductReview.product), selectinload(ProductReview.client_account))
        .order_by(ProductReview.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    reviews = list(result.scalars().all())

    return AdminReviewListResponse(total=total or 0, docs=[_review_to_admin_public(r) for r in reviews])

async def _get_review_or_404(db: AsyncSession, review_uuid: str) -> ProductReview:
    review = await db.scalar(
        select(ProductReview)
        .options(selectinload(ProductReview.product), selectinload(ProductReview.client_account))
        .where(ProductReview.uuid == review_uuid)
    )
    if not review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reseña no encontrada.")
    return review

async def admin_moderate_review(db: AsyncSession, review_uuid: str, data: AdminReviewModerateRequest) -> AdminReviewPublic:
    review = await _get_review_or_404(db, review_uuid)
    # Ver comentario en update_review sobre por que se captura esto antes de refresh().
    snapshot = _capture_relations(review)

    changes = data.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(review, field, value)

    if "is_hidden" in changes:
        await recompute_product_rating(db, review.product_id)

    await db.commit()
    await db.refresh(review)

    logger.info(f"Reseña {review_uuid} moderada por admin (isHidden={review.is_hidden}).")
    return _admin_public_from_snapshot(review, snapshot)

async def admin_delete_review(db: AsyncSession, review_uuid: str) -> None:
    review = await _get_review_or_404(db, review_uuid)
    product_id = review.product_id

    await db.delete(review)
    await db.flush()
    await recompute_product_rating(db, product_id)
    await db.commit()

    logger.info(f"Reseña {review_uuid} eliminada permanentemente por admin.")

async def admin_reply_to_review(db: AsyncSession, review_uuid: str, data: AdminReviewReplyRequest) -> AdminReviewPublic:
    review = await _get_review_or_404(db, review_uuid)
    snapshot = _capture_relations(review)

    review.admin_reply = data.comment
    review.admin_reply_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(review)

    logger.info(f"Admin respondio a la reseña {review_uuid}.")
    return _admin_public_from_snapshot(review, snapshot)

async def admin_delete_reply(db: AsyncSession, review_uuid: str) -> AdminReviewPublic:
    review = await _get_review_or_404(db, review_uuid)
    snapshot = _capture_relations(review)

    review.admin_reply = None
    review.admin_reply_at = None

    await db.commit()
    await db.refresh(review)

    logger.info(f"Respuesta de admin eliminada de la reseña {review_uuid}.")
    return _admin_public_from_snapshot(review, snapshot)
