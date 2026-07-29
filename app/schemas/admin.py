from typing import Any, List, Literal, Optional
from datetime import datetime
from pydantic import Field
from app.schemas.base import CamelModel

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
