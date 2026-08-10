import uuid
from sqlalchemy import Column, Integer, SmallInteger, String, Text, Boolean, DateTime, ForeignKey, UniqueConstraint, CheckConstraint, func
from sqlalchemy.orm import relationship
from app.core.database import Base

class ProductReview(Base):
    """Reseña de un cliente sobre un producto (rating 1-5 + comentario opcional). Una por
    cliente por producto (UniqueConstraint abajo) - para volver a opinar, el cliente edita
    la existente en vez de crear otra. `is_verified_purchase` se calcula una sola vez al
    crear (ver review_service.create_review); no se recalcula si el cliente compra despues."""
    __tablename__ = "product_reviews"
    __table_args__ = (
        UniqueConstraint("product_id", "client_account_id", name="ux_product_reviews_product_client"),
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_product_reviews_rating_range"),
    )

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String, unique=True, index=True, nullable=False, default=lambda: str(uuid.uuid4()))

    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    # ondelete=CASCADE: a diferencia de Order (registro financiero), una reseña es
    # contenido desechable del cliente - mismo tratamiento que ClientAddress.
    client_account_id = Column(Integer, ForeignKey("client_accounts.id", ondelete="CASCADE"), nullable=False, index=True)

    rating = Column(SmallInteger, nullable=False)
    comment = Column(Text, nullable=True)

    # Calculado al crear: el cliente tenia al menos una orden PAID con este producto.
    is_verified_purchase = Column(Boolean, nullable=False, default=False)

    # Moderacion admin - oculta de la vista publica y del promedio, pero el propio cliente
    # sigue viendola en GET /auth/me/reviews. hidden_reason es texto libre (sin modelo de
    # usuarios admin, igual que Order.accepted_by/cancellation_reason).
    is_hidden = Column(Boolean, nullable=False, default=False)
    hidden_reason = Column(String, nullable=True)

    # Respuesta oficial unica del admin (no un hilo) - columnas inline en vez de tabla aparte.
    admin_reply = Column(Text, nullable=True)
    admin_reply_at = Column(DateTime(timezone=True), nullable=True)

    # Cacheado (igual que Product.sales_count) - mantenido por review_service via la tabla
    # product_review_helpful_votes de abajo, nunca escrito directo desde una request.
    helpful_count = Column(Integer, nullable=False, default=0, server_default="0")

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=func.now())

    # Unidireccionales (sin back_populates): ni Product ni ClientAccount necesitan una
    # coleccion .reviews propia hoy - solo se navega desde ProductReview hacia ellos, para
    # armar ReviewPublic/AdminReviewPublic sin una query aparte por fila (ver review_service.py).
    product = relationship("Product")
    client_account = relationship("ClientAccount")


class ReviewHelpfulVote(Base):
    """Un voto 'util' de un cliente sobre una reseña - existe como tabla (no solo un
    contador) porque hace falta poder distinguir/revertir el voto de UN cliente en
    particular (toggle idempotente), algo que ProductReview.helpful_count por si solo no
    puede hacer."""
    __tablename__ = "product_review_helpful_votes"
    __table_args__ = (
        UniqueConstraint("review_id", "client_account_id", name="ux_review_helpful_votes_review_client"),
    )

    id = Column(Integer, primary_key=True, index=True)
    review_id = Column(Integer, ForeignKey("product_reviews.id", ondelete="CASCADE"), nullable=False, index=True)
    client_account_id = Column(Integer, ForeignKey("client_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
