from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import Field

from app.schemas.base import CamelModel


class SupplierSyncStatusResponse(CamelModel):
    enabled: bool = Field(description="True si URREA_USER/URREA_PASSWORD estan configurados en este proceso (api)")
    requested_at: Optional[datetime] = Field(None, description="Ultima vez que un admin pidio un sync manual")
    last_run_started_at: Optional[datetime] = None
    last_run_finished_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    rows_received: Optional[int] = Field(None, description="Codigos en la ultima masiva")
    rows_matched: Optional[int] = Field(None, description="Codigos emparejados con un producto local")
    rows_changed: Optional[int] = Field(None, description="Filas de supplier_products escritas en la ultima pasada")
    pim_fields_written: Optional[int] = Field(None, description="Campos de PIM escritos en la ultima pasada")
    last_error: Optional[str] = None
    linked_products: int = Field(description="Productos locales ligados a un codigo de URREA")
    supplier_available_products: int = Field(description="Productos que la masiva marca como disponibles en URREA")


class SupplierSyncRequestResponse(CamelModel):
    requested_at: datetime
    detail: str


class ProductSupplierInfo(CamelModel):
    """Solo admin: incluye el precio de lista del proveedor."""
    supplier: str
    supplier_code: str
    match_method: str
    barcode: Optional[str] = None
    list_price: Optional[Decimal] = Field(None, description="Precio de lista de URREA (MXP), sin nuestro descuento de distribuidor; no es el costo")
    currency: Optional[str] = None
    sale_multiple: Optional[int] = Field(None, description="Multiplo en que URREA nos vende")
    price: Optional[Decimal] = Field(None, description="Nuestro precio de venta (Sicar X, IVA incluido)")
    availability_status: Optional[str] = Field(None, description="AVAILABLE | LIMITED | UNAVAILABLE segun la masiva; null si el codigo ya no viene")
    photo_urls: list[str] = []
    video_url: Optional[str] = None
    datasheet_url: Optional[str] = None
    manual_url: Optional[str] = None
    set_contents_url: Optional[str] = None
    info: Optional[dict] = None
    pim_fields_applied: Optional[list[str]] = None
    updated_at: datetime
