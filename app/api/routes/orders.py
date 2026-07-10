import logging
from fastapi import APIRouter, Depends, HTTPException, Body, Header
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import validate_api_key
from app.services.order_service import validate_cart_items, create_order_in_sicar, pay_order_in_sicar
from app.services.cancel_service import process_order_cancellation
from app.services.session_service import get_or_refresh_customer_session
from app.schemas.orders import OrderCancelResponse, OrderCreate, OrderCancel, OrderResponse
from app.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

@router.post("/orders", response_model=OrderResponse)
async def create_order(
    order_payload: OrderCreate = Body(...), 
    authorization: str = Header(None, alias="Authorization", description="Token de sesión del cliente web"),
    db: AsyncSession = Depends(get_db),
    _ : str = Depends(validate_api_key)
):
    if not authorization:
        logger.warning("Intento de creacion de orden rechazado: No se proporciono token de sesión.")
        raise HTTPException(status_code=401, detail="No se proporcionó el token de sesión del cliente en los headers.")

    # Verificación y refresco de la sesión del cliente
    try:
        session_data = await get_or_refresh_customer_session(authorization)
        # Obtenemos el token (ya sea el mismo si era válido, o uno nuevo si había expirado)
        valid_client_token = session_data.get("token")
    except Exception as e:
        logger.error(f"Fallo al validar o refrescar sesion del cliente: {str(e)}")
        raise HTTPException(status_code=401, detail="No se pudo validar ni refrescar la sesión del cliente.")

    branch_id = order_payload.branchId
    price_list_uuid = order_payload.priceListUuid
    products_data = order_payload.ecOrderDto.products
    uuids = [p.uuid for p in products_data if p.uuid]
    requested_quantities = {}
    for p in products_data:
        if p.uuid:
            try:
                requested_quantities[p.uuid] = requested_quantities.get(p.uuid, 0) + float(p.quantity)
            except (TypeError, ValueError):
                requested_quantities[p.uuid] = requested_quantities.get(p.uuid, 0)

    try:
        # Pre-validación usando el token fresco
        if uuids:
            await validate_cart_items(uuids, requested_quantities, valid_client_token, branch_id, price_list_uuid)

        # Sincronización del Payload
        order_payload_dict = order_payload.model_dump()
        order_payload_dict["payload"] = valid_client_token

        # Creación delegada al servicio
        sicar_response = await create_order_in_sicar(
            db=db,
            order_payload=order_payload_dict,
            client_token=valid_client_token,
            branch_id=branch_id,
            products_data=order_payload_dict["ecOrderDto"]["products"]
        )
        order_id = sicar_response.get("id")
        total_amount = float(order_payload_dict["ecOrderDto"]["total"])

        payment_response = await pay_order_in_sicar(
            order_id=order_id,
            total_amount=total_amount,
            cash_register_uuid=settings.CASH_REGISTER_UUID,
            branch_id=branch_id
        )

        logger.info(f"Orden creada y pagada exitosamente en la sucursal {branch_id}.")

        return payment_response

    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error inesperado al crear la orden: {e}")
        raise HTTPException(status_code=400, detail="No se pudo procesar la orden. Verifica los datos e intenta nuevamente.")


@router.post("/cancel", response_model=OrderCancelResponse)
async def cancel_order(
    cancel_payload: OrderCancel = Body(...),
    db: AsyncSession = Depends(get_db),
    _ : str = Depends(validate_api_key)
):
    document_uuid = cancel_payload.uuid
    cash_register_uuid = cancel_payload.cashRegisterUuid
    products_to_restore = cancel_payload.products

    if not document_uuid or not cash_register_uuid:
        logger.warning("Intento de cancelacion fallido: Faltan uuid del documento o caja registradora.")
        raise HTTPException(status_code=400, detail="Faltan el uuid del documento o la caja registradora.")

    try:
        cancel_timestamp = await process_order_cancellation(
            db, 
            document_uuid, 
            cash_register_uuid, 
            products_to_restore
        )

        logger.info(f"Pedido {document_uuid} cancelado exitosamente. Stock restaurado.")
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
        raise HTTPException(status_code=400, detail="No se pudo cancelar el pedido. Intenta nuevamente.")