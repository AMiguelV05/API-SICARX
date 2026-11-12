"""Lecturas y acciones de admin sobre la integracion con proveedores (hoy solo URREA). El sync
en si vive en el worker (urrea_sync_worker.py)."""
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product
from app.models.supplier_product import SupplierProduct, SupplierSyncStatus
from app.schemas.products import SupplierMedia
from app.schemas.suppliers import PendingPurchase, ProductSupplierInfo, SupplierSyncStatusResponse
from app.services import urrea_client
from app.services.urrea_client import BULK_JOB as JOB, SUPPLIER

async def get_urrea_status(db: AsyncSession) -> SupplierSyncStatusResponse:
    row = await db.scalar(
        select(SupplierSyncStatus).where(SupplierSyncStatus.supplier == SUPPLIER, SupplierSyncStatus.job == JOB)
    )
    linked = await db.scalar(select(func.count()).select_from(SupplierProduct).where(SupplierProduct.supplier == SUPPLIER))
    available = await db.scalar(
        select(func.count()).select_from(Product).where(Product.supplier_available == True, Product.is_deleted == False)  # noqa: E712
    )
    data = {c: getattr(row, c) for c in (
        "requested_at", "last_run_started_at", "last_run_finished_at", "last_success_at", "rows_received",
        "rows_matched", "rows_changed", "pim_fields_written", "last_error",
    )} if row else {}
    return SupplierSyncStatusResponse(
        enabled=urrea_client.is_enabled(), linked_products=linked or 0, supplier_available_products=available or 0, **data,
    )


async def request_urrea_sync(db: AsyncSession) -> datetime:
    """Marca requested_at; el worker corre el sync en su siguiente minuto. Sin commit."""
    now = datetime.now(timezone.utc)
    stmt = insert(SupplierSyncStatus).values(supplier=SUPPLIER, job=JOB, requested_at=now)
    stmt = stmt.on_conflict_do_update(constraint="uq_supplier_sync_status_job", set_={"requested_at": now})
    await db.execute(stmt)
    return now


_PENDING_PURCHASES = text("""
    SELECT item->>'uuid' AS product_uuid,
           SUM((item->>'backorderQuantity')::numeric) AS backorder_quantity,
           COUNT(DISTINCT orders.id) AS orders_count,
           MIN(orders.created_at) AS oldest_order_at
    FROM orders, jsonb_array_elements(orders.items) AS item
    WHERE orders.status = 'PAID' AND orders.accepted_at IS NULL AND orders.deleted_at IS NULL
      AND item->>'fulfillment' = 'BACKORDER'
    GROUP BY item->>'uuid'
""")


def _round_up_to_multiple(quantity: Decimal, multiple: int | None) -> Decimal:
    if not multiple or multiple <= 1:
        return quantity.to_integral_value(rounding=ROUND_CEILING)
    return (quantity / multiple).to_integral_value(rounding=ROUND_CEILING) * multiple


async def list_pending_purchases(db: AsyncSession) -> list[PendingPurchase]:
    """Lo que hay que pedirle a URREA: piezas sobre pedido de ordenes PAID que todavia no se
    aceptan (al aceptar se descuenta en Sicar X; ver el proceso en ADMIN_INTEGRATION.md).
    Las TO_PAY no cuentan: pueden no pagarse nunca. La mas antigua primero."""
    pending = (await db.execute(_PENDING_PURCHASES)).mappings().all()
    if not pending:
        return []
    uuids = [row["product_uuid"] for row in pending]
    rows = (await db.execute(
        select(Product, SupplierProduct)
        .outerjoin(SupplierProduct, (SupplierProduct.product_id == Product.id) & (SupplierProduct.supplier == SUPPLIER))
        .where(Product.sicar_uuid.in_(uuids))
    )).all()
    by_uuid = {product.sicar_uuid: (product, supplier_product) for product, supplier_product in rows}
    docs = []
    for row in pending:
        product, supplier_product = by_uuid.get(row["product_uuid"], (None, None))
        if product is None:
            continue
        multiple = supplier_product.sale_multiple if supplier_product else None
        docs.append(PendingPurchase(
            product_uuid=product.sicar_uuid, sku=product.sku, name=product.name,
            supplier_code=supplier_product.supplier_code if supplier_product else None,
            sale_multiple=multiple, list_price=supplier_product.list_price if supplier_product else None,
            backorder_quantity=row["backorder_quantity"],
            suggested_purchase_quantity=_round_up_to_multiple(row["backorder_quantity"], multiple),
            orders_count=row["orders_count"], oldest_order_at=row["oldest_order_at"],
            stock=product.stock, reserved=product.reserved,
        ))
    docs.sort(key=lambda d: d.oldest_order_at)
    return docs


async def get_supplier_media(db: AsyncSession, product_id: int) -> SupplierMedia | None:
    """Video y PDFs del proveedor para el detalle de producto (cara al cliente: nunca precios).
    None si el producto no esta ligado o el proveedor no tiene ningun medio."""
    row = (await db.execute(
        select(SupplierProduct.video_url, SupplierProduct.datasheet_url, SupplierProduct.manual_url)
        .where(SupplierProduct.product_id == product_id, SupplierProduct.supplier == SUPPLIER)
    )).first()
    if row is None or not any(row):
        return None
    return SupplierMedia(video_url=row.video_url, datasheet_url=row.datasheet_url, manual_url=row.manual_url)


async def get_product_supplier_info(db: AsyncSession, product_uuid: str) -> ProductSupplierInfo:
    row = (await db.execute(
        select(SupplierProduct, Product.price)
        .join(Product, Product.id == SupplierProduct.product_id)
        .where(Product.sicar_uuid == product_uuid, Product.is_deleted == False)  # noqa: E712
    )).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="El producto no esta ligado a ningun codigo de proveedor.")
    supplier_product, price = row
    return ProductSupplierInfo(
        supplier=supplier_product.supplier,
        supplier_code=supplier_product.supplier_code,
        match_method=supplier_product.match_method,
        barcode=supplier_product.barcode,
        list_price=supplier_product.list_price,
        currency=supplier_product.currency,
        sale_multiple=supplier_product.sale_multiple,
        price=price,
        availability_status=supplier_product.availability_status,
        photo_urls=supplier_product.photo_urls or [],
        video_url=supplier_product.video_url,
        datasheet_url=supplier_product.datasheet_url,
        manual_url=supplier_product.manual_url,
        set_contents_url=supplier_product.set_contents_url,
        info=supplier_product.info,
        pim_fields_applied=supplier_product.pim_fields_applied,
        updated_at=supplier_product.updated_at,
    )
