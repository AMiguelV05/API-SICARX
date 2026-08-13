import logging
import uuid
from decimal import Decimal
from datetime import datetime, timezone
from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.models.order import Order, SicarSyncOutbox
from app.services.order_notification_service import notify_order_confirmed
from app.services.order_cancellation_notification_service import notify_order_cancelled
from app.services.order_service import _to_decimal
from app.services.product_stock_service import apply_reserved_deltas, apply_sales_count_deltas
from app.services import coupon_service

logger = logging.getLogger(__name__)

# Estados terminales de un pago de Mercado Pago - ver finalize_order_payment.
MP_APPROVED_STATUSES = {"approved"}
MP_PENDING_STATUSES = {"pending", "in_process"}
MP_FAILED_STATUSES = {"rejected", "cancelled"}

async def create_local_order(
    db: AsyncSession, client_account_id: int | None, order_payload_dict: dict, local_products: dict | None = None,
    delivery_address_snapshot: dict | None = None, coupon_id: int | None = None, coupon_code: str | None = None,
    guest_email: str | None = None,
) -> Order:
    """Persiste la orden localmente (status siempre "TO_PAY" en este punto; ver
    finalize_order_payment). `sicar_order_id` se genera aqui con `uuid.uuid4()` - ya no
    viene de una respuesta real de Sicar X, checkout no le avisa nada todavia.
    `local_products` solo se usa para agregar `imageUrl` a cada item guardado.
    `coupon_id`/`coupon_code` (opcionales) ya vienen validados/bloqueados por el llamador
    (routes/orders.py, via coupon_service.lock_and_validate_coupon) - aqui solo se persisten.

    Checkout de invitado: `client_account_id=None` + `guest_email` (ya en minuscula, ver
    routes/orders.py). Exactamente uno de los dos debe venir poblado - lo exige
    `ck_orders_has_identity` a nivel de base de datos, no se revalida aqui.

    NO hace commit, solo `flush()` (para que `order.id`/`order.uuid` esten disponibles al
    llamador) - `routes/orders.py::create_order` hace el unico commit junto con el
    descuento de stock, para que ambos se persistan o reviertan juntos."""
    eco_order = order_payload_dict["ecOrderDto"]
    local_products = local_products or {}

    items = []
    for line in eco_order.get("products") or []:
        item = dict(line)
        product = local_products.get(item.get("uuid"))
        item["imageUrl"] = product.image_url if product else None
        items.append(item)

    order = Order(
        client_account_id=client_account_id,
        guest_email=guest_email,
        sicar_order_id=str(uuid.uuid4()),
        status="TO_PAY",
        # Estado inicial local del tablero de despacho - Sicar X todavia no sabe de la orden.
        dispatch_status="PENDING_ACCEPTANCE",
        branch_id=order_payload_dict.get("branchId"),
        total=Decimal(str(eco_order.get("total"))),
        total_quantity=Decimal(str(order_payload_dict.get("totalQuantity"))),
        delivery_info=eco_order.get("deliveryInfo"),
        items=items,
        delivery_address_snapshot=delivery_address_snapshot,
        coupon_id=coupon_id,
        coupon_code=coupon_code,
        subtotal=Decimal(str(eco_order.get("subtotal"))) if eco_order.get("subtotal") is not None else None,
        discount_amount=Decimal(str(eco_order.get("discountAmount"))) if eco_order.get("discountAmount") is not None else None,
    )
    db.add(order)
    await db.flush()

    identity = f"cliente {client_account_id}" if client_account_id is not None else f"invitado {guest_email}"
    logger.info(f"Orden local {order.uuid} creada (TO_PAY, pendiente de commit) para {identity} (sicar_order_id={order.sicar_order_id}).")
    return order

