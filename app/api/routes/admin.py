import logging
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, Depends, Body, HTTPException, Query, status
from app.core.database import DbDep
from app.core.security import get_current_admin, CurrentAdminDep, SuperAdminDep
from app.schemas.admin import (
    AdminHealthResponse,
    SyncStatusResponse,
    OutboxListResponse,
    OutboxRowPublic,
    AdminOrderListResponse,
    AdminOrderPublic,
    OrderAcceptRequest,
    OrderAcceptResponse,
    AdminOrderCancelRequest,
    AdminOrderCancelResponse,
    AdvanceDispatchStatusRequest,
    AdvanceDispatchStatusResponse,
    DeliveryAssignRequest,
    DeliveryAssignResponse,
    ShippingQuoteRequest,
    ShippingQuoteResponse,
    ShippingGenerateRequest,
    ShippingGenerateResponse,
    ShippingCancelRequest,
    ShippingCancelResponse,
    ShippingCancelRefund,
)
from app.schemas.refund import RefundCreateRequest, RefundPublic, RefundListResponse
from app.services import admin_service, audit_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["Admin"], dependencies=[Depends(get_current_admin)])


@router.get("/health", response_model=AdminHealthResponse, summary="Estado operativo: DB, token Sicar X, cola de sincronizacion")
async def admin_health(db: DbDep):
    """Chequeo operativo rapido: conectividad a la DB (`SELECT 1`), si el `sicar_auth`
    singleton de este proceso tiene un token vigente, y conteos de `sicar_sync_outbox`
    agrupados por status."""
    return await admin_service.get_health(db)


@router.get("/sync/catalog-status", response_model=SyncStatusResponse, summary="Ultima corrida del sync de catalogo")
async def admin_catalog_sync_status(db: DbDep):
    """Datos de la ultima corrida del sync de catalogo (`SyncStatus`, fila unica escrita
    por `sync_task.py` al inicio/fin de cada pasada): timestamps, productos procesados/
    desactivados y el ultimo error, si hubo uno."""
    return await admin_service.get_catalog_sync_status(db)


