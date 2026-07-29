import logging
from decimal import Decimal
from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.models.order import Order, SicarSyncOutbox
from app.services.cancel_service import fetch_document_dispatch_status
from app.services.order_notification_service import notify_order_confirmed
from app.services.order_cancellation_notification_service import notify_order_cancelled
from app.services.order_service import pay_order_in_sicar, _to_decimal
from app.services.product_stock_service import apply_stock_deltas

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

async def create_local_order(db: AsyncSession, client_account_id: int, order_payload_dict: dict, sicar_response: dict, local_products: dict | None = None) -> Order:
    """Persiste localmente la orden recien creada (y reservada) en Sicar X, ANTES de
    intentar cualquier cobro con Mercado Pago - status siempre "TO_PAY" en este punto
    (pay_order_in_sicar todavia no corre; ver order_history_service.finalize_order_payment,
    que es quien la transiciona a PAID/CANCELLED segun el resultado del pago). Es la
    unica fuente de historial de ordenes - Sicar X no expone un endpoint para listar
    ordenes por cliente (ver CLAUDE.md).

    `sicar_response` es la respuesta de `create_order_in_sicar` (creacion del documento),
    no la de `pay_order_in_sicar` (que ya no corre en este punto del flujo).

    `local_products` (sicar_uuid -> Product, el mismo dict ya cargado en routes/orders.py
    para build_order_payload) se usa solo para agregar `imageUrl` a cada item guardado
    aqui - una copia de `eco_order["products"]`, NUNCA el mismo objeto que ya se envio a
    Sicar X en create_order_in_sicar (ese ya salio por la red antes de llegar aqui, asi
    que agregarle un campo extra en este punto no afecta lo que Sicar X recibio).

    NO hace commit — solo `flush()` (para que `order.id`/`order.uuid` queden disponibles
    para el llamador, p.ej. payment_service.create_preference). El llamador
    (routes/orders.py::create_order) hace el único commit, junto con el descuento de
    stock ya preparado por create_order_in_sicar, para que ambos se persistan o se
    reviertan juntos."""
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
        items=items,
    )
    db.add(order)
    await db.flush()

    logger.info(f"Orden local {order.uuid} creada (TO_PAY, pendiente de commit) para cliente {client_account_id} (sicar_order_id={order.sicar_order_id}).")
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
    """Usada por POST /cancel para verificar que la orden a cancelar pertenece al
    cliente autenticado antes de proceder - 404 en vez de 403 para no filtrar la
    existencia de ordenes de otros clientes (mismo patron que address_service).

    Filtra `deleted_at.is_(None)`: una orden ya borrada via DELETE /orders/{id} debe
    resolver como "no encontrada" - sin esto, un segundo DELETE o un /cancel posterior
    sobre la misma orden encontraria la fila (soft-deleted) y reabriria un camino que
    con el borrado duro de antes era imposible (ver Riesgo R2 del plan)."""
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

async def get_order_by_uuid(db: AsyncSession, order_uuid: str) -> Order | None:
    """Sin filtro de cliente - usada por el webhook de Mercado Pago
    (`POST /payments/webhook`), que no tiene identidad de cliente (lo llama Mercado
    Pago, no el frontend). `order_uuid` es el `external_reference` que se manda a
    Mercado Pago en `create_preference`/`create_payment`.

    Filtra `deleted_at.is_(None)` por la misma razon que get_owned_order_by_sicar_id:
    una orden borrada por el cliente (DELETE /orders/{id}) no debe poder ser
    resucitada/mutada por una notificacion de pago tardia (p. ej. una ficha OXXO
    pagada despues de "eliminar" la reserva) - ver Riesgo R2 del plan."""
    return await db.scalar(select(Order).where(Order.uuid == order_uuid, Order.deleted_at.is_(None)))

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