async def list_client_orders(db: AsyncSession, client_account_id: int, limit: int, offset: int) -> tuple[int, list[Order]]:
    total = await db.scalar(
        select(func.count()).select_from(Order)
        .where(Order.client_account_id == client_account_id, Order.deleted_at.is_(None))
    )
    result = await db.execute(
        select(Order)
        .where(Order.client_account_id == client_account_id, Order.deleted_at.is_(None))
        .order_by(Order.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return total or 0, list(result.scalars().all())

async def get_client_order(db: AsyncSession, client_account_id: int, order_uuid: str) -> Order:
    order = await db.scalar(
        select(Order).where(
            Order.uuid == order_uuid, Order.client_account_id == client_account_id, Order.deleted_at.is_(None)
        )
    )
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Orden no encontrada.")
    return order

async def get_owned_order_by_sicar_id(db: AsyncSession, client_account_id: int, sicar_order_id: str) -> Order:
    """Verifica que la orden pertenezca al cliente autenticado antes de cancelar - 404 en
    vez de 403 para no filtrar existencia (mismo patron que address_service). Filtra
    `deleted_at` para que una orden ya borrada no reabra un DELETE/cancel duplicado."""
    order = await db.scalar(
        select(Order).where(
            Order.sicar_order_id == sicar_order_id,
            Order.client_account_id == client_account_id,
            Order.deleted_at.is_(None),
        )
    )
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Orden no encontrada.")
    return order

async def get_order_for_action(db: AsyncSession, client: "ClientAccount | None", order_id: str) -> Order:
    """Punto unico de resolucion para /pay, /cancel, DELETE y el status de invitado.

    Cuenta (`client` no None): sin cambios - `order_id` es `sicar_order_id`, ownership por
    `client_account_id`, via `get_owned_order_by_sicar_id`.

    Invitado (`client is None`): no hay sesion que demuestre pertenencia, asi que la prueba
    es poseer el identificador de la orden en si - `order_id` puede ser `sicar_order_id` O
    `uuid` (ambos alta entropia, ambos ya devueltos como `id`/`orderUuid` al hacer checkout),
    y debe seguir sin cuenta (`client_account_id IS NULL`). Esto deja que el frontend
    reutilice el mismo campo `id` que ya usa para cuentas, sin distinguir por estado de
    login. 404 (no 403) si no existe o si la orden ya fue vinculada a una cuenta (ver
    client_service.link_guest_orders_by_email) - mismo patron "no confirmar existencia" que
    el resto de este archivo; una vez vinculada, el cliente ya debe usar la ruta autenticada."""
    if client is not None:
        return await get_owned_order_by_sicar_id(db, client.id, order_id)

    order = await db.scalar(
        select(Order).where(
            or_(Order.sicar_order_id == order_id, Order.uuid == order_id),
            Order.client_account_id.is_(None),
            Order.deleted_at.is_(None),
        )
    )
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Orden no encontrada.")
    return order

async def get_order_by_uuid(db: AsyncSession, order_uuid: str) -> Order | None:
    """Sin filtro de cliente - la usa el webhook de Mercado Pago (sin identidad de
    cliente); `order_uuid` es el `external_reference` mandado a Mercado Pago. Filtra
    `deleted_at` para que un pago tardio no resucite/mute una orden ya borrada por el
    cliente."""
    return await db.scalar(select(Order).where(Order.uuid == order_uuid, Order.deleted_at.is_(None)))

async def prepare_local_cancellation(
    db: AsyncSession, order: Order, *, cash_register_uuid: str | None = None, require_status: str | None = None,
) -> Order:
    """Cancela LOCALMENTE sin tocar Sicar X todavia: relockea la fila, marca CANCELLED y,
    solo si la orden ya habia sido aceptada (`accepted_at is not None`), encola una fila en
    sicar_sync_outbox para que el worker avise a Sicar X de forma asincrona con reintentos
    (si nunca fue aceptada, Sicar X nunca supo de esta orden). Unico punto llamado por las
    3 rutas de cancelacion (POST /cancel, DELETE, rama rejected/cancelled de
    finalize_order_payment) - no duplicar esta logica.

    Inventario: si `accepted_at is None`, la orden solo tenia una reserva local
    (Product.reserved, ver checkout en routes/orders.py) que se libera aqui mismo - Sicar X
    nunca supo de esta orden, nada que reconciliar. Si ya fue aceptada, Product.stock ya fue
    descontado de forma local+real al aceptar (ver sicar_sync_worker.py) y su restauracion
    ocurre de forma asincrona cuando el CANCEL encolado abajo tenga exito en el worker - no
    se toca inventario local aqui en ese caso (evita la reversion "instantanea pero
    incorrecta" que el siguiente sync periodico terminaba revirtiendo de nuevo).

    SELECT...FOR UPDATE + re-chequeo de status DENTRO del lock es el unico mutex real
    ahora que no hay llamada sincrona a Sicar X que serialice cancelaciones concurrentes -
    sin el, dos intentos concurrentes (doble click, /cancel vs. webhook tardio) podrian
    restaurar stock por duplicado. Re-entrante para finalize_order_payment, que ya
    sostiene este mismo lock. `require_status` deja que DELETE lo exija dentro del lock
    (evita perder una carrera contra /pay). `cash_register_uuid` se captura aqui porque el
    worker ya no tiene acceso al request original.

    NO hace commit - el llamador es responsable de un unico commit."""
    locked_result = await db.execute(
        select(Order)
        .where(Order.id == order.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    order = locked_result.scalar_one()

    if order.status == "CANCELLED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Esta orden ya fue cancelada.")
    if require_status is not None and order.status != require_status:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Esta orden ya no está en el estado esperado para esta operación.",
        )

    # Solo una orden que llego a estar PAID incremento sales_count (ver finalize_order_payment) - hay que revertirlo aqui; una que se cancela desde TO_PAY nunca lo incremento.
    was_paid = order.status == "PAID"

    # Libera la redencion del cupon SOLO si sigue PENDING (la orden nunca llego a PAID) - una
    # ya CONFIRMED no se toca (anti-abuso deliberado, ver CLAUDE.md/coupon_service). Se decide
    # por el estado de la propia redencion, no el de la orden, asi que es seguro llamarla
    # incondicionalmente en las 3 rutas de cancelacion que comparten esta funcion.
    if order.coupon_id:
        await coupon_service.release_redemption(db, order)

    deltas = [(item.get("uuid"), _to_decimal(item.get("quantity", 0))) for item in (order.items or [])]
    if order.accepted_at is None:
        # Nunca se le aviso a Sicar X de esta orden - solo liberar el hold local.
        await apply_reserved_deltas(db, [(product_uuid, -qty) for product_uuid, qty in deltas])
    # else: la restauracion de Product.stock ocurre en sicar_sync_worker.py cuando el
    # SicarSyncOutbox(action="CANCEL") encolado abajo tenga exito.
    if was_paid:
        await apply_sales_count_deltas(db, [(product_uuid, -qty) for product_uuid, qty in deltas])

    order.status = "CANCELLED"
    if order.accepted_at is not None:
        db.add(SicarSyncOutbox(
            order_id=order.id,
            action="CANCEL",
            cash_register_uuid=cash_register_uuid or settings.CASH_REGISTER_UUID,
            status="PENDING",
            next_attempt_at=datetime.now(timezone.utc),
        ))

    return order

async def finalize_order_payment(db: AsyncSession, order: Order, mp_payment: dict, background_tasks: BackgroundTasks) -> Order:
    """Aplica el resultado de un pago de Mercado Pago a la orden local - punto unico
    compartido por `/orders/{id}/pay` (submit sincrono) y el webhook (unico camino para
    Wallet); no duplicar esta logica. `mp_payment` es la respuesta re-consultada de
    Mercado Pago, nunca el cuerpo crudo de una notificacion de webhook.

    `SELECT ... FOR UPDATE` al inicio serializa el pago sincrono y el webhook cuando
    llegan casi al mismo tiempo para la misma orden, para no notificar duplicado.
    Notifica al frontend (`notify_order_confirmed`) solo en la transicion real TO_PAY ->
    PAID, nunca en reintentos del webhook - es el unico punto donde los tres caminos de
    pago convergen (ver CLAUDE.md, "Payments with Mercado Pago").

    Ya no hace ninguna llamada HTTP a Sicar X aqui - el unico aviso a Sicar X en el ciclo
    de vida de una orden ocurre al aceptarla o cancelarla, ambos asincronos via
    `sicar_sync_outbox` (ver CLAUDE.md, "SICAR es solo ERP de inventario")."""
    locked_result = await db.execute(
        select(Order)
        .where(Order.id == order.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    order = locked_result.scalar_one()

    mp_status = mp_payment.get("status")

    order.mp_payment_id = str(mp_payment.get("id")) if mp_payment.get("id") is not None else order.mp_payment_id
    order.mp_status = mp_status
    order.mp_status_detail = mp_payment.get("status_detail")
    order.mp_payment_method_id = mp_payment.get("payment_method_id")
    ticket_url = (mp_payment.get("transaction_details") or {}).get("external_resource_url")
    if ticket_url:
        order.mp_ticket_url = ticket_url

    became_paid = False
    became_cancelled = False
    if mp_status in MP_APPROVED_STATUSES:
        if order.status != "PAID":
            became_paid = True
        order.status = "PAID"
        if became_paid:
            # Registro de "mas vendidos" (Product.sales_count) - solo en la transicion real, nunca en un reintento del webhook para una orden ya PAID.
            deltas = [(item.get("uuid"), _to_decimal(item.get("quantity", 0))) for item in (order.items or [])]
            await apply_sales_count_deltas(db, deltas)
            # Idem para la redencion del cupon: consumo permanente solo en la transicion real a PAID.
            if order.coupon_id:
                await coupon_service.confirm_redemption(db, order)
    elif mp_status in MP_PENDING_STATUSES:
        # Solo aplica si sigue TO_PAY - una notificacion pending/in_process tardia de OTRO intento de pago no debe regresar una orden ya resuelta a TO_PAY.
        if order.status == "TO_PAY":
            order.status = "TO_PAY"
    elif mp_status in MP_FAILED_STATUSES:
        # Idem: solo cancela si sigue TO_PAY - evita que un intento de pago fallido cancele una orden que otro intento ya dejo PAID.
        if order.status == "TO_PAY":
            became_cancelled = True
            order = await prepare_local_cancellation(db, order)

    try:
        await db.commit()
    except Exception:
        logger.critical(
            f"Fallo el commit final de finalize_order_payment para la orden "
            f"{order.sicar_order_id} (mp_payment_id={mp_payment.get('id')}, "
            f"mp_status='{mp_status}') DESPUES de que la llamada externa a Sicar/Mercado "
            f"Pago ya se aplico - el estado local puede haber quedado desincronizado, "
            f"revisar manualmente."
        )
        raise
    await db.refresh(order)

    if became_paid:
        try:
            await notify_order_confirmed(order, background_tasks)
        except Exception as e:
            logger.error(f"Fallo inesperado (no manejado por notify_order_confirmed) notificando la orden {order.uuid}: {type(e).__name__}: {e!r}")
    if became_cancelled:
        try:
            await notify_order_cancelled(order, background_tasks)
        except Exception as e:
            logger.error(f"Fallo inesperado (no manejado por notify_order_cancelled) notificando la orden {order.uuid}: {type(e).__name__}: {e!r}")

    logger.info(f"Orden local {order.uuid} finalizada con estado de Mercado Pago '{mp_status}' -> status local '{order.status}'.")
    return order
