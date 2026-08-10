from typing import Dict, List, Literal, Optional
from datetime import datetime
from pydantic import Field
from app.schemas.base import CamelModel

SortBy = Literal["newest", "highest_rating", "lowest_rating", "most_helpful"]

class ReviewCreate(CamelModel):
    rating: int = Field(ge=1, le=5, description="Calificación de 1 a 5 estrellas")
    comment: Optional[str] = Field(default=None, description="Comentario opcional")

class ReviewUpdate(CamelModel):
    rating: Optional[int] = Field(default=None, ge=1, le=5, description="Nueva calificación de 1 a 5 estrellas")
    comment: Optional[str] = Field(default=None, description="Nuevo comentario")

class ReviewPublic(CamelModel):
    uuid: str
    product_uuid: str = Field(description="sicar_uuid del producto reseñado")
    client_name: str = Field(description="Nombre del cliente autor de la reseña")
    rating: int
    comment: Optional[str]
    is_verified_purchase: bool
    is_hidden: bool = Field(description="true si un admin la ocultó - solo visible aquí para el propio autor (GET /auth/me/reviews)")
    helpful_count: int
    admin_reply: Optional[str] = None
    admin_reply_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

class ReviewListResponse(CamelModel):
    total: int
    docs: List[ReviewPublic]
    average_rating: Optional[float] = Field(default=None, description="Promedio de las reseñas no ocultas del producto (null si no tiene ninguna)")
    reviews_count: int = Field(description="Cantidad de reseñas no ocultas del producto")
    rating_breakdown: Dict[int, int] = Field(description="Cantidad de reseñas no ocultas por calificación, claves 1-5")

class HelpfulVoteResponse(CamelModel):
    review_uuid: str
    helpful_count: int
    marked_by_me: bool = Field(description="true si la cuenta autenticada ya marcó esta reseña como útil")

# --- Vistas/admin ---------------------------------------------------------

class AdminReviewPublic(ReviewPublic):
    """Superconjunto de ReviewPublic para uso admin: agrega clientEmail/clientUuid y hiddenReason."""
    client_uuid: str
    client_email: str
    hidden_reason: Optional[str] = None

class AdminReviewListResponse(CamelModel):
    total: int
    docs: List[AdminReviewPublic]

class AdminReviewModerateRequest(CamelModel):
    is_hidden: Optional[bool] = Field(default=None, description="true para ocultar la reseña de la vista pública, false para volver a mostrarla")
    hidden_reason: Optional[str] = Field(default=None, description="Motivo de la ocultación, texto libre")

class AdminReviewReplyRequest(CamelModel):
    comment: str = Field(min_length=1, description="Respuesta oficial del admin a la reseña")
