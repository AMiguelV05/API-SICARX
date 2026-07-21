import logging
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Body, Header, status
from sqlalchemy import select
from app.core.database import DbDep
from app.core.security import validate_api_key, CurrentClientHeaderDep
from app.models.product import Product
from app.services.order_service import validate_cart_items, build_order_payload, create_order_in_sicar
from app.services.cancel_service import process_order_cancellation
from app.services.session_service import get_or_refresh_customer_session
from app.services.order_history_service import create_local_order, get_owned_order_by_sicar_id, finalize_order_payment
from app.services import payment_service
from app.schemas.orders import OrderCancelResponse, OrderCreate, OrderCancel, OrderResponse, PaymentSubmit, OrderPayResponse
from app.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/orders", tags=["Orders Creation and Cancellation"], dependencies=[Depends(validate_api_key)])

@router.post("", response_model=OrderResponse, summary="Crear pedido")
async def create_order(
    client: CurrentClientHeaderDep,
    db: DbDep,
    order_payload: OrderCreate = Body(),
    authorization: str = Header(None, alias="Authorization", description="Token de sesión del cliente web"),
):
    """
    Contrato semiautomático: el frontend solo envía `products: [{uuid, quantity}]` y
    `deliveryInfo`; precios, impuestos, sku, descripción, unidad y totales se calculan
    en el backend a partir de Sicar X y del catálogo local (`order_service.build_order_payload`).

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

    requested_quantities = {}
    for p in order_payload.products:
        if p.uuid:
            requested_quantities[p.uuid] = requested_quantities.get(p.uuid, 0) + float(p.quantity)
    uuids = list(requested_quantities.keys())

    if not uuids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El carrito no contiene productos válidos.")

    try:
        # Pre-validación de stock/disponibilidad y datos de precio/impuestos usando el token fresco
        cart_data = await validate_cart_items(uuids, requested_quantities, valid_client_token, branch_id, price_list_uuid)

        # Productos ya sincronizados localmente (sku, nombre, unidad de venta)
        result = await db.execute(select(Product).where(Product.sicar_uuid.in_(uuids)))
        local_products = {p.sicar_uuid: p for p in result.scalars().all()}

        order_payload_dict = build_order_payload(
            cart_data=cart_data,
            local_products=local_products,
            quantities=requested_quantities,
            delivery_info=order_payload.deliveryInfo.model_dump(exclude_none=True),
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
        )

        # No fatal: la orden sigue soportando tarjeta/OXXO sin la opcion de wallet si
        # Mercado Pago no responde aqui - ver payment_service.create_preference.
        preference = await payment_service.create_preference(local_order)

        logger.info(f"Orden {local_order.uuid} reservada (TO_PAY) en la sucursal {branch_id} para cliente {client.email}.")

        return OrderResponse(
            id=sicar_response.get("id"),
            serieFolio=sicar_response.get("serieFolio"),
            date=sicar_response.get("date"),
            status=sicar_response.get("status") or "TO_PAY",
            orderUuid=local_order.uuid,
            preferenceId=(preference or {}).get("id"),
            amount=total_amount,
        )

    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error inesperado al crear la orden: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Ocurrió un error interno al procesar la orden. Intenta más tarde.")


@router.post("/{order_id}/pay", response_model=OrderPayResponse, summary="Cobrar pedido con Mercado Pago")
async def pay_order(
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
async def cancel_order(
    order_id: str,
    client: CurrentClientHeaderDep,
    db: DbDep,
    cancel_payload: OrderCancel = Body(),
):
    """
    Cancela un pedido en Sicar X y restaura el stock local. Usa el token admin/B2B
    internamente para hablar con Sicar X, pero ahora también requiere el header
    `X-Client-Token` (cuenta de cliente local): la orden debe pertenecer al cliente
    autenticado, o se responde 404 (sin revelar si la orden existe pero es de otra
    cuenta). `order_id` es el `id` devuelto por `POST /orders` (no un UUID real de Sicar);
    el backend lo resuelve internamente al identificador que Sicar X espera antes de
    cancelar.

    Si la orden ya tiene un pago de Mercado Pago asociado, primero se limpia ese lado
    (reembolso si ya estaba aprobado, o cancelacion si seguia pendiente/en proceso) antes
    de tocar Sicar X — el dinero se resuelve antes que la contabilidad interna, mismo
    orden que ya sigue `pay_order_in_sicar` en el flujo de cobro.
    """
    document_uuid = order_id
    cash_register_uuid = cancel_payload.cashRegisterUuid
    products_to_restore = cancel_payload.products

    if not cash_register_uuid:
        logger.warning("Intento de cancelacion fallido: Falta la caja registradora.")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Falta la caja registradora.")

    local_order = await get_owned_order_by_sicar_id(db, client.id, document_uuid)

    try:
        if local_order.mp_payment_id:
            if local_order.mp_status == "approved":
                await payment_service.refund_payment(local_order.mp_payment_id)
            elif local_order.mp_status in ("pending", "in_process"):
                await payment_service.cancel_payment(local_order.mp_payment_id)

        cancel_timestamp = await process_order_cancellation(
            db,
            document_uuid,
            cash_register_uuid,
            products_to_restore
        )

        local_order.status = "CANCELLED"
        await db.commit()

        logger.info(f"Pedido {document_uuid} cancelado exitosamente por cliente {client.email}. Stock restaurado.")
        return OrderCancelResponse(
            documentUuid=document_uuid,
            sicarTimestamp=cancel_timestamp,
            message="Pedido cancelado exitosamente.",
            status="CANCELLED"
        )

    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error inesperado al cancelar el pedido {document_uuid}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Ocurrió un error interno al cancelar el pedido. Intenta más tarde.")
