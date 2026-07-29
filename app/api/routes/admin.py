import logging
from typing import Optional
from fastapi import APIRouter, Depends, Body, HTTPException, Query, status
from app.core.database import DbDep
from app.core.security import validate_admin_key
from app.schemas.admin import (
    AdminHealthResponse,
    SyncStatusResponse,
    OutboxListResponse,
    OutboxRowPublic,
    AdminOrderListResponse,
    AdminOrderPublic,
    OrderAcceptRequest,
    OrderAcceptResponse,
    DeliveryAssignRequest,
    DeliveryAssignResponse,
)
from app.services import admin_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["Admin"], dependencies=[Depends(validate_admin_key)])


@router.get("/health", response_model=AdminHealthResponse, summary="Estado operativo: DB, token Sicar X, cola de sincronizacion")
async def admin_health(db: DbDep):
    return await admin_service.get_health(db)


@router.get("/sync/catalog-status", response_model=SyncStatusResponse, summary="Ultima corrida del sync de catalogo")
async def admin_catalog_sync_status(db: DbDep):
    return await admin_service.get_catalog_sync_status(db)


@router.get("/sync/outbox", response_model=OutboxListResponse, summary="Listar filas de sicar_sync_outbox")
async def admin_list_outbox(
    db: DbDep,
    status_filter: Optional[list[str]] = Query(default=None, alias="status", description="PENDING/IN_PROGRESS/SUCCEEDED/FAILED - por defecto excluye SUCCEEDED"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    total, rows = await admin_service.list_outbox(db, status_filter, limit, offset)
    return OutboxListResponse(total=total, docs=[OutboxRowPublic.model_validate(r) for r in rows])


@router.post("/sync/outbox/{outbox_id}/retry", response_model=OutboxRowPublic, summary="Reintentar una fila FAILED de sicar_sync_outbox")
async def admin_retry_outbox(outbox_id: int, db: DbDep):
    row = await admin_service.retry_outbox_row(db, outbox_id)
    return OutboxRowPublic.model_validate(row)


@router.get("/orders", response_model=AdminOrderListResponse, summary="Busqueda de ordenes (todas las cuentas)")
async def admin_list_orders(
    db: DbDep,
    status_filter: Optional[str] = Query(default=None, alias="status", description="TO_PAY/PAID/CANCELLED"),
    dispatch_status: Optional[str] = Query(default=None),
    client_email: Optional[str] = Query(default=None),
    client_uuid: Optional[str] = Query(default=None),
    include_deleted: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    total, orders = await admin_service.list_orders_admin(
        db,
        status_filter=status_filter,
        dispatch_status=dispatch_status,
        client_email=client_email,
        client_uuid=client_uuid,
        include_deleted=include_deleted,
        limit=limit,
        offset=offset,
    )
    return AdminOrderListResponse(total=total, docs=orders)


@router.get("/orders/{order_uuid}", response_model=AdminOrderPublic, summary="Detalle de una orden (cualquier cuenta)")
async def admin_get_order(order_uuid: str, db: DbDep, include_deleted: bool = Query(default=False)):
    return await admin_service.get_order_admin(db, order_uuid, include_deleted=include_deleted)


@router.post("/orders/{order_uuid}/accept", response_model=OrderAcceptResponse, summary="Aceptar una orden (local; sincronizacion con Sicar X pendiente de implementar)")
async def admin_accept_order(order_uuid: str, db: DbDep, data: OrderAcceptRequest = Body(default=OrderAcceptRequest())):
    order = await admin_service.accept_order(db, order_uuid, data.accepted_by)
    return OrderAcceptResponse(order_uuid=order.uuid, accepted_at=order.accepted_at, accepted_by=order.accepted_by)


@router.post("/orders/{order_uuid}/assign-delivery", response_model=DeliveryAssignResponse, summary="Asignar mensajeria/paqueteria a una orden (metadato local, sin integracion externa)")
async def admin_assign_delivery(order_uuid: str, db: DbDep, data: DeliveryAssignRequest = Body()):
    order = await admin_service.assign_delivery(db, order_uuid, data.delivery_company)
    return DeliveryAssignResponse(
        order_uuid=order.uuid,
        delivery_company=order.delivery_company,
        delivery_assigned_at=order.delivery_assigned_at,
    )
