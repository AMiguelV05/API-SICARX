from decimal import Decimal
from sqlalchemy import Column, Integer, String, Numeric, Boolean, Text, JSON, DateTime, ForeignKey, Index, text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.hybrid import hybrid_property
from app.core.database import Base

class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        # jsonb_path_ops: solo se necesita el operador de contencion (@>), da un indice 2-3x mas chico que el default.
        Index("ix_products_tags_gin", "tags", postgresql_using="gin", postgresql_ops={"tags": "jsonb_path_ops"}),
        Index("ix_products_attributes_gin", "attributes", postgresql_using="gin", postgresql_ops={"attributes": "jsonb_path_ops"}),
        # Declarados aqui (no solo en la migracion que los creo) para que autogenerate no proponga su drop como falso positivo - ver 806cd48b3b2a.
        Index("ix_products_sku_trgm", "sku", postgresql_using="gin", postgresql_ops={"sku": "gin_trgm_ops"}),
        Index("ix_products_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}),
        # Parcial: solo cubre lo que consulta GET /products/best-sellers y sort_by=relevance (mismo patron que ix_client_addresses_one_default en client.py).
        Index("ix_products_sales_count", "sales_count", postgresql_where=text("is_deleted = false AND is_active = true")),
        # Expresion, no columna: debe coincidir EXACTAMENTE con available_stock.expression
        # (GREATEST(stock - reserved, 0)) para que el planner la use en los filtros in_stock
        # de catalog_service.py (Product.available_stock > 0) - ver migracion 3a208e2812e1.
        Index("ix_products_available_stock", text("GREATEST(stock - reserved, 0)"), postgresql_where=text("is_deleted = false AND is_active = true")),
    )

    id = Column(Integer, primary_key=True, index=True)

    sicar_uuid = Column(String, unique=True, index=True, nullable=False)
    sku = Column(String, nullable=True)
    additional_skus = Column(JSON, nullable=True)

    name = Column(String, nullable=False)
    description_details = Column(Text, nullable=True)
    image_url = Column(String, nullable=True)

    tags = Column(JSONB, nullable=True)  # JSONB (no JSON): permite filtrar por contencion con indice GIN
    additional_images = Column(JSON, nullable=True)  # Listado de URLs de listImages
    sales_unit_uuid = Column(String, nullable=True)  # Para saber si se vende por PZA, MTR, KGS
    unit_short_name = Column(String, nullable=True)  # Nombre corto resuelto (p.ej. "PZA"/"MTR") via content.units - ver fetch_full_details_from_sicar

    department_uuid = Column(String, index=True, nullable=True)
    category_uuid = Column(String, index=True, nullable=True)

    # PIM propio (no sincronizado desde Sicar X) - valores EAV libres por producto, clave=Attribute.slug.
    attributes = Column(JSONB, nullable=True)
    variant_group_uuid = Column(String, ForeignKey("variant_groups.uuid"), nullable=True, index=True)

    price = Column(Numeric(10, 2), nullable=False)
    # Numeric (no Float): evita error de representacion binaria en la aritmetica de stock. 3 decimales para productos por peso (is_bulk).
    # Verdad de Sicar X: solo lo escribe el upsert de sync_task.py y los dos "espejos"
    # locales en sicar_sync_worker.py (exito de ACCEPT/CANCEL). Checkout/cancelacion ya NO
    # lo tocan directamente - ver `reserved` abajo y CLAUDE.md.
    stock = Column(Numeric(12, 3), default=Decimal("0"))
    is_bulk = Column(Boolean, default=False)

    # Reserva local (nunca la maneja Sicar X): suma de cantidades de ordenes locales
    # abiertas (TO_PAY/PAID) todavia no reflejadas en el stock real de Sicar X. Igual que
    # sales_count, el upsert de sync_task.py nunca la incluye en product_values, asi que
    # el sync periodico no puede resetearla - existe justamente para que ese sync no pueda
    # revertir una reserva de checkout todavia no aceptada. Ver `available_stock` abajo.
    reserved = Column(Numeric(12, 3), nullable=False, default=Decimal("0"), server_default="0")

    # Registro local de unidades vendidas (nunca lo maneja Sicar X) - se incrementa/decrementa
    # en la transicion TO_PAY->PAID y en su reverso via apply_sales_count_deltas (ver
    # order_history_service.py). server_default requerido: el upsert de sync_task.py omite
    # esta columna a proposito (ver comentario junto a update_dict ahi) para que el sync
    # periodico no la resetee.
    sales_count = Column(Numeric(12, 3), nullable=False, default=Decimal("0"), server_default="0")

    @hybrid_property
    def available_stock(self):
        """Cantidad realmente vendible ahora mismo (stock - reserved). Clamped a 0: si el
        stock real de Sicar X baja por una razon ajena a este backend (venta en tienda)
        mientras hay unidades reservadas localmente, reserved puede superar transitoriamente
        a stock - ver sync_task.py, alerta de deriva. Unica definicion de disponibilidad
        usada en todo lo cara al cliente (catalogo, busqueda, carrito, checkout)."""
        stock = self.stock if self.stock is not None else Decimal("0")
        reserved = self.reserved if self.reserved is not None else Decimal("0")
        return max(stock - reserved, Decimal("0"))

    @available_stock.expression
    def available_stock(cls):
        return func.greatest(cls.stock - cls.reserved, 0)

    is_active = Column(Boolean, default=True)
    is_deleted = Column(Boolean, default=False)  # Para marcar productos que ya no existen en Sicar
    last_sync_id = Column(String, index=True, nullable=True) # Columna para detectar productos a eliminar
    details_updated_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class SyncStatus(Base):
    """Fila unica (id=1) con el estado de la corrida mas reciente de sync_task.py - antes
    no habia forma de consultar el ultimo sync exitoso sin leer sync.log directamente."""
    __tablename__ = "sync_status"

    id = Column(Integer, primary_key=True)
    last_run_started_at = Column(DateTime(timezone=True), nullable=True)
    last_run_finished_at = Column(DateTime(timezone=True), nullable=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    products_processed = Column(Integer, nullable=True)
    products_deactivated = Column(Integer, nullable=True)
    last_error = Column(String, nullable=True)
