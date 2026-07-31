import logging
from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.models.order import Order, SicarSyncOutbox
from app.models.product import SyncStatus
from app.schemas.admin import (
    AdminHealthResponse,
    SyncStatusResponse,
    AdminOrderPublic,
    ShippingQuoteRequest,
    ShippingQuoteOption,
    ShippingGenerateRequest,
)
from app.schemas.client import ClientAddressPublic
from app.services.sicar_auth import sicar_auth
from app.services import shipping_service
from app.services import order_status_notification_service

logger = logging.getLogger(__name__)

OUTBOX_STATUSES = ("PENDING", "IN_PROGRESS", "SUCCEEDED", "FAILED")

# Transiciones legales para POST /admin/orders/{uuid}/advance-status - dispatch_status
# actual -> conjunto de targets permitidos (adelante y un paso de reversion, ver
# ADMIN_INTEGRATION.md). PENDING_ACCEPTANCE deliberadamente ausente: salir de ahi es
# trabajo exclusivo de /accept (Sicar X nunca acepta PENDING_ACCEPTANCE como target de
# PUT /external/v1/dispatch, asi que no hay nada que revertir hacia ese estado).
_LEGAL_DISPATCH_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"PREPARING"},
    "PREPARING": {"PENDING", "COMPLETE"},
    "COMPLETE": {"PREPARING", "DISPATCHED"},
    "DISPATCHED": {"COMPLETE"},
}


async def get_health(db: AsyncSession) -> AdminHealthResponse:
    try:
        await db.execute(text("SELECT 1"))
        database_ok = True
    except Exception as e:
        logger.error(f"GET /admin/health: fallo el ping a la base de datos: {e}")
        database_ok = False

    token = await sicar_auth.get_token()

    counts_result = await db.execute(
        select(SicarSyncOutbox.status, func.count()).group_by(SicarSyncOutbox.status)
    )
    counts = {s: 0 for s in OUTBOX_STATUSES}
    counts.update({row[0]: row[1] for row in counts_result.all()})

    return AdminHealthResponse(
        database_ok=database_ok,
        sicar_token_present=bool(token),
        outbox_counts=counts,
    )


async def get_catalog_sync_status(db: AsyncSession) -> SyncStatusResponse:
    """Lee la fila unica (id=1) escrita por sync_task.py al iniciar/terminar cada pasada
    de sincronizacion. Si el worker nunca ha corrido en este ambiente (base de datos
    recien migrada), la fila todavia no existe - se responde con todos los campos en
    None en vez de un 404, ya que "el worker nunca ha corrido" es un estado valido a
    reportar, no un error de la ruta."""
    row = await db.get(SyncStatus, 1)
    if row is None:
        return SyncStatusResponse()
    return SyncStatusResponse.model_validate(row)


async def list_outbox(db: AsyncSession, status_filter: list[str] | None, limit: int, offset: int) -> tuple[int, list[SicarSyncOutbox]]:
    statuses = status_filter or ["PENDING", "IN_PROGRESS", "FAILED"]
    total = await db.scalar(
        select(func.count()).select_from(SicarSyncOutbox).where(SicarSyncOutbox.status.in_(statuses))
    )
    result = await db.execute(
        select(SicarSyncOutbox)
        .where(SicarSyncOutbox.status.in_(statuses))
        .order_by(SicarSyncOutbox.next_attempt_at)
        .limit(limit)
        .offset(offset)
    )
    return total or 0, list(result.scalars().all())


async def retry_outbox_row(db: AsyncSession, row_id: int) -> SicarSyncOutbox:
    row = await db.get(SicarSyncOutbox, row_id)
    if row is None or row.status != "FAILED":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No se encontro una fila FAILED con ese id.")
    row.status = "PENDING"
    row.next_attempt_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    logger.info(f"sicar_sync_outbox {row.id} (orden {row.order_id}) reiniciada manualmente a PENDING via /admin.")
    return row


