import logging
from decimal import Decimal
from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.order import Order
from app.services.cancel_service import fetch_document_dispatch_status

logger = logging.getLogger(__name__)

# Estados de nuestro campo local `status` que ya son definitivos - no tiene caso
# refrescar el dispatchStatus de una orden cancelada.
TERMINAL_STATUSES = {"CANCELLED"}

# dispatchStatus de Sicar X (document-graph/v1/graph-v2, confirmado en vivo contra el
# panel admin de Sicar X - ver CLAUDE.md) que ya significan que la orden llego a su
# estado final de cumplimiento; no hace falta seguir refrescando despues de esto.
TERMINAL_DISPATCH_STATUSES = {"COMPLETE", "DISPATCHED"}

def _parse_sicar_date(value) -> datetime | None:
    """`date`/`sicarTimestamp` de Sicar X llegan como epoch numerico; el formato
    (segundos vs milisegundos) no esta confirmado, asi que se infiere por magnitud.
    No es un campo critico - si falla el parseo, se guarda como None en vez de
    bloquear la creacion de la orden."""
    if value is None:
        return None
    try:
        numeric = float(value)
        seconds = numeric / 1000 if numeric > 1e12 else numeric
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        logger.warning(f"No se pudo interpretar la fecha de Sicar X: {value!r}")
        return None

async def create_local_order(db: AsyncSession, client_account_id: int, order_payload_dict: dict, payment_response: dict) -> Order:
    """Persiste localmente la orden ya creada y pagada en Sicar X. Es la unica fuente
    de historial de ordenes - Sicar X no expone un endpoint para listar ordenes por
    cliente (ver CLAUDE.md)."""
    eco_order = order_payload_dict["ecOrderDto"]

    order = Order(
        client_account_id=client_account_id,
        sicar_order_id=str(payment_response.get("id")),
        serie_folio=payment_response.get("serieFolio"),
        sicar_date=_parse_sicar_date(payment_response.get("date")),
        status=payment_response.get("status") or "PAID",
        # Toda orden REMOTE/PICKUP nueva entra al tablero de despacho de Sicar X en este
        # estado (confirmado en vivo) - se evita una llamada extra a Sicar en el camino
        # caliente de checkout solo para confirmar lo que ya sabemos.
        dispatch_status="PENDING_ACCEPTANCE",
        branch_id=order_payload_dict.get("branchId"),
        total=Decimal(str(eco_order.get("total"))),
        total_quantity=Decimal(str(order_payload_dict.get("totalQuantity"))),
        delivery_info=eco_order.get("deliveryInfo"),
        items=eco_order.get("products"),
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)

    logger.info(f"Orden local {order.uuid} creada para cliente {client_account_id} (sicar_order_id={order.sicar_order_id}).")
    return order

async def list_client_orders(db: AsyncSession, client_account_id: int, limit: int, offset: int) -> tuple[int, list[Order]]:
    total = await db.scalar(
        select(func.count()).select_from(Order).where(Order.client_account_id == client_account_id)
    )
    result = await db.execute(
        select(Order)
        .where(Order.client_account_id == client_account_id)
        .order_by(Order.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return total or 0, list(result.scalars().all())

async def get_client_order(db: AsyncSession, client_account_id: int, order_uuid: str) -> Order:
    order = await db.scalar(
        select(Order).where(Order.uuid == order_uuid, Order.client_account_id == client_account_id)
    )
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Orden no encontrada.")
    return order

async def get_owned_order_by_sicar_id(db: AsyncSession, client_account_id: int, sicar_order_id: str) -> Order:
    """Usada por POST /cancel para verificar que la orden a cancelar pertenece al
    cliente autenticado antes de proceder - 404 en vez de 403 para no filtrar la
    existencia de ordenes de otros clientes (mismo patron que address_service)."""
    order = await db.scalar(
        select(Order).where(Order.sicar_order_id == sicar_order_id, Order.client_account_id == client_account_id)
    )
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Orden no encontrada.")
    return order

async def refresh_order_status_if_needed(db: AsyncSession, order: Order) -> Order:
    """Refresca dispatch_status/dispatch_history desde Sicar X (generatedV2, ver
    cancel_service.fetch_document_dispatch_status) si la orden no esta en un estado
    definitivo. No falla la respuesta si Sicar X no responde - el detalle sigue
    sirviendose con el ultimo estado local conocido."""
    if order.status in TERMINAL_STATUSES or order.dispatch_status in TERMINAL_DISPATCH_STATUSES:
        return order

    remote = await fetch_document_dispatch_status(order.sicar_order_id)
    if not remote:
        return order

    changed = False
    if remote.get("dispatchStatus") and remote["dispatchStatus"] != order.dispatch_status:
        order.dispatch_status = remote["dispatchStatus"]
        changed = True
    if remote.get("dispatchHistory") != order.dispatch_history:
        order.dispatch_history = remote.get("dispatchHistory")
        changed = True

    if changed:
        await db.commit()
        await db.refresh(order)

    return order
