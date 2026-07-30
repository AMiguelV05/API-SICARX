from typing import Any, List, Literal, Optional
from datetime import datetime
from pydantic import Field
from app.schemas.base import CamelModel
from app.schemas.client import ClientAddressPublic

# GET /admin/health

class AdminHealthResponse(CamelModel):
    database_ok: bool = Field(description="True si SELECT 1 respondio contra Postgres")
    sicar_token_present: bool = Field(description="True si el proceso sicar_auth de este worker/api actualmente sostiene un token en memoria")
    outbox_counts: dict[str, int] = Field(description="Conteo de sicar_sync_outbox agrupado por status (PENDING/IN_PROGRESS/SUCCEEDED/FAILED)")

# GET /admin/sync/catalog-status

class SyncStatusResponse(CamelModel):
    last_run_started_at: Optional[datetime] = None
    last_run_finished_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    products_processed: Optional[int] = None
    products_deactivated: Optional[int] = None
    last_error: Optional[str] = None

# GET /admin/sync/outbox

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

# Guia de envio con envia.com (POST /admin/orders/{uuid}/shipping/quote|generate) - ver
# ADMIN_INTEGRATION.md. Definida antes de AdminOrderPublic para que su campo
# `shipping_label` pueda referenciarla directamente.

class ShippingLabelInfo(CamelModel):
    carrier: str
    service: str
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

# GET /admin/orders, GET /admin/orders/{uuid}

class AdminOrderPublic(CamelModel):
    """Superconjunto de OrderPublic (schemas/orders.py) para uso admin - misma forma base
    (uuid/sicar_order_id/status/dispatch_status/.../items/created_at) mas campos que el
    cliente no necesita ver de si mismo: clientEmail/clientName resueltos igual que
    order_notification_service.notify_order_confirmed, deletedAt (los admins SI necesitan
    ver ordenes soft-deleted), y los nuevos campos de aceptacion/mensajeria de este mismo
    cambio (accepted_at/accepted_by/delivery_company/delivery_assigned_at)."""
    uuid: str
    sicar_order_id: str
    serie_folio: Optional[str]
    status: str
    dispatch_status: Optional[str] = None
    dispatch_history: Optional[list[dict[str, Any]]] = None
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

# POST /admin/orders/{uuid}/accept

class OrderAcceptRequest(CamelModel):
    accepted_by: Optional[str] = Field(default=None, description="Identificador/nombre de quien acepta - texto libre, no hay modelo de usuarios admin todavia")

class OrderAcceptResponse(CamelModel):
    order_uuid: str
    accepted_at: datetime
    accepted_by: Optional[str] = None
    sync_status: Literal["QUEUED"] = "QUEUED"
    note: str = "La aceptacion local ya se aplico; el avance de dispatchStatus en Sicar X se procesa de forma asincrona via sicar_sync_outbox (normalmente en menos de un minuto)."

# POST /admin/orders/{uuid}/assign-delivery

class DeliveryAssignRequest(CamelModel):
    delivery_company: str = Field(min_length=1, description="Nombre de la paqueteria/repartidor asignado - texto libre, no valida contra ningun catalogo")

class DeliveryAssignResponse(CamelModel):
    order_uuid: str
    delivery_company: str
    delivery_assigned_at: datetime

# POST /admin/orders/{uuid}/shipping/quote

class ShippingDimensionsRequest(CamelModel):
    """Dimensiones/peso del paquete - compartido por /shipping/quote y /shipping/generate.
    Todos > 0 via Field(gt=0): un valor faltante o <= 0 responde 422 (Pydantic), no el
    400 hecho a mano que ADMIN_INTEGRATION.md menciona como sugerencia original."""
    weight: float = Field(gt=0, description="Kilogramos")
    length: float = Field(gt=0, description="Centimetros")
    width: float = Field(gt=0, description="Centimetros")
    height: float = Field(gt=0, description="Centimetros")

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

# POST /admin/orders/{uuid}/shipping/generate

class ShippingGenerateRequest(ShippingDimensionsRequest):
    carrier: str = Field(min_length=1)
    service: str = Field(min_length=1)

class ShippingGenerateResponse(CamelModel):
    order_uuid: str
    dispatch_status: str
    shipping_label: ShippingLabelInfo