async def _to_admin_order_public(order: Order) -> AdminOrderPublic:
    client_account = await order.awaitable_attrs.client_account
    contact_email = ((order.delivery_info or {}).get("contactInfo") or {}).get("email")
    client_email = contact_email or (client_account.email if client_account else None)

    public = AdminOrderPublic.model_validate(order)
    public.client_email = client_email
    public.client_name = client_account.name if client_account else None
    # order.delivery_address_snapshot no coincide de nombre con AdminOrderPublic.delivery_address
    # (ver Order.delivery_address_snapshot), asi que model_validate(order) no lo llena solo -
    # se mapea aqui a mano, igual que client_email/client_name arriba.
    if order.delivery_address_snapshot:
        public.delivery_address = ClientAddressPublic.model_validate(order.delivery_address_snapshot)
    return public


async def list_orders_admin(
    db: AsyncSession,
    *,
    status_filter: str | None,
    dispatch_status: str | None,
    client_email: str | None,
    client_uuid: str | None,
    include_deleted: bool,
    limit: int,
    offset: int,
) -> tuple[int, list[AdminOrderPublic]]:
    """A diferencia de todo lookup existente en order_history_service.py, este NO esta
    acotado a un client_account_id especifico - es busqueda admin sobre todas las
    cuentas. client_email hace join contra ClientAccount solo cuando se pide (evita el
    join en la consulta comun sin ese filtro)."""
    from app.models.client import ClientAccount  # import perezoso: evita ciclo import-time con order.py

    # selectinload evita el N+1: sin esto, _to_admin_order_public dispara una consulta
    # extra por fila (hasta 200 por pagina) al acceder a order.awaitable_attrs.client_account.
    query = select(Order).options(selectinload(Order.client_account))
    count_query = select(func.count()).select_from(Order)

    if not include_deleted:
        query = query.where(Order.deleted_at.is_(None))
        count_query = count_query.where(Order.deleted_at.is_(None))
    if status_filter:
        query = query.where(Order.status == status_filter)
        count_query = count_query.where(Order.status == status_filter)
    if dispatch_status:
        query = query.where(Order.dispatch_status == dispatch_status)
        count_query = count_query.where(Order.dispatch_status == dispatch_status)
    if client_uuid:
        query = query.join(ClientAccount, Order.client_account_id == ClientAccount.id).where(ClientAccount.uuid == client_uuid)
        count_query = count_query.join(ClientAccount, Order.client_account_id == ClientAccount.id).where(ClientAccount.uuid == client_uuid)
    if client_email:
        query = query.join(ClientAccount, Order.client_account_id == ClientAccount.id).where(ClientAccount.email == client_email.lower())
        count_query = count_query.join(ClientAccount, Order.client_account_id == ClientAccount.id).where(ClientAccount.email == client_email.lower())

    total = await db.scalar(count_query)
    result = await db.execute(query.order_by(Order.created_at.desc()).limit(limit).offset(offset))
    orders = list(result.scalars().all())

    return total or 0, [await _to_admin_order_public(o) for o in orders]


async def get_order_admin(db: AsyncSession, order_uuid: str, *, include_deleted: bool) -> AdminOrderPublic:
    query = select(Order).where(Order.uuid == order_uuid)
    if not include_deleted:
        query = query.where(Order.deleted_at.is_(None))
    order = await db.scalar(query)
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Orden no encontrada.")
    return await _to_admin_order_public(order)