@router.get("/sync/outbox", response_model=OutboxListResponse, summary="Listar filas de sicar_sync_outbox")
async def admin_list_outbox(
    db: DbDep,
    status_filter: Optional[list[str]] = Query(default=None, alias="status", description="PENDING/IN_PROGRESS/SUCCEEDED/FAILED - por defecto excluye SUCCEEDED"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Lista filas de `sicar_sync_outbox` (los pasos hacia Sicar X pendientes/en curso/
    fallidos de sincronizar de forma asincrona), ordenadas por `next_attempt_at`. Por
    defecto excluye `SUCCEEDED` para enfocarse en lo que todavia requiere atencion."""
    total, rows = await admin_service.list_outbox(db, status_filter, limit, offset)
    return OutboxListResponse(total=total, docs=[OutboxRowPublic.model_validate(r) for r in rows])


@router.post("/sync/outbox/{outbox_id}/retry", response_model=OutboxRowPublic, summary="Reintentar una fila FAILED de sicar_sync_outbox")
async def admin_retry_outbox(outbox_id: int, db: DbDep):
    """Reintenta manualmente una fila `FAILED` (agoto sus `MAX_ATTEMPTS`): la vuelve a
    `PENDING` con `next_attempt_at = now()` para que `sicar_sync_worker.py` la recoja en
    su siguiente ciclo. `404` si la fila no esta en `FAILED`."""
    row = await admin_service.retry_outbox_row(db, outbox_id)
    return OutboxRowPublic.model_validate(row)


@router.get("/orders", response_model=AdminOrderListResponse, summary="Busqueda de ordenes (todas las cuentas)")
async def admin_list_orders(
    db: DbDep,
    status_filter: Optional[str] = Query(default=None, alias="status", description="TO_PAY/PAID/CANCELLED"),
    dispatch_status: Optional[str] = Query(default=None, alias="dispatchStatus"),
    client_email: Optional[str] = Query(default=None, alias="clientEmail"),
    client_uuid: Optional[str] = Query(default=None, alias="clientUuid"),
    include_deleted: bool = Query(default=False, alias="includeDeleted"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Busqueda de ordenes de TODAS las cuentas (a diferencia de `GET /auth/me/orders`,
    que solo ve las del cliente autenticado) - filtrable por status/dispatchStatus/
    correo/uuid de cliente y rango de fechas, para uso operativo interno."""
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
async def admin_get_order(order_uuid: str, db: DbDep, include_deleted: bool = Query(default=False, alias="includeDeleted")):
    """Detalle de cualquier orden por `uuid` (no `sicar_order_id`, a diferencia de las
    rutas de cliente). `includeDeleted=true` permite ver ordenes soft-deleted, util para
    reconciliar la cola de sincronizacion con Sicar X."""
    return await admin_service.get_order_admin(db, order_uuid, include_deleted=include_deleted)


@router.post("/orders/{order_uuid}/accept", response_model=OrderAcceptResponse, summary="Aceptar una orden localmente y encolar el avance de dispatchStatus en Sicar X")
async def admin_accept_order(order_uuid: str, db: DbDep, current: CurrentAdminDep, data: OrderAcceptRequest = Body(default=OrderAcceptRequest())):
    """Marca la orden como aceptada localmente (`accepted_at`/`accepted_by`) y encola un
    `SicarSyncOutbox` (`action="ACCEPT"`) para que el worker avance su `dispatchStatus` a
    `PENDING` en Sicar X de forma asincrona - Sicar X no sabe que la orden existe hasta
    este punto."""
    order = await admin_service.accept_order(db, order_uuid, data.accepted_by)
    await audit_service.log_action(db, current, "order.accept", "order", order.uuid)
    await db.commit()
    return OrderAcceptResponse(order_uuid=order.uuid, accepted_at=order.accepted_at, accepted_by=order.accepted_by, dispatch_status=order.dispatch_status)


@router.post("/orders/{order_uuid}/cancel", response_model=AdminOrderCancelResponse, summary="Cancelar una orden como administrador")
async def admin_cancel_order(order_uuid: str, db: DbDep, current: CurrentAdminDep, background_tasks: BackgroundTasks, data: AdminOrderCancelRequest = Body()):
    """Cancela localmente cualquier orden (`status = "CANCELLED"`, stock/reserva
    restaurados), con un `reason` obligatorio que queda guardado en la orden y viaja en el
    webhook `order-cancelled` - visible para el cliente, a diferencia de una cancelación
    hecha por el propio cliente (`reason` queda `null` en ese caso). Mismo gating que
    `POST /orders/{id}/cancel`: `409` si ya está `CANCELLED` o si `dispatchStatus` ya es
    `DISPATCHED` (COMPLETE sigue siendo cancelable - también es terminal para pedidos
    PICKUP). Si la orden ya había sido aceptada, se le avisa a Sicar X de forma asíncrona
    vía `sicar_sync_outbox`; si nunca fue aceptada, no hay nada que sincronizar."""
    order, was_accepted = await admin_service.cancel_order_admin(db, order_uuid, data.reason, background_tasks)
    await audit_service.log_action(db, current, "order.cancel", "order", order.uuid, {"reason": data.reason})
    await db.commit()
    return AdminOrderCancelResponse(
        order_uuid=order.uuid,
        cancelled_at=datetime.now(timezone.utc),
        reason=order.cancellation_reason,
        sync_status="QUEUED" if was_accepted else "NOT_NEEDED",
        note=(
            "La cancelación ya se aplicó localmente; se le avisa a Sicar X de forma asíncrona vía sicar_sync_outbox."
            if was_accepted
            else "La cancelación ya se aplicó localmente. Sicar X nunca fue notificado de esta orden, así que no hay nada que sincronizar."
        ),
    )


@router.post("/orders/{order_uuid}/refund", response_model=RefundPublic, summary="Emitir un reembolso parcial o total sobre una orden pagada")
async def admin_refund_order(order_uuid: str, db: DbDep, current: SuperAdminDep, data: RefundCreateRequest = Body()):
    """Solo super_admin. `409` si la orden no esta `PAID` o no tiene `mp_payment_id`.
    `400` si `amount` excede `order.total` menos lo ya reembolsado. No toca
    `Product.stock`/`reserved` (evento solo monetario) ni `Order.status` (se queda
    `PAID`) - ver CLAUDE.md, "Reembolsos parciales"."""
    refund = await admin_service.refund_order(db, order_uuid, data.amount, data.reason, current)
    await audit_service.log_action(db, current, "order.refund", "order", order_uuid, {"amount": float(refund.amount), "reason": data.reason})
    await db.commit()
    return RefundPublic.model_validate(refund)


@router.get("/orders/{order_uuid}/refunds", response_model=RefundListResponse, summary="Listar los reembolsos emitidos sobre una orden")
async def admin_list_order_refunds(
    order_uuid: str,
    db: DbDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Cualquier admin autenticado puede ver (solo super_admin puede emitir, ver arriba).
    Incluye tanto reembolsos parciales explicitos como el reembolso automatico de una
    cancelacion completa (`reason="Cancelación de orden"`/`"Cancelación de orden (admin)"`)."""
    total, refunds = await admin_service.list_order_refunds(db, order_uuid, limit, offset)
    return RefundListResponse(total=total, docs=[RefundPublic.model_validate(r) for r in refunds])


@router.post("/orders/{order_uuid}/advance-status", response_model=AdvanceDispatchStatusResponse, summary="Avanzar (o revertir un paso) el dispatchStatus de una orden")
async def admin_advance_order_status(order_uuid: str, db: DbDep, data: AdvanceDispatchStatusRequest = Body()):
    """Avanza (o revierte, para correcciones manuales) el `dispatchStatus` de la orden en
    Sicar X directamente via `PUT /external/v1/dispatch` (llamada sincrona, admin token) -
    vocabulario valido: PENDING_ACCEPTANCE/PENDING/PREPARING/COMPLETE/DISPATCHED."""
    order = await admin_service.advance_order_dispatch_status(db, order_uuid, data.dispatch_status)
    return AdvanceDispatchStatusResponse(order_uuid=order.uuid, dispatch_status=order.dispatch_status)


@router.post("/orders/{order_uuid}/assign-delivery", response_model=DeliveryAssignResponse, summary="Asignar mensajeria/paqueteria a una orden (metadato local, sin integracion externa)")
async def admin_assign_delivery(order_uuid: str, db: DbDep, data: DeliveryAssignRequest = Body()):
    """Guarda `delivery_company` (texto libre) + `delivery_assigned_at` como metadato
    local unicamente - no llama a ninguna API de paqueteria externa. Distinto de la
    integracion real con envia.com (`/shipping/quote`/`/shipping/generate` abajo)."""
    order = await admin_service.assign_delivery(db, order_uuid, data.delivery_company)
    return DeliveryAssignResponse(
        order_uuid=order.uuid,
        delivery_company=order.delivery_company,
        delivery_assigned_at=order.delivery_assigned_at,
    )


@router.post("/orders/{order_uuid}/shipping/quote", response_model=ShippingQuoteResponse, summary="Cotizar opciones de envio con envia.com (no persiste nada)")
async def admin_quote_shipping(order_uuid: str, db: DbDep, data: ShippingQuoteRequest = Body()):
    """Cotiza opciones de envio reales con envia.com para una orden DELIVERYMAN con
    `dispatchStatus: COMPLETE` (`409` si no lo esta). No persiste nada ni cuesta dinero -
    solo consulta. `origin` es opcional; los campos omitidos caen a `.env`."""
    options = await admin_service.quote_shipping(db, order_uuid, data)
    return ShippingQuoteResponse(options=options)


@router.post("/orders/{order_uuid}/shipping/generate", response_model=ShippingGenerateResponse, summary="Generar una guia de envio real con envia.com y avanzar dispatchStatus a DISPATCHED")
async def admin_generate_shipping_label(order_uuid: str, db: DbDep, data: ShippingGenerateRequest = Body()):
    """Genera una guia real (con costo) via envia.com, persiste `Order.shipping_label` y
    avanza `dispatchStatus` a `DISPATCHED` de inmediato. `409` si ya tiene guia o si la
    orden no esta lista para envio. Sin reintento automatico si envia.com hace timeout."""
    order = await admin_service.generate_shipping_label(db, order_uuid, data)
    return ShippingGenerateResponse(
        order_uuid=order.uuid,
        dispatch_status=order.dispatch_status,
        shipping_label=order.shipping_label,
    )


@router.post("/orders/{order_uuid}/shipping/cancel", response_model=ShippingCancelResponse, summary="Cancelar ante envia.com una guia de envio ya generada")
async def admin_cancel_shipping_label(order_uuid: str, db: DbDep, data: ShippingCancelRequest = Body()):
    """Cancela la guia real ante envia.com (identificada solo por `carrier`/`trackingNumber`,
    ya persistidos en `shippingLabel`) y, si tiene exito, limpia `shippingLabel` y revierte
    `dispatchStatus` de `DISPATCHED` a `COMPLETE` en la misma transaccion - la unica forma de
    reabrir una orden a `/shipping/quote`/`/shipping/generate` despues de un error. `409` si
    la orden no tiene guia. `502` si envia.com rechaza la cancelacion (p. ej. guia ya recogida
    por el carrier o fuera de la ventana de cancelacion) - incluye el mensaje real de envia.com."""
    order, refund = await admin_service.cancel_shipping_label(db, order_uuid, data.reason)
    return ShippingCancelResponse(
        order_uuid=order.uuid,
        dispatch_status=order.dispatch_status,
        shipping_label=None,
        refund=ShippingCancelRefund(**refund),
    )
