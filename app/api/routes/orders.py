from fastapi import APIRouter, Depends, HTTPException, Body, Header
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import validate_api_key
from app.services.order_service import validate_cart_items, create_order_in_sicar
from app.services.cancel_service import process_order_cancellation
from app.schemas.orders import OrderCancelResponse, OrderCreate, OrderCancel, OrderResponse

router = APIRouter(tags=["Orders"])

@router.post("/orders",
             response_model=OrderResponse)

async def create_order(
    order_payload: OrderCreate = Body(...), 
    authorization: str = Header(..., description="Token de sesión del cliente web"),
    db: AsyncSession = Depends(get_db),
    _ : str = Depends(validate_api_key) # API Key obligatoria
):
    if not authorization:
        raise HTTPException(status_code=401, detail="No se proporcionó el token de sesión del cliente en los headers.")

    branch_id = order_payload.branchId
    price_list_uuid = order_payload.priceListUuid
    products_data = order_payload.ecOrderDto.products
    uuids = [p.uuid for p in products_data if p.uuid]

    try:
        # Validación usando el token del cliente
        if uuids:
            await validate_cart_items(uuids, authorization, branch_id, price_list_uuid)

        # Creación delegada al servicio
        sicar_response = await create_order_in_sicar(db, order_payload, authorization, branch_id, products_data)
        return sicar_response
        
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/cancel",
             response_model=OrderCancelResponse)
async def cancel_order(
    cancel_payload: OrderCancel = Body(...),
    db: AsyncSession = Depends(get_db),
    _ : str = Depends(validate_api_key) # API Key obligatoria
):
    document_uuid = cancel_payload.uuid
    cash_register_uuid = cancel_payload.cashRegisterUuid
    products_to_restore = cancel_payload.products

    if not document_uuid or not cash_register_uuid:
        raise HTTPException(status_code=400, detail="Faltan el uuid del documento o la caja registradora.")

    try:
        # Cancelación delegada al servicio (Auto-Login B2B)
        cancel_timestamp = await process_order_cancellation(
            db, 
            document_uuid, 
            cash_register_uuid, 
            products_to_restore
        )
        return OrderCancelResponse(
            documentUuid=document_uuid,
            sicarTimestamp=cancel_timestamp,
            message="Pedido cancelado exitosamente.",
            status="CANCELLED"
        )   
        
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))