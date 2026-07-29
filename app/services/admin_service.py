import logging
from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.order import Order, SicarSyncOutbox
from app.models.product import SyncStatus
from app.schemas.admin import AdminHealthResponse, SyncStatusResponse, AdminOrderPublic
from app.services.sicar_auth import sicar_auth

logger = logging.getLogger(__name__)

OUTBOX_STATUSES = ("PENDING", "IN_PROGRESS", "SUCCEEDED", "FAILED")


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

    query = select(Order)
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
    """Marca la orden como aceptada localmente y encola una fila de sicar_sync_outbox con
    action="ACCEPT" para que el worker intente avanzar el dispatchStatus real en Sicar X.

    El branch "ACCEPT" en sicar_sync_worker.py llama a cancel_service.advance_dispatch_status
    (mutacion real capturada en vivo contra app.sicarx.com, ver su docstring) para avanzar
    dispatchStatus a PENDING en Sicar X. Esta funcion solo se encarga de la mitad local:
    dejar accepted_at/accepted_by en la fila y encolar el intento - la mitad de Sicar X
    corre de forma asincrona, igual que CANCEL."""
    order = await db.scalar(select(Order).where(Order.uuid == order_uuid, Order.deleted_at.is_(None)))
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Orden no encontrada.")
    if order.accepted_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Esta orden ya fue aceptada.")

    order.accepted_at = datetime.now(timezone.utc)
    order.accepted_by = accepted_by

    db.add(SicarSyncOutbox(
        order_id=order.id,
        action="ACCEPT",
        cash_register_uuid="",  # no aplica a esta accion, pero la columna es NOT NULL
        status="PENDING",
        next_attempt_at=datetime.now(timezone.utc),
    ))

    await db.commit()
    await db.refresh(order)
    logger.info(f"Orden {order.uuid} aceptada por '{accepted_by}' via /admin; encolada sincronizacion ACCEPT hacia Sicar X.")
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