async def accept_order(db: AsyncSession, order_uuid: str, accepted_by: str | None) -> Order:
    """Marca la orden como aceptada y avanza dispatch_status a PENDING localmente de
    inmediato, y encola una fila de sicar_sync_outbox con action="ACCEPT" para que el
    worker aplique el UNICO aviso que este backend le hace a Sicar X en todo el ciclo de
    vida de una orden: el descuento de inventario (ver CLAUDE.md, "SICAR es solo ERP de
    inventario"; sicar_stock_service.apply_order_stock_delta). Mismo principio "Postgres
    local autoritativo de inmediato" que ya usa generate_shipping_label para DISPATCHED:
    el dashboard admin es quien decide el estado, Sicar X solo se entera despues.

    Requiere `order.status == "PAID"` - aceptar (y por lo tanto descontar inventario real
    en Sicar X) una orden que todavia no se ha cobrado de verdad no tiene sentido, y antes
    no habia ningun chequeo de status aqui."""
    order = await db.scalar(select(Order).where(Order.uuid == order_uuid, Order.deleted_at.is_(None)))
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Orden no encontrada.")
    if order.accepted_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Esta orden ya fue aceptada.")
    if order.status != "PAID":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Solo se pueden aceptar ordenes ya pagadas.")

    order.accepted_at = datetime.now(timezone.utc)
    order.accepted_by = accepted_by
    order.dispatch_status = "PENDING"

    db.add(SicarSyncOutbox(
        order_id=order.id,
        action="ACCEPT",
        cash_register_uuid="",  # no aplica a esta accion, pero la columna es NOT NULL
        status="PENDING",
        next_attempt_at=datetime.now(timezone.utc),
    ))

    await db.commit()
    await db.refresh(order)
    logger.info(f"Orden {order.uuid} aceptada por '{accepted_by}' via /admin (dispatchStatus=PENDING de inmediato); encolado el descuento de inventario en Sicar X.")

    try:
        await order_status_notification_service.notify_order_accepted(order)
    except Exception as e:
        logger.error(f"Fallo inesperado notificando 'orden aceptada' para la orden {order.uuid}: {type(e).__name__}: {e!r}")

    return order


async def advance_order_dispatch_status(db: AsyncSession, order_uuid: str, target_status: str) -> Order:
    """Avanza (o revierte un paso) dispatch_status localmente de inmediato - puramente
    local, ya no le avisa nada a Sicar X (ver CLAUDE.md, "SICAR es solo ERP de
    inventario": el unico aviso a Sicar X en todo el ciclo de vida de una orden es el
    descuento/reversión de inventario en accept_order/prepare_local_cancellation, nunca
    el dispatch_status en si). Cubre PENDING<->PREPARING<->COMPLETE<->DISPATCHED (ver
    _LEGAL_DISPATCH_TRANSITIONS); PENDING_ACCEPTANCE->PENDING sigue siendo trabajo
    exclusivo de accept_order.

    COMPLETE->DISPATCHED solo aplica a ordenes DELIVERYMAN (para PICKUP, COMPLETE ya es
    el estado terminal - "Listo para recoger" es solo la etiqueta que el dashboard le da
    a COMPLETE, no un estado propio). DISPATCHED->COMPLETE (revertir) se bloquea si la
    orden ya tiene shipping_label real de envia.com generado - ese hecho (guia con costo
    ya pagado) no es reversible desde aqui, a diferencia de un DISPATCHED alcanzado
    manualmente por esta misma funcion."""
    order = await db.scalar(select(Order).where(Order.uuid == order_uuid, Order.deleted_at.is_(None)))
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Orden no encontrada.")
    if order.status == "CANCELLED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Esta orden ya fue cancelada.")

    current = order.dispatch_status
    if target_status not in _LEGAL_DISPATCH_TRANSITIONS.get(current, set()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Transición de estado no permitida (de '{current}' a '{target_status}').",
        )

    if target_status == "DISPATCHED" and (order.delivery_info or {}).get("deliveryType") != "DELIVERYMAN":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Los pedidos de recolección en tienda no tienen paso de envío.")
    if current == "DISPATCHED" and target_status == "COMPLETE" and order.shipping_label:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se puede revertir: esta orden ya tiene una guía de envío real generada con envia.com.",
        )

    order.dispatch_status = target_status

    await db.commit()
    await db.refresh(order)
    logger.info(f"Orden {order.uuid}: dispatchStatus '{current}' -> '{target_status}' via /admin.")

    delivery_type = (order.delivery_info or {}).get("deliveryType")
    try:
        if target_status == "COMPLETE" and delivery_type == "PICKUP":
            await order_status_notification_service.notify_order_ready_for_pickup(order)
        elif target_status == "DISPATCHED":
            await order_status_notification_service.notify_order_dispatched(order)
    except Exception as e:
        logger.error(f"Fallo inesperado notificando el cambio de estado de la orden {order.uuid}: {type(e).__name__}: {e!r}")

    return order


