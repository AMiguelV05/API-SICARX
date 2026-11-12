"""Lecturas y acciones de admin sobre la integracion con proveedores (hoy solo URREA). El sync
en si vive en el worker (urrea_sync_worker.py)."""
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product
from app.models.supplier_product import SupplierProduct, SupplierSyncStatus
from app.schemas.suppliers import ProductSupplierInfo, SupplierSyncStatusResponse
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
