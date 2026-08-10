import uuid
from sqlalchemy import Column, Integer, String, Numeric, Boolean, DateTime, ForeignKey, Table, Index, UniqueConstraint, func
from app.core.database import Base

# N:M cupon<->categoria, solo relevante cuando Coupon.scope_type == "CATEGORY" - la
# pertenencia scope_type/junction correcta se valida en coupon_service, no aqui (sin
# precedente de CHECK/trigger cross-tabla en este codebase).
coupon_categories = Table(
    "coupon_categories", Base.metadata,
    Column("coupon_id", Integer, ForeignKey("coupons.id"), primary_key=True),
    Column("category_uuid", String, ForeignKey("categories.uuid"), primary_key=True),
)

# N:M cupon<->producto, solo relevante cuando scope_type == "PRODUCT". product_id (no
# sicar_uuid), igual que product_categories/product_vehicles.
coupon_products = Table(
    "coupon_products", Base.metadata,
    Column("coupon_id", Integer, ForeignKey("coupons.id"), primary_key=True),
    Column("product_id", Integer, ForeignKey("products.id"), primary_key=True),
)

# N:M cupon<->cliente elegible ("codigos asignados"). Vacia = cupon publico (cualquier
# cliente puede intentarlo, sujeto a las demas reglas); no vacia = solo estos clientes.
coupon_assigned_clients = Table(
    "coupon_assigned_clients", Base.metadata,
    Column("coupon_id", Integer, ForeignKey("coupons.id"), primary_key=True),
    Column("client_account_id", Integer, ForeignKey("client_accounts.id"), primary_key=True),
)


class Coupon(Base):
    __tablename__ = "coupons"

    id = Column(Integer, primary_key=True, index=True)
    # Identificador publico para /admin/coupons/*, distinto de `code` (lo que el cliente
    # teclea) - mismo split id/uuid que Order/Cart, aqui con una tercera identidad de por medio.
    uuid = Column(String, unique=True, index=True, nullable=False, default=lambda: str(uuid.uuid4()))
    # Siempre normalizado (strip+upper) por coupon_service antes de guardar/comparar.
    code = Column(String, unique=True, index=True, nullable=False)

    discount_type = Column(String, nullable=False)  # "PERCENTAGE" | "FIXED_AMOUNT"
    discount_value = Column(Numeric(10, 2), nullable=False)
    # Tope de monto descontado - solo tiene sentido para PERCENTAGE (validado en el schema).
    max_discount_amount = Column(Numeric(10, 2), nullable=True)

    scope_type = Column(String, nullable=False, default="ORDER")  # "ORDER" | "CATEGORY" | "PRODUCT"

    min_order_amount = Column(Numeric(10, 2), nullable=True)
    starts_at = Column(DateTime(timezone=True), nullable=True)
    ends_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    max_total_uses = Column(Integer, nullable=True)
    max_uses_per_client = Column(Integer, nullable=True)
    first_purchase_only = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=func.now())


class CouponRedemption(Base):
    """Registro de uso de un cupon, uno por orden (UniqueConstraint en order_id - sin
    stacking). Ciclo de vida PENDING (reservado al crear la orden) -> CONFIRMED (la orden
    llego a PAID, consumo permanente incluso si se cancela/reembolsa despues - decision
    deliberada anti-abuso) o RELEASED (la orden nunca llego a PAID, cupo liberado)."""
    __tablename__ = "coupon_redemptions"
    __table_args__ = (
        UniqueConstraint("order_id", name="ux_coupon_redemptions_order_id"),
        Index("ix_coupon_redemptions_coupon_status", "coupon_id", "status"),
        Index("ix_coupon_redemptions_coupon_client_status", "coupon_id", "client_account_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    coupon_id = Column(Integer, ForeignKey("coupons.id"), nullable=False, index=True)
    client_account_id = Column(Integer, ForeignKey("client_accounts.id"), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    status = Column(String, nullable=False, default="PENDING")  # PENDING | CONFIRMED | RELEASED

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=func.now())