async def assign_delivery(db: AsyncSession, order_uuid: str, delivery_company: str) -> Order:
    order = await db.scalar(select(Order).where(Order.uuid == order_uuid, Order.deleted_at.is_(None)))
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Orden no encontrada.")

    order.delivery_company = delivery_company
    order.delivery_assigned_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(order)
    logger.info(f"Orden {order.uuid} asignada a mensajeria '{delivery_company}' via /admin.")
    return order


async def _get_order_ready_for_shipping(db: AsyncSession, order_uuid: str) -> Order:
    """Lookup + validaciones compartidas por quote_shipping/generate_shipping_label -
    ver ADMIN_INTEGRATION.md, seccion "Guia de envio con envia.com". No confiar en que el
    frontend ya valido deliveryType/dispatchStatus antes de llamar aqui."""
    order = await db.scalar(select(Order).where(Order.uuid == order_uuid, Order.deleted_at.is_(None)))
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Orden no encontrada.")
    if (order.delivery_info or {}).get("deliveryType") != "DELIVERYMAN":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Esta orden no es de entrega a domicilio.")
    if order.dispatch_status != "COMPLETE":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Esta orden todavía no está lista para generar envío (dispatchStatus debe ser COMPLETE).",
        )
    missing = shipping_service.missing_address_fields(order.delivery_address_snapshot)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"La dirección de entrega de esta orden está incompleta para generar un envío (faltan: {', '.join(missing)}).",
        )
    return order


async def quote_shipping(db: AsyncSession, order_uuid: str, data: ShippingQuoteRequest) -> list[ShippingQuoteOption]:
    """No persiste nada - se puede llamar repetidamente mientras el admin ajusta
    peso/medidas antes de generar la guia real (ver quote_shipping vs
    generate_shipping_label en ADMIN_INTEGRATION.md). `data.origin` (opcional) permite
    overridear cualquier subconjunto de campos del origen - ver ShippingOriginOverride."""
    order = await _get_order_ready_for_shipping(db, order_uuid)
    origin_overrides = data.origin.model_dump(exclude_none=True) if data.origin else None
    options = await shipping_service.get_shipping_quote(order, data.weight, data.length, data.width, data.height, origin_overrides)
    return [ShippingQuoteOption(**opt) for opt in options]


async def generate_shipping_label(db: AsyncSession, order_uuid: str, data: ShippingGenerateRequest) -> Order:
    """Genera una guia real (con costo) via envia.com y, si tiene exito, persiste el
    resultado y avanza dispatch_status a DISPATCHED de inmediato - puramente local,
    Sicar X no se entera de esto (ver CLAUDE.md, "SICAR es solo ERP de inventario": el
    unico aviso a Sicar X en todo el ciclo de vida de una orden es el descuento/reversión
    de inventario en accept_order/prepare_local_cancellation, nunca el dispatch_status)."""
    order = await _get_order_ready_for_shipping(db, order_uuid)
    if order.shipping_label:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Esta orden ya tiene una guía de envío generada.")

    origin_overrides = data.origin.model_dump(exclude_none=True) if data.origin else None
    label = await shipping_service.generate_shipping_label(
        order, data.weight, data.length, data.width, data.height, data.carrier, data.service, origin_overrides,
    )

    order.shipping_label = label
    order.dispatch_status = "DISPATCHED"

    await db.commit()
    await db.refresh(order)
    logger.info(f"Guía de envío generada para la orden {order.uuid} (carrier={data.carrier}, service={data.service}) via /admin.")

    try:
        await order_status_notification_service.notify_order_dispatched(order)
    except Exception as e:
        logger.error(f"Fallo inesperado notificando 'orden enviada' para la orden {order.uuid}: {type(e).__name__}: {e!r}")

    return order
