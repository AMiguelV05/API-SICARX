import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Body, Header, Request, status
from sqlalchemy import select
from app.core.database import DbDep
from app.core.security import validate_api_key, CurrentClientHeaderDep
from app.core.rate_limit import limiter
from app.models.product import Product
from app.models.order import Order
from app.services.order_service import validate_cart_items, build_order_payload, create_order_in_sicar
from app.services.session_service import get_or_refresh_customer_session
from app.services.order_history_service import create_local_order, get_owned_order_by_sicar_id, finalize_order_payment, prepare_local_cancellation
from app.services.order_cancellation_notification_service import notify_order_cancelled
from app.services.order_idempotency_service import claim_idempotency_key, is_claim_abandoned, discard_claim
from app.services.address_service import get_owned_address
from app.services import payment_service
from app.schemas.orders import OrderCancelResponse, OrderCreate, OrderCancel, OrderResponse, PaymentSubmit, OrderPayResponse
from app.schemas.client import ClientAddressPublic
from app.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/orders", tags=["Orders Creation and Cancellation"], dependencies=[Depends(validate_api_key)])

@router.post("", response_model=OrderResponse, summary="Crear pedido")
@limiter.limit("10/minute")
async def create_order(
    request: Request,
    client: CurrentClientHeaderDep,
    db: DbDep,
    order_payload: OrderCreate = Body(),
    authorization: str = Header(None, alias="Authorization", description="Token de sesión del cliente web"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", description="Opcional. Si se repite la misma clave (p. ej. un reintento de red del mismo submit de checkout), se devuelve la orden ya creada en vez de crear una duplicada."),
):
    """
    Contrato semiautomático: el frontend solo envía `products: [{uuid, quantity}]` y
    `deliveryInfo`; precios, impuestos, sku, descripción, unidad y totales se calculan
    en el backend a partir de Sicar X y del catálogo local (`order_service.build_order_payload`).

    `Idempotency-Key` (opcional): un identificador generado por el frontend (p. ej. un
    UUID por click en "pagar") que, si se reenvía sin cambios en un reintento, hace que
    esta llamada devuelva la orden ya creada la primera vez en vez de crear una segunda
    orden en Sicar X. Sin este header el comportamiento es igual que antes.

    Requiere DOS tokens distintos, ninguno reemplaza al otro:
    - `Authorization`: JWT de sesión del cliente web en Sicar X (obtenido de
      `POST /session/init`) — se usa para validar el carrito y crear la orden en Sicar X.
    - `X-Client-Token`: JWT de la cuenta de cliente local (obtenido de `POST /auth/login`
      o `/auth/register`) — identifica qué `ClientAccount` queda dueña de la orden para
      que después pueda verla en `GET /auth/me/orders`. Login ahora es obligatorio para
      comprar; ya no existe checkout anónimo.

    Esta llamada SOLO reserva el pedido en Sicar X (queda en `TO_PAY`) y prepara el cobro
    con Mercado Pago — todavía no cobra nada. Devuelve `preferenceId`/`amount` para que
    el frontend renderice el Payment Brick, y `orderUuid`/`id` para el siguiente paso:
    `POST /orders/{id}/pay` con el `formData` del `onSubmit` del Brick (tarjeta/OXXO). Si
    el comprador paga con Mercado Pago Wallet, esa vía nunca llama a este backend — el
    webhook (`POST /payments/webhook`) es quien confirma el pago en ese caso.

    `deliveryInfo.deliveryType` acepta `PICKUP` (recoger en tienda) o `DELIVERYMAN`
    (entrega a domicilio). Para `DELIVERYMAN`, `deliveryInfo.addressUuid` es obligatorio
    y debe ser el `uuid` de una dirección ya guardada del cliente autenticado (ver
    `GET /auth/me/addresses`) — se resuelve aquí mismo (404 si no existe o no pertenece
    al cliente) y se traduce al formato exacto que espera Sicar X. No se calcula ni se
    cobra ningún costo de envío en esta llamada (`amount` sigue siendo solo el total de
    productos, igual que en `PICKUP`) — pendiente de una futura integración.
    """
    if not authorization:
        logger.warning("Intento de creacion de orden rechazado: No se proporciono token de sesión.")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No se proporcionó el token de sesión del cliente en los headers.")

    # Verificación y refresco de la sesión del cliente
    try:
        session_data = await get_or_refresh_customer_session(authorization)
        # Obtenemos el token (ya sea el mismo si era válido, o uno nuevo si había expirado)
        valid_client_token = session_data.get("token")
    except Exception as e:
        logger.error(f"Fallo al validar o refrescar sesion del cliente: {str(e)}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No se pudo validar ni refrescar la sesión del cliente.")

    branch_id = order_payload.branchId or session_data.get("branchId") or 151456
    price_list_uuid = order_payload.priceListUuid or session_data.get("priceListUuid") or settings.SICAR_PRICE_LIST_ID
    content_id = order_payload.contentId or session_data.get("contentId") or str(uuid4())

    # Suma en Decimal (no float) para lineas duplicadas del mismo producto: sumar floats
    # directamente (p. ej. 0.1 + 0.2 en un producto a granel) puede arrastrar ruido de
    # representacion binaria (0.30000000000000004) hacia build_order_payload, el mismo tipo
    # de deriva que ya causo el rechazo "precio alterado" documentado en CLAUDE.md - ahi el
    # bug estaba del lado del precio; aqui es el mismo problema del lado de la cantidad.
    requested_quantities = {}
    for p in order_payload.products:
        if p.uuid:
            current = Decimal(str(requested_quantities.get(p.uuid, 0)))
            requested_quantities[p.uuid] = float(current + Decimal(str(p.quantity)))
    uuids = list(requested_quantities.keys())

    if not uuids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El carrito no contiene productos válidos.")

    # Idempotencia: reclamo en su propia mini-transaccion, independiente del commit
    # atomico de mas abajo (creacion de la orden). `claim`/`claim_pending` solo se usan
    # si se recibio el header - sin el, este bloque entero es un no-op.
    claim = None
    claim_pending = False
    if idempotency_key:
        claim, is_new = await claim_idempotency_key(db, client.id, idempotency_key)
        if not is_new:
            if claim.order_uuid:
                # deleted_at.is_(None): una orden borrada (DELETE /orders/{id}) no debe
                # resucitarse via un reintento de idempotencia - debe caer al camino de
                # crear una orden nueva, como si la clave no tuviera nada asociado.
                result = await db.execute(
                    select(Order).where(Order.uuid == claim.order_uuid, Order.deleted_at.is_(None))
                )
                cached_order = result.scalar_one_or_none()
                if cached_order is not None:
                    logger.info(f"POST /orders con Idempotency-Key repetida ({idempotency_key}) para cliente {client.id} - devolviendo orden ya creada {cached_order.uuid}.")
                    return OrderResponse(
                        id=cached_order.sicar_order_id,
                        serieFolio=cached_order.serie_folio,
                        date=cached_order.sicar_date.timestamp() if cached_order.sicar_date else 0,
                        status=cached_order.status,
                        orderUuid=cached_order.uuid,
                        preferenceId=cached_order.mp_preference_id,
                        amount=float(cached_order.total),
                    )
            if not is_claim_abandoned(claim):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya hay una solicitud en proceso con esta clave de idempotencia. Espera unos segundos e intenta de nuevo.")
            # Reclamo abandonado (el proceso se interrumpio antes de terminar la orden la
            # vez anterior) - se descarta y se reintenta como si la clave fuera nueva.
            await discard_claim(db, claim)
            claim, is_new = await claim_idempotency_key(db, client.id, idempotency_key)
            if not is_new:
                # Carrera con otra solicitud concurrente que reclamo la clave justo ahora.
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya hay una solicitud en proceso con esta clave de idempotencia. Espera unos segundos e intenta de nuevo.")
        claim_pending = True

    sicar_response = None
    try:
        # Pre-validación de stock/disponibilidad y datos de precio/impuestos usando el token fresco
        cart_data = await validate_cart_items(uuids, requested_quantities, valid_client_token, branch_id, price_list_uuid)

        # Productos ya sincronizados localmente (sku, nombre, unidad de venta)
        result = await db.execute(select(Product).where(Product.sicar_uuid.in_(uuids)))
        local_products = {p.sicar_uuid: p for p in result.scalars().all()}

        delivery_address_snapshot = None
        if order_payload.deliveryInfo.deliveryType == "DELIVERYMAN":
            address = await get_owned_address(db, client, order_payload.deliveryInfo.addressUuid)

            missing = [field for field, value in [
                ("street", address.street),
                ("city", address.city),
                ("county", address.county),
                ("state", address.state),
                ("zipCode", address.zip_code),
                ("extNumber", address.ext_number),
                ("neighborhood", address.neighborhood),
            ] if not value]
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"La dirección seleccionada está incompleta para entrega a domicilio (faltan: {', '.join(missing)})."
                )

            delivery_info_dict = order_payload.deliveryInfo.model_dump(exclude_none=True, exclude={"addressUuid"})
            delivery_info_dict["contactInfo"]["address"] = {
                "street": address.street,
                "extNumber": address.ext_number,
                "intNumber": address.int_number,
                "district": address.neighborhood,
                "city": address.city,
                "county": address.county,
                "state": address.state,
                "zipCode": address.zip_code,
                "country": "MEX",
                "reference": address.references,
            }
            # Foto fija del address book en la forma ClientAddressPublic (uuid/label/lat/lng
            # incluidos) - distinta del dict arriba, que es la forma que exige Sicar X. Ver
            # Order.delivery_address_snapshot y ADMIN_INTEGRATION.md ("Guia de envio").
            delivery_address_snapshot = ClientAddressPublic.model_validate(address).model_dump(mode="json", by_alias=True)
        else:
            delivery_info_dict = order_payload.deliveryInfo.model_dump(exclude_none=True)

        order_payload_dict = build_order_payload(
            cart_data=cart_data,
            local_products=local_products,
            quantities=requested_quantities,
            delivery_info=delivery_info_dict,
            branch_id=branch_id,
            price_list_uuid=price_list_uuid,
            content_id=content_id,
            wholesale_prices=order_payload.wholesalePrices,
        )
        order_payload_dict["payload"] = valid_client_token

        # Creación delegada al servicio
        sicar_response = await create_order_in_sicar(
            db=db,
            order_payload=order_payload_dict,
            client_token=valid_client_token,
            branch_id=branch_id,
            products_data=order_payload_dict["ecOrderDto"]["products"]
        )
        total_amount = float(order_payload_dict["ecOrderDto"]["total"])

        local_order = await create_local_order(
            db=db,
            client_account_id=client.id,
            order_payload_dict=order_payload_dict,
            sicar_response=sicar_response,
            local_products=local_products,
            delivery_address_snapshot=delivery_address_snapshot,
        )

        # No fatal: la orden sigue soportando tarjeta/OXXO sin la opcion de wallet si
        # Mercado Pago no responde aqui - ver payment_service.create_preference.
        preference = await payment_service.create_preference(local_order)
        if preference:
            local_order.mp_preference_id = preference.get("id")

        if claim is not None:
            claim.order_uuid = local_order.uuid
            claim_pending = False

        # Único commit: el descuento de stock (create_order_in_sicar), la fila local
        # Order (create_local_order) y el reclamo de idempotencia (si aplica) se
        # persisten juntos o se revierten juntos.
        await db.commit()
        await db.refresh(local_order)

        logger.info(f"Orden {local_order.uuid} reservada (TO_PAY) en la sucursal {branch_id} para cliente {client.email}.")

        return OrderResponse(
            id=sicar_response.get("id"),
            serieFolio=sicar_response.get("serieFolio"),
            date=sicar_response.get("date"),
            status=sicar_response.get("status") or "TO_PAY",
            orderUuid=local_order.uuid,
            preferenceId=local_order.mp_preference_id,
            amount=total_amount,
        )

    except HTTPException:
        await db.rollback()
        if claim_pending:
            await discard_claim(db, claim)
        raise
    except Exception as e:
        await db.rollback()
        if claim_pending:
            await discard_claim(db, claim)
        if sicar_response is not None:
            # La orden ya existe en Sicar X (reservo stock ahi) pero el guardado local
            # fallo despues - no hay forma de reconciliar automaticamente (Sicar X no
            # expone un endpoint para listar ordenes por cliente), asi que se deja este
            # log para que soporte pueda actuar manualmente con el folio.
            logger.critical(
                f"Orden ya creada en Sicar X (id={sicar_response.get('id')}, "
                f"folio={sicar_response.get('serieFolio')}) pero fallo el guardado local "
                f"para cliente {client.id}: {e}"
            )
        else:
            logger.error(f"Error inesperado al crear la orden: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Ocurrió un error interno al procesar la orden. Intenta más tarde.")


