import logging
from decimal import Decimal
from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.models.order import Order
from app.schemas.orders import ProductItem
from app.services.cancel_service import fetch_document_dispatch_status, process_order_cancellation
from app.services.email_service import send_order_confirmation_email
from app.services.order_service import pay_order_in_sicar

logger = logging.getLogger(__name__)

# Estados terminales de un pago de Mercado Pago - ver finalize_order_payment.
MP_APPROVED_STATUSES = {"approved"}
MP_PENDING_STATUSES = {"pending", "in_process"}
MP_FAILED_STATUSES = {"rejected", "cancelled"}

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

async def create_local_order(db: AsyncSession, client_account_id: int, order_payload_dict: dict, sicar_response: dict) -> Order:
    """Persiste localmente la orden recien creada (y reservada) en Sicar X, ANTES de
    intentar cualquier cobro con Mercado Pago - status siempre "TO_PAY" en este punto
    (pay_order_in_sicar todavia no corre; ver order_history_service.finalize_order_payment,
    que es quien la transiciona a PAID/CANCELLED segun el resultado del pago). Es la
    unica fuente de historial de ordenes - Sicar X no expone un endpoint para listar
    ordenes por cliente (ver CLAUDE.md).

    `sicar_response` es la respuesta de `create_order_in_sicar` (creacion del documento),
    no la de `pay_order_in_sicar` (que ya no corre en este punto del flujo)."""
    eco_order = order_payload_dict["ecOrderDto"]

    order = Order(
        client_account_id=client_account_id,
        sicar_order_id=str(sicar_response.get("id")),
        serie_folio=sicar_response.get("serieFolio"),
        sicar_date=_parse_sicar_date(sicar_response.get("date")),
        status="TO_PAY",
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

    logger.info(f"Orden local {order.uuid} creada (TO_PAY) para cliente {client_account_id} (sicar_order_id={order.sicar_order_id}).")
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

async def get_order_by_uuid(db: AsyncSession, order_uuid: str) -> Order | None:
    """Sin filtro de cliente - usada por el webhook de Mercado Pago
    (`POST /payments/webhook`), que no tiene identidad de cliente (lo llama Mercado
    Pago, no el frontend). `order_uuid` es el `external_reference` que se manda a
    Mercado Pago en `create_preference`/`create_payment`."""
    return await db.scalar(select(Order).where(Order.uuid == order_uuid))

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

async def finalize_order_payment(db: AsyncSession, order: Order, mp_payment: dict) -> Order:
    """Aplica el resultado de un pago de Mercado Pago a una orden local - punto unico
    compartido por `POST /orders/{id}/pay` (submit sincrono desde el Payment Brick) y el
    webhook (`POST /payments/webhook`, unico camino para el metodo Wallet, que nunca
    toca nuestro backend en el submit - ver payment_service.py). No duplicar esta logica
    en ambos lugares.

    `mp_payment` es la respuesta ya re-consultada de Mercado Pago (nunca el cuerpo crudo
    de una notificacion de webhook, que no es autoritativo).

    Al transicionar de TO_PAY a PAID (nunca en reintentos del webhook sobre una orden ya
    PAID), tambien dispara el correo de confirmacion via Resend (email_service.py) - este
    es el unico punto donde los tres caminos de pago (tarjeta/OXXO sincrono, y Wallet/OXXO
    tardio via webhook) convergen, asi que es el unico lugar correcto para enviarlo; el
    frontend no puede hacerlo por su cuenta porque el metodo Wallet nunca le llega una
    respuesta sincrona (ver CLAUDE.md, "Payments with Mercado Pago")."""
    mp_status = mp_payment.get("status")

    order.mp_payment_id = str(mp_payment.get("id")) if mp_payment.get("id") is not None else order.mp_payment_id
    order.mp_status = mp_status
    order.mp_status_detail = mp_payment.get("status_detail")
    order.mp_payment_method_id = mp_payment.get("payment_method_id")
    ticket_url = (mp_payment.get("transaction_details") or {}).get("external_resource_url")
    if ticket_url:
        order.mp_ticket_url = ticket_url

    became_paid = False
    if mp_status in MP_APPROVED_STATUSES:
        if order.status != "PAID":
            became_paid = True
            await pay_order_in_sicar(
                order_id=order.sicar_order_id,
                total_amount=float(order.total),
                cash_register_uuid=settings.CASH_REGISTER_UUID,
                branch_id=order.branch_id,
            )
        order.status = "PAID"
    elif mp_status in MP_PENDING_STATUSES:
        order.status = "TO_PAY"
    elif mp_status in MP_FAILED_STATUSES:
        if order.status != "CANCELLED":
            products_to_restore = [
                ProductItem(uuid=item.get("uuid"), quantity=float(item.get("quantity", 0)))
                for item in (order.items or [])
            ]
            await process_order_cancellation(
                db,
                order.sicar_order_id,
                settings.CASH_REGISTER_UUID,
                products_to_restore,
            )
        order.status = "CANCELLED"

    await db.commit()
    await db.refresh(order)

    if became_paid:
        await send_order_confirmation_email(order)

    logger.info(f"Orden local {order.uuid} finalizada con estado de Mercado Pago '{mp_status}' -> status local '{order.status}'.")
    return order