async def prepare_local_cancellation(
    db: AsyncSession, order: Order, *, cash_register_uuid: str | None = None, require_status: str | None = None,
) -> Order:
    """Deja una orden lista para cancelarse LOCALMENTE, sin tocar Sicar X todavia:
    relockea la fila, restaura stock, marca CANCELLED y encola una fila en
    sicar_sync_outbox para que app/worker/sicar_sync_worker.py le avise a Sicar X de
    forma asincrona, con reintentos. Es el unico punto que llaman las 3 rutas de
    cancelacion (POST /cancel, DELETE, y la rama rejected/cancelled de
    finalize_order_payment mas abajo) - no duplicar esta logica en ninguna de ellas.

    Relockea con SELECT...FOR UPDATE y re-chequea el status DENTRO del lock (no antes):
    al quitar la llamada sincrona a Sicar X de la ruta de cancelacion, se perdio el
    mutex accidental que esa llamada representaba (hoy, dos cancelaciones concurrentes
    sobre la misma orden se resuelven porque Sicar X rechaza la segunda antes de que
    llegue a restaurar stock). Sin este lock, dos intentos concurrentes (doble click, o
    /cancel corriendo contra un webhook de Mercado Pago tardio) podrian pasar ambos un
    chequeo "todavia no esta CANCELLED" sin lock y restaurar stock por duplicado.
    Re-entrante y no-op seguro para finalize_order_payment, que ya sostiene este mismo
    lock cuando llama aqui.

    `require_status` permite que DELETE (solo valido sobre TO_PAY) lo exija DENTRO del
    lock, no solo en un chequeo previo a el - sin esto, un /pay que gane la carrera
    contra un DELETE podria dejar la orden en PAID y que DELETE la cancelara de todos
    modos.

    `cash_register_uuid` se captura aqui (en la fila de outbox) porque, una vez que la
    llamada real a Sicar X se difiere al worker, ya no hay acceso a la caja registradora
    que el cliente pidio en el request original (OrderCancel.cashRegisterUuid) - sin
    esto se perderia silenciosamente. Si no se especifica, usa la caja por default.

    NO hace commit - mismo patron que el resto de este archivo, el llamador es
    responsable de un unico commit."""
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

    deltas = [(item.get("uuid"), _to_decimal(item.get("quantity", 0))) for item in (order.items or [])]
    await apply_stock_deltas(db, deltas)

    order.status = "CANCELLED"
    db.add(SicarSyncOutbox(
        order_id=order.id,
        action="CANCEL",
        cash_register_uuid=cash_register_uuid or settings.CASH_REGISTER_UUID,
        status="PENDING",
        next_attempt_at=datetime.now(timezone.utc),
    ))

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
    PAID), tambien notifica al frontend via un webhook firmado
    (order_notification_service.notify_order_confirmed) para que el frontend envie el
    correo de confirmacion con su propio template de react-email y su propia cuenta de
    Resend - este es el unico punto donde los tres caminos de pago (tarjeta/OXXO
    sincrono, y Wallet/OXXO tardio via webhook) convergen, asi que es el unico lugar
    donde el trigger puede vivir de forma confiable; el frontend no puede dispararlo por
    su cuenta (ni por polling) porque el metodo Wallet nunca le llega una respuesta
    sincrona ni garantiza que el navegador siga abierto (ver CLAUDE.md, "Payments with
    Mercado Pago").

    El pago sincrono (`/orders/{id}/pay`) y el webhook de Mercado Pago pueden llegar casi
    al mismo tiempo para la misma orden (el webhook tambien sirve de respaldo para
    tarjeta/OXXO, no solo Wallet) - cada uno con su propia sesion de DB. Por eso lo
    primero que hace esta funcion es re-leer la fila con `SELECT ... FOR UPDATE`: eso
    serializa a ambos llamadores contra el bloqueo de fila de Postgres, para que el
    segundo en llegar vea el estado ya actualizado por el primero (status == PAID) en
    vez de aplicar `pay_order_in_sicar`/`notify_order_confirmed` por duplicado.

    Nota sobre atomicidad: esto ya solo aplica al camino PAID. `pay_order_in_sicar`
    (llamada HTTP real a Sicar X) ocurre ANTES del unico `db.commit()` de esta funcion,
    que es lo mas cerca que se puede quedar de "un solo commit para todos los cambios
    locales" sin una infraestructura de outbox/saga para ese camino especifico. Si esa
    llamada HTTP externa ya tuvo exito pero el commit de abajo falla, el estado local
    queda desincronizado de lo que realmente paso en Sicar/Mercado Pago - ese caso no se
    puede cerrar del todo aqui, solo se vuelve diagnosticable (ver el log CRITICAL mas
    abajo) en vez de silencioso. El camino CANCELLED ya no tiene este problema: desde
    que existe sicar_sync_outbox (ver prepare_local_cancellation arriba), la cancelacion
    en Sicar X es asincrona y con reintentos - si el commit de abajo falla, Sicar X
    nunca llego a tocarse, asi que no hay nada que reconciliar."""
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
            await pay_order_in_sicar(
                order_id=order.sicar_order_id,
                total_amount=float(order.total),
                cash_register_uuid=settings.CASH_REGISTER_UUID,
                branch_id=order.branch_id,
            )
        order.status = "PAID"
    elif mp_status in MP_PENDING_STATUSES:
        # Solo aplica si la orden sigue en TO_PAY: un pedido puede tener mas de un intento
        # de pago (p. ej. un ticket OXXO pendiente mientras el cliente tambien prueba una
        # tarjeta) - una notificacion "pending"/"in_process" tardia de un intento distinto
        # al que realmente termino aprobando/cancelando la orden NO debe regresarla a
        # TO_PAY. Ver CLAUDE.md / hallazgo de auditoria sobre este mismo bug.
        if order.status == "TO_PAY":
            order.status = "TO_PAY"
    elif mp_status in MP_FAILED_STATUSES:
        # Misma razon que arriba: solo cancelar si la orden sigue en TO_PAY. Sin este
        # guard, una notificacion rechazada/cancelada de un intento de pago distinto
        # (p. ej. un ticket OXXO que expira) podia cancelar una orden que otro intento ya
        # habia dejado PAID, restaurando stock sobre un cobro ya aplicado.
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
            await notify_order_confirmed(order)
        except Exception as e:
            logger.error(f"Fallo inesperado (no manejado por notify_order_confirmed) notificando la orden {order.uuid}: {type(e).__name__}: {e!r}")
    if became_cancelled:
        try:
            await notify_order_cancelled(order)
        except Exception as e:
            logger.error(f"Fallo inesperado (no manejado por notify_order_cancelled) notificando la orden {order.uuid}: {type(e).__name__}: {e!r}")

    logger.info(f"Orden local {order.uuid} finalizada con estado de Mercado Pago '{mp_status}' -> status local '{order.status}'.")
    return order
