from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB

from app.core.database import Base


class SupplierProduct(Base):
    """Ficha de un producto en el catalogo de un proveedor (hoy solo URREA), emparejada con un
    Product local. Generica por proveedor a proposito: el envio directo desde la bodega del
    proveedor (dropship) y otros proveedores reusan esta tabla. Escrita solo por el worker
    (urrea_sync_worker.py); los precios del proveedor nunca se exponen al cliente."""
    __tablename__ = "supplier_products"
    __table_args__ = (
        UniqueConstraint("supplier", "supplier_code", name="uq_supplier_products_code"),
        UniqueConstraint("supplier", "product_id", name="uq_supplier_products_product"),
        Index("ix_supplier_products_product_id", "product_id"),
    )

    id = Column(Integer, primary_key=True)
    supplier = Column(String(20), nullable=False)  # "URREA"
    supplier_code = Column(String(50), nullable=False)  # Codigo en la lista de precios del proveedor
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    match_method = Column(String(20), nullable=False)  # SKU_PREFIX | BARCODE
    barcode = Column(String(50), nullable=True)

    # Precio de lista del proveedor (MXP), SIN el descuento de distribuidor que nos aplican: no
    # es nuestro costo, por eso no se calcula margen. Solo admin.
    list_price = Column(Numeric(12, 4), nullable=True)
    currency = Column(String(3), nullable=True)
    sale_multiple = Column(Integer, nullable=True)  # Multiplo en que el proveedor nos vende

    # Estatus de la disponibilidad masiva: AVAILABLE | LIMITED | UNAVAILABLE. Es solo una pista
    # (el probe mostro que no siempre coincide con la consulta por cantidad, opcion 2), por eso
    # no lleva fecha por fila: escribirla en cada pasada reescribiria ~15k filas cada vez.
    availability_status = Column(String(20), nullable=True)

    photo_urls = Column(JSONB, nullable=True)  # Lista de URLs, la primera es la principal
    video_url = Column(String, nullable=True)
    datasheet_url = Column(String, nullable=True)
    manual_url = Column(String, nullable=True)
    set_contents_url = Column(String, nullable=True)

    # Resto de la ficha, normalizada (nombre largo, marca, familia/clase, bullets, garantia...).
    info = Column(JSONB, nullable=True)
    # Campos PIM de products ya aplicados desde esta ficha. NULL = la ficha nunca se ha
    # aplicado: la primera aplicacion REEMPLAZA los valores locales (venian de la misma fuente,
    # sin editar); despues solo se llenan campos vacios que no esten en esta lista. Ver
    # urrea_pim_service.py.
    pim_fields_applied = Column(JSONB, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class SupplierSyncStatus(Base):
    """Estado del sync con un proveedor, una fila por (supplier, job). `requested_at` lo pone
    el admin (POST /admin/suppliers/urrea/sync) y el worker lo atiende en su siguiente minuto -
    la API nunca corre el sync."""
    __tablename__ = "supplier_sync_status"
    __table_args__ = (UniqueConstraint("supplier", "job", name="uq_supplier_sync_status_job"),)

    id = Column(Integer, primary_key=True)
    supplier = Column(String(20), nullable=False)
    job = Column(String(40), nullable=False)
    requested_at = Column(DateTime(timezone=True), nullable=True)
    last_run_started_at = Column(DateTime(timezone=True), nullable=True)
    last_run_finished_at = Column(DateTime(timezone=True), nullable=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    rows_received = Column(Integer, nullable=True)
    rows_matched = Column(Integer, nullable=True)
    rows_changed = Column(Integer, nullable=True)
    pim_fields_written = Column(Integer, nullable=True)
    last_error = Column(Text, nullable=True)
