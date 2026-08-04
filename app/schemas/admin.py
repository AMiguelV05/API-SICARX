from typing import Any, List, Literal, Optional
from datetime import datetime
from pydantic import Field
from app.schemas.base import CamelModel
from app.schemas.client import ClientAddressPublic

class AdminHealthResponse(CamelModel):
    database_ok: bool = Field(description="True si SELECT 1 respondio contra Postgres")
    sicar_token_present: bool = Field(description="True si el proceso sicar_auth de este worker/api actualmente sostiene un token en memoria")
    outbox_counts: dict[str, int] = Field(description="Conteo de sicar_sync_outbox agrupado por status (PENDING/IN_PROGRESS/SUCCEEDED/FAILED)")

class SyncStatusResponse(CamelModel):
    last_run_started_at: Optional[datetime] = None
    last_run_finished_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    products_processed: Optional[int] = None
    products_deactivated: Optional[int] = None
    last_error: Optional[str] = None

class OutboxRowPublic(CamelModel):
    id: int
    order_id: int
    action: str
    status: str
    attempts: int
    last_error: Optional[str] = None
    next_attempt_at: datetime
    created_at: datetime
    updated_at: Optional[datetime] = None

class OutboxListResponse(CamelModel):
    total: int
    docs: List[OutboxRowPublic]

# Definida antes de AdminOrderPublic para que su campo shipping_label pueda referenciarla directamente.

class ShippingLabelInfo(CamelModel):
    carrier: str
    service: str
    shipment_id: Optional[int] = Field(default=None, description="ID del envio en envia.com - buscar por este valor en su dashboard")
    service_description: Optional[str] = None
    tracking_number: Optional[str] = None
    track_url: Optional[str] = None
    label_url: Optional[str] = None
    total_price: float
    currency: str
    weight: float
    length: float
    width: float
    height: float
    generated_at: datetime

class AdminOrderPublic(CamelModel):
    """Superconjunto de OrderPublic para uso admin: agrega clientEmail/clientName, deletedAt (admins ven soft-deleted) y campos de aceptacion/mensajeria."""
    uuid: str
    sicar_order_id: str
    serie_folio: Optional[str]
    status: str
    dispatch_status: Optional[str] = None
    total: float
    total_quantity: float
    delivery_info: dict[str, Any]
    items: list[dict[str, Any]]
    created_at: datetime
    deleted_at: Optional[datetime] = None
    client_email: Optional[str] = None
    client_name: Optional[str] = None
    accepted_at: Optional[datetime] = None
    accepted_by: Optional[str] = None
    delivery_company: Optional[str] = None
    delivery_assigned_at: Optional[datetime] = None
    # Foto fija tomada al crear la orden (None para PICKUP) - ver Order.delivery_address_snapshot.
    delivery_address: Optional[ClientAddressPublic] = None
    # None hasta que POST .../shipping/generate tenga exito una vez - ver Order.shipping_label.
    shipping_label: Optional[ShippingLabelInfo] = None

class AdminOrderListResponse(CamelModel):
    total: int
    docs: List[AdminOrderPublic]

class OrderAcceptRequest(CamelModel):
    accepted_by: Optional[str] = Field(default=None, description="Identificador/nombre de quien acepta - texto libre, no hay modelo de usuarios admin todavia")

class OrderAcceptResponse(CamelModel):
    order_uuid: str
    accepted_at: datetime
    accepted_by: Optional[str] = None
    dispatch_status: str
    sync_status: Literal["QUEUED"] = "QUEUED"
    note: str = "La aceptación ya se aplicó localmente (dispatchStatus = PENDING); se le avisa a Sicar X de forma asíncrona via sicar_sync_outbox."

class AdvanceDispatchStatusRequest(CamelModel):
    dispatch_status: Literal["PREPARING", "COMPLETE", "DISPATCHED"] = Field(
        description="Estado objetivo. Debe ser el siguiente paso legal (o el anterior, para revertir "
        "un error) desde el dispatchStatus actual de la orden - ver ADMIN_INTEGRATION.md."
    )

class AdvanceDispatchStatusResponse(CamelModel):
    order_uuid: str
    dispatch_status: str
    sync_status: Literal["QUEUED"] = "QUEUED"
    note: str = "El nuevo estado ya se aplicó localmente; se le avisa a Sicar X de forma asíncrona via sicar_sync_outbox."

class DeliveryAssignRequest(CamelModel):
    delivery_company: str = Field(min_length=1, description="Nombre de la paqueteria/repartidor asignado - texto libre, no valida contra ningun catalogo")

class DeliveryAssignResponse(CamelModel):
    order_uuid: str
    delivery_company: str
    delivery_assigned_at: datetime

class ShippingOriginOverride(CamelModel):
    """Override opcional campo por campo del origen (ENVIA_ORIGIN_*); lo omitido cae al valor de .env. Sin country/phone_code: el negocio solo envia dentro de Mexico."""
    name: Optional[str] = None
    company: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    street: Optional[str] = None
    number: Optional[str] = None
    district: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    reference: Optional[str] = None

class ShippingDimensionsRequest(CamelModel):
    """Dimensiones/peso del paquete, compartido por /shipping/quote y /shipping/generate; validado > 0 via Pydantic (422, no 400)."""
    weight: float = Field(gt=0, description="Kilogramos")
    length: float = Field(gt=0, description="Centimetros")
    width: float = Field(gt=0, description="Centimetros")
    height: float = Field(gt=0, description="Centimetros")
    origin: Optional[ShippingOriginOverride] = Field(default=None, description="Override opcional del origen - ver ShippingOriginOverride")

class ShippingQuoteRequest(ShippingDimensionsRequest):
    pass

class ShippingQuoteOption(CamelModel):
    carrier: Optional[str] = None
    service: str
    service_description: Optional[str] = None
    delivery_estimate: Optional[str] = None
    total_price: float
    currency: str

class ShippingQuoteResponse(CamelModel):
    options: List[ShippingQuoteOption]

class ShippingGenerateRequest(ShippingDimensionsRequest):
    carrier: str = Field(min_length=1)
    service: str = Field(min_length=1)

class ShippingGenerateResponse(CamelModel):
    order_uuid: str
    dispatch_status: str
    shipping_label: ShippingLabelInfo
