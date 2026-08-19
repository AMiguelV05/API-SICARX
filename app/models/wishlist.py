import uuid
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Index, UniqueConstraint, func, text
from sqlalchemy.orm import relationship
from app.core.database import Base

class WishlistCollection(Base):
    """Lista de productos guardados de un cliente. Toda cuenta tiene una coleccion default
    ('Favoritos', is_default=True) que es el objetivo fijo del atajo de corazon
    (GET/PUT/DELETE /wishlist/favorites/*) - se crea de forma perezosa en el primer uso,
    mismo patron que el carrito anonimo de cart_service. Ademas puede tener otras
    colecciones con nombre, creadas explicitamente por el cliente."""
    __tablename__ = "wishlist_collections"
    __table_args__ = (
        # Mismo patron que ix_client_addresses_one_default: maximo una default por cliente,
        # garantizado a nivel de BD, no solo en el service layer.
        Index(
            "ix_wishlist_collections_one_default",
            "client_account_id",
            unique=True,
            postgresql_where=text("is_default = true"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String, unique=True, index=True, nullable=False, default=lambda: str(uuid.uuid4()))
    client_account_id = Column(Integer, ForeignKey("client_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    items = relationship("WishlistItem", back_populates="collection", cascade="all, delete-orphan")


class WishlistItem(Base):
    """Un producto guardado dentro de una coleccion - tabla de toggle pura (sin uuid
    propio), direccionada por su padre (collection_id) + el producto, mismo patron que
    ReviewHelpfulVote. UniqueConstraint evita duplicar el mismo producto dos veces en la
    misma lista; el mismo producto SI puede estar en varias listas distintas del cliente."""
    __tablename__ = "wishlist_items"
    __table_args__ = (
        UniqueConstraint("collection_id", "product_id", name="ux_wishlist_items_collection_product"),
    )

    id = Column(Integer, primary_key=True, index=True)
    collection_id = Column(Integer, ForeignKey("wishlist_collections.id", ondelete="CASCADE"), nullable=False, index=True)
    # FK a Product.id (no sicar_uuid), sin ondelete - mismo patron que ProductReview.product_id:
    # los productos nunca se hard-deletean (solo is_deleted=True), no hace falta cascada.
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    collection = relationship("WishlistCollection", back_populates="items")
    product = relationship("Product")
