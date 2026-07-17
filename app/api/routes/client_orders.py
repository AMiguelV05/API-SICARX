import logging
from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import validate_api_key, get_current_client
from app.models.client import ClientAccount
from app.schemas.orders import OrderPublic, OrderListResponse
from app.services.order_history_service import list_client_orders, get_client_order, refresh_order_status_if_needed, TERMINAL_STATUSES

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/me/orders", tags=["Client Orders"])

@router.get("", response_model=OrderListResponse, summary="Listar historial de pedidos del cliente")
async def list_my_orders(
    limit: int = Query(default=60, description="Cantidad de ordenes por pagina"),
    offset: int = Query(default=0, description="Paginacion (inicio)"),
    client: ClientAccount = Depends(get_current_client),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(validate_api_key)
):
    """
    Historial de pedidos de la cuenta autenticada, mas recientes primero. Solo
    Postgres local (sin llamadas a Sicar X) — igual que `POST /catalog` frente a
    `GET /products/{uuid}`.
    """
    total, orders = await list_client_orders(db, client.id, limit, offset)
    return OrderListResponse(total=total, docs=orders)

@router.get("/{order_uuid}", response_model=OrderPublic, summary="Detalle de un pedido del cliente")
async def get_my_order(
    order_uuid: str = Path(...),
    client: ClientAccount = Depends(get_current_client),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(validate_api_key)
):
    """
    Detalle de un pedido (debe pertenecer al cliente autenticado, si no responde `404`).
    Si el estado local no es definitivo, intenta refrescarlo contra Sicar X antes de
    responder — igual que el refresco perezoso de `GET /products/{uuid}`.
    """
    order = await get_client_order(db, client.id, order_uuid)
    if order.status not in TERMINAL_STATUSES:
        order = await refresh_order_status_if_needed(db, order)
    return order