@router.post("/{order_id}/pay", response_model=OrderPayResponse, summary="Cobrar pedido con Mercado Pago")
@limiter.limit("10/minute")
async def pay_order(
    request: Request,
    order_id: str,
    client: CurrentClientHeaderDep,
    db: DbDep,
    submit: PaymentSubmit = Body(),
):
    """
    Cobra, via Mercado Pago, el pedido creado por `POST /orders` (`order_id` es el `id`
    devuelto por esa llamada). Recibe el `formData` tal cual lo entrega el `onSubmit` del
    Payment Brick (tarjeta u OXXO/otros metodos con submit sincrono — el metodo Wallet no
    llama a esta ruta, ver `POST /orders`). Requiere `X-Client-Token`; la orden debe
    pertenecer a la cuenta autenticada (404 si no, mismo patron que `/cancel`).

    El monto cobrado SIEMPRE es el `total` ya guardado en la orden — nunca un valor
    enviado en el body — para no confiar en un precio que pueda venir manipulado desde
    el cliente. Segun el resultado del cobro, la orden pasa a `PAID` (aprobado,
    aplicando tambien el pago interno en Sicar X), sigue en `TO_PAY` (pendiente - OXXO,
    tarjeta en revision) o pasa a `CANCELLED` (rechazado - libera el stock reservado).
    """
    local_order = await get_owned_order_by_sicar_id(db, client.id, order_id)

    if local_order.status != "TO_PAY":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Esta orden ya fue pagada o cancelada.")

    try:
        mp_payment = await payment_service.create_payment(local_order, submit.model_dump())
        local_order = await finalize_order_payment(db, local_order, mp_payment)

        logger.info(f"Pago procesado para la orden {local_order.uuid} (cliente {client.email}): mp_status={local_order.mp_status} -> status={local_order.status}.")

        return OrderPayResponse(
            orderUuid=local_order.uuid,
            status=local_order.status,
            mpPaymentId=local_order.mp_payment_id,
            mpStatus=local_order.mp_status,
            mpStatusDetail=local_order.mp_status_detail,
            ticketUrl=local_order.mp_ticket_url,
        )
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error inesperado al procesar el pago de la orden {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Ocurrió un error interno al procesar el pago. Intenta más tarde.")


@router.post("/{order_id}/cancel", response_model=OrderCancelResponse, summary="Cancelar pedido")
@limiter.limit("15/minute")
async def cancel_order(
    request: Request,
    order_id: str,
    client: CurrentClientHeaderDep,
    db: DbDep,
    cancel_payload: OrderCancel = Body(),
):
    """
    Cancela un pedido localmente de inmediato (status, stock, notificaciones) y requiere
    el header `X-Client-Token` (cuenta de cliente local): la orden debe pertenecer al
    cliente autenticado, o se responde 404 (sin revelar si la orden existe pero es de
    otra cuenta). `order_id` es el `id` devuelto por `POST /orders`.

    La cancelacion en Sicar X ya NO ocurre en esta llamada: se encola en
    `sicar_sync_outbox` y la procesa `app/worker/sicar_sync_worker.py` de forma
    asincrona, con reintentos - asi un Sicar X caido nunca bloquea que un cliente
    cancele su pedido. `sicarTimestamp` en la respuesta es ahora el momento en que la
    cancelacion se acepto localmente, no una confirmacion de Sicar X (ver
    FRONTEND_INTEGRATION.md).

    Si la orden ya tiene un pago de Mercado Pago asociado, se limpia ese lado
    (reembolso si ya estaba aprobado, o cancelacion si seguia pendiente/en proceso) -
    eso si sigue siendo sincrono, Mercado Pago es un sistema aparte con su propio
    esquema de reintentos/webhooks. Si esta llamada pierde la carrera para cancelar la
    orden (otra solicitud concurrente ya la marco CANCELLED primero) DESPUES de haber
    resuelto el pago con Mercado Pago, el nuevo `mp_status` se conserva de todos modos
    en vez de perderse en un rollback - ver el manejo de `mp_resolved_here` mas abajo.

    El stock se restaura a partir de `local_order.items` (lo realmente reservado al
    crear la orden), no de `cancel_payload.products` — ese campo se sigue aceptando por
    compatibilidad con el frontend pero ya no se usa para nada, ver FRONTEND_INTEGRATION.md.
    """
    cash_register_uuid = cancel_payload.cashRegisterUuid

    if not cash_register_uuid:
        logger.warning("Intento de cancelacion fallido: Falta la caja registradora.")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Falta la caja registradora.")

    local_order = await get_owned_order_by_sicar_id(db, client.id, order_id)

    if local_order.status == "CANCELLED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Esta orden ya fue cancelada.")

    # True si esta solicitud ya resolvio (reembolso/cancelacion) el pago de Mercado Pago
    # - un hecho externo, no reversible - antes de intentar la cancelacion local. Si
    # `prepare_local_cancellation` termina rechazando esta solicitud (409, otra
    # solicitud concurrente ya cancelo la orden primero), el except de abajo usa esta
    # bandera para NO revertir ese cambio de mp_status: el pago ya se resolvio de
    # verdad, perderlo en un rollback dejaria la contabilidad local desincronizada de
    # lo que realmente paso en Mercado Pago.
    mp_resolved_here = False

    try:
        if local_order.mp_payment_id:
            if local_order.mp_status == "approved":
                await payment_service.refund_payment(local_order.mp_payment_id)
                local_order.mp_status = "refunded"
                mp_resolved_here = True
            elif local_order.mp_status in ("pending", "in_process"):
                await payment_service.cancel_payment(local_order.mp_payment_id)
                local_order.mp_status = "cancelled"
                mp_resolved_here = True

        local_order = await prepare_local_cancellation(db, local_order, cash_register_uuid=cash_register_uuid)
        cancel_timestamp = datetime.now(timezone.utc).timestamp() * 1000
        await db.commit()

        logger.info(f"Pedido {order_id} cancelado localmente por cliente {client.email}. Stock restaurado, sincronizacion con Sicar X encolada.")

        try:
            await notify_order_cancelled(local_order)
        except Exception as e:
            logger.error(f"Fallo inesperado (no manejado por notify_order_cancelled) notificando la orden {local_order.uuid}: {type(e).__name__}: {e!r}")

        return OrderCancelResponse(
            documentUuid=order_id,
            sicarTimestamp=cancel_timestamp,
            message="Pedido cancelado exitosamente.",
            status="CANCELLED"
        )

    except HTTPException:
        if mp_resolved_here:
            await db.commit()
        else:
            await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error inesperado al cancelar el pedido {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Ocurrió un error interno al cancelar el pedido. Intenta más tarde.")


@router.delete("/{order_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar pedido reservado sin pagar")
@limiter.limit("15/minute")
async def delete_order(
    request: Request,
    order_id: str,
    client: CurrentClientHeaderDep,
    db: DbDep,
):
    """
    Descarta una orden que quedo reservada en Sicar X (`TO_PAY`) pero nunca se pago. Ya
    NO borra la fila de `orders` por completo — la marca con `deleted_at` (soft-delete)
    en su lugar, para que `app/worker/sicar_sync_worker.py` siga teniendo la fila
    disponible y pueda avisarle a Sicar X de la cancelacion de forma asincrona (mismo
    mecanismo que `/cancel`, ver `sicar_sync_outbox`). De cara al cliente el contrato no
    cambia: `deleted_at` se filtra en todas las consultas de historial
    (`list_client_orders`, `get_client_order`, `get_owned_order_by_sicar_id`,
    `get_order_by_uuid`), asi que la orden desaparece de `GET /auth/me/orders` de
    inmediato, exactamente como documenta FRONTEND_INTEGRATION.md - solo que ahora la
    fila persiste (oculta) en vez de perderse, lo cual es estrictamente mejor: nada que
    reconciliar se pierde si la sincronizacion con Sicar X llegara a fallar.

    Requiere `X-Client-Token`; la orden debe pertenecer al cliente autenticado (404 si
    no, mismo patron que `/cancel` — y sigue siendo 404, no 409, en una segunda llamada
    sobre la misma orden ya borrada, porque el filtro de arriba hace que ya no se
    encuentre: la ruta se mantiene idempotente). Solo aplica a ordenes en `TO_PAY` - 409
    si ya esta `PAID` o `CANCELLED` (esas si se conservan visibles y no se pueden borrar
    por aqui).

    Antes de encolar la cancelacion: cancela cualquier pago de Mercado Pago que haya
    quedado pendiente/en proceso (OXXO sin pagar, tarjeta en revision - nunca
    `approved`, porque eso ya habria puesto la orden en `PAID` y quedo excluido arriba).
    Si esta llamada pierde la carrera para cancelar la orden (otra solicitud
    concurrente ya la marco CANCELLED primero) DESPUES de haber cancelado el pago con
    Mercado Pago, el nuevo `mp_status` se conserva de todos modos - ver `mp_resolved_here`.
    """
    local_order = await get_owned_order_by_sicar_id(db, client.id, order_id)

    if local_order.status != "TO_PAY":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Solo se pueden eliminar ordenes reservadas que aun no han sido pagadas ni canceladas."
        )

    # Ver el comentario equivalente en cancel_order - misma razon.
    mp_resolved_here = False

    try:
        if local_order.mp_payment_id and local_order.mp_status in ("pending", "in_process"):
            await payment_service.cancel_payment(local_order.mp_payment_id)
            local_order.mp_status = "cancelled"
            mp_resolved_here = True

        local_order = await prepare_local_cancellation(db, local_order, require_status="TO_PAY")
        local_order.deleted_at = datetime.now(timezone.utc)
        await db.commit()

        logger.info(f"Orden {order_id} (reservada, nunca pagada) eliminada (soft-delete) por cliente {client.email}. Sincronizacion con Sicar X encolada.")

        try:
            await notify_order_cancelled(local_order)
        except Exception as e:
            logger.error(f"Fallo inesperado (no manejado por notify_order_cancelled) notificando la orden {local_order.uuid}: {type(e).__name__}: {e!r}")

    except HTTPException:
        if mp_resolved_here:
            await db.commit()
        else:
            await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error inesperado al eliminar el pedido {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Ocurrió un error interno al eliminar el pedido. Intenta más tarde.")
