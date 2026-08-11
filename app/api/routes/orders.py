import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Body, Header, Request, status
from sqlalchemy import select
from app.core.database import DbDep
from app.core.security import validate_api_key, OptionalClientHeaderDep
from app.core.rate_limit import limiter
from app.models.order import Order
from app.services.order_service import validate_cart_items, build_order_payload, compute_subtotal, _to_decimal
from app.services.product_stock_service import apply_reserved_deltas
from app.services.order_history_service import create_local_order, get_order_for_action, finalize_order_payment, prepare_local_cancellation
from app.services.order_cancellation_notification_service import notify_order_cancelled
from app.services.order_idempotency_service import claim_idempotency_key, is_claim_abandoned, discard_claim
from app.services.address_service import get_owned_address
from app.services import payment_service
from app.services import coupon_service
from app.schemas.orders import OrderCancelResponse, OrderCreate, OrderCancel, OrderResponse, PaymentSubmit, OrderPayResponse, OrderPublic
from app.schemas.client import ClientAddressPublic
from app.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/orders", tags=["Orders Creation and Cancellation"], dependencies=[Depends(validate_api_key)])

@router.post("", response_model=OrderResponse, summary="Crear pedido")
@limiter.limit("10/minute")
async def create_order(
    request: Request,
    client: OptionalClientHeaderDep,
    db: DbDep,
    order_payload: OrderCreate = Body(),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", description="Opcional. Si se repite la misma clave (p. ej. un reintento de red del mismo submit de checkout), se devuelve la orden ya creada en vez de crear una duplicada."),
):
    """
    El frontend solo envía `products: [{uuid, quantity}]` y `deliveryInfo`; precio, sku,
    descripción, unidad y totales se calculan aquí desde el catálogo local, sin ninguna
    llamada en vivo a Sicar X.

    `Idempotency-Key` (opcional): reenviar la misma clave en un reintento devuelve la
    orden ya creada en vez de duplicarla.

    `X-Client-Token` es OPCIONAL: si viene, la orden queda vinculada a esa cuenta (camino de
    cuenta, sin cambios). Si NO viene, es un checkout de invitado: `deliveryInfo.contactInfo.email`
    es obligatorio (es la única identidad del invitado), `deliveryInfo.addressUuid` está
    prohibido (no hay address book sin cuenta - usar `deliveryInfo.address` en su lugar para
    DELIVERYMAN) y `couponCode` está prohibido (los cupones requieren cuenta). El `id`/`orderUuid`
    devueltos por esta llamada duplican como prueba de pertenencia del invitado para
    `POST /orders/{id}/pay`, `/cancel`, `DELETE` y `GET /orders/guest/{id}` - guárdalos.

    Si más adelante el invitado crea una cuenta con el mismo correo y la verifica (clic en el
    enlace de verificación, o login con Google con correo ya verificado), este pedido se
    vincula automáticamente a esa cuenta - ver `client_service.link_guest_orders_by_email`.

    Esta llamada SOLO reserva el pedido localmente (`TO_PAY`) y crea la preferencia de
    Mercado Pago — todavía no cobra nada. Devuelve `preferenceId`/`amount` para el
    Payment Brick y `orderUuid`/`id` para `POST /orders/{id}/pay` (tarjeta/OXXO); si el
    comprador paga con Wallet, la confirmación llega vía `POST /payments/webhook`.

    `deliveryInfo.deliveryType`: `PICKUP` o `DELIVERYMAN` (requiere `addressUuid` de una
    dirección ya guardada del cliente, o `address` inline si es invitado). No se calcula ni
    cobra costo de envío en esta llamada.
    """
    branch_id = order_payload.branchId or 151456
    price_list_uuid = order_payload.priceListUuid or settings.SICAR_PRICE_LIST_ID
    content_id = order_payload.contentId or str(uuid4())

    # Suma en Decimal, no float: sumar floats directamente (p. ej. 0.1 + 0.2 en un producto
    # a granel) puede arrastrar ruido de representacion binaria hacia build_order_payload.
    requested_quantities = {}
    for p in order_payload.products:
        if p.uuid:
            current = Decimal(str(requested_quantities.get(p.uuid, 0)))
            requested_quantities[p.uuid] = float(current + Decimal(str(p.quantity)))
    uuids = list(requested_quantities.keys())

    if not uuids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El carrito no contiene productos válidos.")

    # Checkout de invitado (sin X-Client-Token): el correo de contactInfo es la unica
    # identidad disponible, addressUuid/couponCode requieren una cuenta real. El camino de
    # cuenta rechaza en cambio una direccion inline (debe usar su address book).
    guest_email: str | None = None
    if client is None:
        contact_email = order_payload.deliveryInfo.contactInfo.email
        if not contact_email:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El correo es obligatorio para completar un pedido sin cuenta.")
        guest_email = contact_email.lower()
        if order_payload.deliveryInfo.addressUuid:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Un pedido sin cuenta no puede usar una dirección guardada; envía 'address' en su lugar.")
        if order_payload.couponCode:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Los cupones de descuento requieren una cuenta. Inicia sesión o crea una cuenta para usar un cupón.")
    elif order_payload.deliveryInfo.address:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Una cuenta autenticada debe usar 'addressUuid' de una dirección guardada, no una dirección inline.")

    # Reclamo de idempotencia en su propia mini-transaccion, independiente del commit de
    # mas abajo. Sin el header, este bloque entero es un no-op.
    claim = None
    claim_pending = False
    if idempotency_key:
        claim, is_new = await claim_idempotency_key(db, client.id if client else None, idempotency_key, guest_email=guest_email)
        if not is_new:
            if claim.order_uuid:
                # deleted_at.is_(None): una orden borrada no debe resucitarse via un
                # reintento de idempotencia - debe crearse una nueva.
                result = await db.execute(
                    select(Order).where(Order.uuid == claim.order_uuid, Order.deleted_at.is_(None))
                )
                cached_order = result.scalar_one_or_none()
                if cached_order is not None:
                    logger.info(f"POST /orders con Idempotency-Key repetida ({idempotency_key}) para {'cliente ' + str(client.id) if client else 'invitado ' + str(guest_email)} - devolviendo orden ya creada {cached_order.uuid}.")
                    return OrderResponse(
                        id=cached_order.sicar_order_id,
                        serieFolio=cached_order.serie_folio,
                        date=None,
                        status=cached_order.status,
                        orderUuid=cached_order.uuid,
                        preferenceId=cached_order.mp_preference_id,
                        amount=float(cached_order.total),
                        discountAmount=float(cached_order.discount_amount) if cached_order.discount_amount is not None else 0.0,
                    )
            if not is_claim_abandoned(claim):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya hay una solicitud en proceso con esta clave de idempotencia. Espera unos segundos e intenta de nuevo.")
            # Reclamo abandonado (el proceso se interrumpio antes de terminar la orden la
            # vez anterior) - se descarta y se reintenta como si la clave fuera nueva.
            await discard_claim(db, claim)
            claim, is_new = await claim_idempotency_key(db, client.id if client else None, idempotency_key, guest_email=guest_email)
            if not is_new:
                # Carrera con otra solicitud concurrente que reclamo la clave justo ahora.
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya hay una solicitud en proceso con esta clave de idempotencia. Espera unos segundos e intenta de nuevo.")
        claim_pending = True

    try:
        local_products = await validate_cart_items(db, uuids, requested_quantities)

        # Cupon: se re-valida y bloquea AQUI, nunca se confia el descuento que GET /cart ya
        # mostro como preview - mismo principio que precio/stock, nunca confiar en el cliente.
        # Nunca alcanzable para invitado (couponCode ya se rechazo mas arriba con 400).
        discount_amount = Decimal("0")
        coupon = None
        if order_payload.couponCode:
            raw_subtotal = compute_subtotal(local_products, requested_quantities)
            coupon = await coupon_service.lock_and_validate_coupon(db, order_payload.couponCode, client.id, raw_subtotal)
            scoped_subtotal = await coupon_service.compute_scoped_subtotal(db, coupon, local_products, requested_quantities)
            discount_amount = coupon_service.compute_discount_amount(coupon, scoped_subtotal)

        delivery_address_snapshot = None
        if order_payload.deliveryInfo.deliveryType == "DELIVERYMAN":
            if client is not None:
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

                delivery_info_dict = order_payload.deliveryInfo.model_dump(exclude_none=True, exclude={"addressUuid", "address"})
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
                # Foto fija del address book en la forma ClientAddressPublic, distinta del
                # dict de arriba (el formato que exige Sicar X) - ver Order.delivery_address_snapshot.
                delivery_address_snapshot = ClientAddressPublic.model_validate(address).model_dump(mode="json", by_alias=True)
            else:
                # Invitado: la direccion viene inline en el body (ya validada como completa
                # a nivel de schema, ver GuestDeliveryAddress), no de un address book.
                guest_address = order_payload.deliveryInfo.address

                delivery_info_dict = order_payload.deliveryInfo.model_dump(exclude_none=True, exclude={"addressUuid", "address"})
                delivery_info_dict["contactInfo"]["address"] = {
                    "street": guest_address.street,
                    "extNumber": guest_address.ext_number,
                    "intNumber": guest_address.int_number,
                    "district": guest_address.neighborhood,
                    "city": guest_address.city,
                    "county": guest_address.county,
                    "state": guest_address.state,
                    "zipCode": guest_address.zip_code,
                    "country": "MEX",
                    "reference": guest_address.references,
                }
                # Mismo shape que la foto fija del camino de cuenta (ClientAddressPublic),
                # pero sin fila real de address book - uuid queda None a proposito.
                delivery_address_snapshot = ClientAddressPublic(
                    uuid=None,
                    street=guest_address.street,
                    ext_number=guest_address.ext_number,
                    int_number=guest_address.int_number,
                    neighborhood=guest_address.neighborhood,
                    city=guest_address.city,
                    county=guest_address.county,
                    state=guest_address.state,
                    zip_code=guest_address.zip_code,
                    references=guest_address.references,
                    latitude=guest_address.latitude,
                    longitude=guest_address.longitude,
                ).model_dump(mode="json", by_alias=True)
        else:
            delivery_info_dict = order_payload.deliveryInfo.model_dump(exclude_none=True)

        order_payload_dict = build_order_payload(
            local_products=local_products,
            quantities=requested_quantities,
            delivery_info=delivery_info_dict,
            branch_id=branch_id,
            price_list_uuid=price_list_uuid,
            content_id=content_id,
            wholesale_prices=order_payload.wholesalePrices,
            discount_amount=discount_amount,
        )
        total_amount = float(order_payload_dict["ecOrderDto"]["total"])

        # Reserva local de inventario en un solo lote (Product.reserved, no Product.stock -
        # ver CLAUDE.md, "stock sync y ordenes"). Sicar X no se entera de esta orden hasta
        # que un admin la acepte (admin_service.accept_order); stock real se descuenta ahi.
        deltas = [(item.get("uuid"), _to_decimal(item.get("quantity", 0))) for item in order_payload_dict["ecOrderDto"]["products"]]
        await apply_reserved_deltas(db, deltas)

        local_order = await create_local_order(
            db=db,
            client_account_id=client.id if client else None,
            order_payload_dict=order_payload_dict,
            local_products=local_products,
            delivery_address_snapshot=delivery_address_snapshot,
            coupon_id=coupon.id if coupon else None,
            coupon_code=coupon.code if coupon else None,
            guest_email=guest_email,
        )

        # Debe ir DESPUES de create_local_order (necesita local_order.id) y DENTRO de la
        # misma transaccion/lock que coupon_service.lock_and_validate_coupon de arriba, para
        # que el conteo de topes de uso y este insert sean atomicos entre si.
        if coupon is not None:
            await coupon_service.create_redemption(db, coupon, client.id, local_order.id)

        # No fatal: la orden sigue soportando tarjeta/OXXO sin la opcion de wallet si
        # Mercado Pago no responde aqui - ver payment_service.create_preference.
        preference = await payment_service.create_preference(local_order)
        if preference:
            local_order.mp_preference_id = preference.get("id")

        if claim is not None:
            claim.order_uuid = local_order.uuid
            claim_pending = False

        # Único commit: stock, Order y el reclamo de idempotencia se persisten o revierten juntos.
        await db.commit()
        await db.refresh(local_order)

        logger.info(f"Orden {local_order.uuid} reservada (TO_PAY) en la sucursal {branch_id} para {'cliente ' + client.email if client else 'invitado ' + str(guest_email)}.")

        return OrderResponse(
            id=local_order.sicar_order_id,
            serieFolio=None,
            date=None,
            status="TO_PAY",
            orderUuid=local_order.uuid,
            preferenceId=local_order.mp_preference_id,
            amount=total_amount,
            discountAmount=float(discount_amount),
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
        logger.error(f"Error inesperado al crear la orden: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Ocurrió un error interno al procesar la orden. Intenta más tarde.")


@router.post("/{order_id}/pay", response_model=OrderPayResponse, summary="Cobrar pedido con Mercado Pago")
@limiter.limit("10/minute")
async def pay_order(
    request: Request,
    order_id: str,
    client: OptionalClientHeaderDep,
    db: DbDep,
    submit: PaymentSubmit = Body(),
):
    """
    Cobra, via Mercado Pago, el pedido creado por `POST /orders`. Recibe el `formData`
    del `onSubmit` del Payment Brick (tarjeta/OXXO — Wallet no llama a esta ruta).

    Cuenta: requiere `X-Client-Token`, 404 si la orden no pertenece a la cuenta
    autenticada. Invitado (sin `X-Client-Token`): `order_id` puede ser el `id` o el
    `orderUuid` devueltos por `POST /orders`, 404 si la orden ya no está sin cuenta (p. ej.
    ya fue vinculada a una cuenta, ver `link_guest_orders_by_email`) — ver `get_order_for_action`.

    El monto cobrado SIEMPRE es el `total` ya guardado en la orden, nunca el del body.
    Segun el resultado, la orden pasa a `PAID`, sigue `TO_PAY` (OXXO/tarjeta pendiente)
    o pasa a `CANCELLED` (rechazado, libera el stock reservado).
    """
    local_order = await get_order_for_action(db, client, order_id)

    if local_order.status != "TO_PAY":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Esta orden ya fue pagada o cancelada.")

    try:
        mp_payment = await payment_service.create_payment(local_order, submit.model_dump())
        local_order = await finalize_order_payment(db, local_order, mp_payment)

        logger.info(f"Pago procesado para la orden {local_order.uuid} ({'cliente ' + client.email if client else 'invitado ' + str(local_order.guest_email)}): mp_status={local_order.mp_status} -> status={local_order.status}.")

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
    client: OptionalClientHeaderDep,
    db: DbDep,
    cancel_payload: OrderCancel = Body(),
):
    """
    Cancela un pedido localmente de inmediato (status, stock, notificaciones).

    Cuenta: requiere `X-Client-Token`, 404 si la orden no pertenece al cliente
    autenticado. Invitado (sin `X-Client-Token`): `order_id` puede ser el `id` o el
    `orderUuid` devueltos por `POST /orders` - ver `get_order_for_action`.

    La cancelacion en Sicar X ya NO ocurre aqui: se encola en `sicar_sync_outbox` y la
    procesa el worker de forma asincrona/con reintentos, asi Sicar X caido nunca bloquea
    la cancelacion. `sicarTimestamp` es el momento local de aceptacion, no de Sicar X.

    409 si la orden ya esta CANCELLED, o si `dispatch_status` es "DISPATCHED" (ya
    enviada) — "COMPLETE" no cuenta, tambien es el estado terminal de pedidos PICKUP.

    Si hay un pago de Mercado Pago asociado, se reembolsa/cancela de forma sincrona
    antes de la cancelacion local (`mp_resolved_here` conserva ese resultado aunque
    esta solicitud pierda la carrera de cancelacion local).

    El stock se restaura desde `local_order.items`, no `cancel_payload.products` (ese
    campo ya no se usa, ver FRONTEND_INTEGRATION.md).
    """
    cash_register_uuid = cancel_payload.cashRegisterUuid

    if not cash_register_uuid:
        logger.warning("Intento de cancelacion fallido: Falta la caja registradora.")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Falta la caja registradora.")

    local_order = await get_order_for_action(db, client, order_id)

    if local_order.status == "CANCELLED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Esta orden ya fue cancelada.")

    # COMPLETE se excluye a proposito: tambien es el estado terminal de pedidos PICKUP
    # (listos para recoger en tienda), que nunca se "enviaron" a ningun lado.
    if local_order.dispatch_status == "DISPATCHED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Esta orden ya fue enviada y no puede cancelarse.")

    # True si esta solicitud ya resolvio (reembolso/cancelacion) el pago de Mercado Pago
    # - un hecho externo no reversible. Si prepare_local_cancellation rechaza despues
    # (409, otra solicitud ya cancelo la orden), el except de abajo usa esta bandera
    # para NO revertir ese cambio de mp_status en el rollback.
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

        logger.info(f"Pedido {order_id} cancelado localmente por {'cliente ' + client.email if client else 'invitado ' + str(local_order.guest_email)}. Stock restaurado, sincronizacion con Sicar X encolada.")

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
    client: OptionalClientHeaderDep,
    db: DbDep,
):
    """
    Descarta una orden reservada (`TO_PAY`) que nunca se pago. Ya NO borra la fila —
    la marca con `deleted_at` (soft-delete) para que el worker siga pudiendo avisarle a
    Sicar X de la cancelacion de forma asincrona (mismo mecanismo que `/cancel`). De
    cara al cliente el contrato no cambia: `deleted_at` se filtra en toda consulta de
    historial, asi que la orden desaparece de `GET /auth/me/orders` de inmediato.

    Cuenta: requiere `X-Client-Token`, 404 si la orden no pertenece al cliente
    autenticado. Invitado (sin `X-Client-Token`): `order_id` puede ser el `id` o el
    `orderUuid` devueltos por `POST /orders` - ver `get_order_for_action`. En ambos casos
    es idempotente sobre la misma orden ya borrada. 409 si ya esta `PAID` o `CANCELLED`,
    o si ya tiene una referencia de pago OXXO generada (`mp_ticket_url`) — ver el
    comentario junto a ese chequeo.

    Antes de encolar la cancelacion, cancela cualquier pago de Mercado Pago pendiente/en
    proceso (nunca `approved`, eso ya la habria puesto en `PAID`) - ver `mp_resolved_here`
    para el manejo de la carrera con una cancelacion concurrente.
    """
    local_order = await get_order_for_action(db, client, order_id)

    if local_order.status != "TO_PAY":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Solo se pueden eliminar ordenes reservadas que aun no han sido pagadas ni canceladas."
        )

    if local_order.mp_ticket_url:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Esta orden ya tiene una referencia de pago (OXXO) generada y no puede descartarse automáticamente. Usa /cancel si el cliente desea cancelarla explícitamente.",
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

        logger.info(f"Orden {order_id} (reservada, nunca pagada) eliminada (soft-delete) por {'cliente ' + client.email if client else 'invitado ' + str(local_order.guest_email)}. Sincronizacion con Sicar X encolada.")

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


@router.get("/guest/{order_id}", response_model=OrderPublic, summary="Consultar estado de un pedido de invitado")
@limiter.limit("20/minute")
async def get_guest_order(
    request: Request,
    order_id: str,
    db: DbDep,
):
    """
    Consulta el estado de un pedido creado sin cuenta (`POST /orders` sin `X-Client-Token`).
    Sin sesión: la prueba de pertenencia es poseer `order_id` (el `id` o `orderUuid`
    devueltos al hacer checkout) - ver `order_history_service.get_order_for_action`.

    404 si la orden no existe, o si ya fue vinculada a una cuenta (ver
    `link_guest_orders_by_email`) - en ese caso el cliente ya debe usar
    `GET /auth/me/orders/{orderUuid}` en su lugar, esto no es un error.
    """
    return await get_order_for_action(db, None, order_id)
