import logging
from fastapi import APIRouter, Depends, Path, Query
from app.core.database import DbDep
from app.core.security import validate_api_key, CurrentClientDep
from app.schemas.orders import OrderPublic, OrderListResponse
from app.services.order_history_service import list_client_orders, get_client_order, refresh_order_status_if_needed, TERMINAL_STATUSES

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/me/orders", tags=["Client Orders"], dependencies=[Depends(validate_api_key)])

@router.get("", response_model=OrderListResponse, summary="Listar historial de pedidos del cliente")
async def list_my_orders(
    client: CurrentClientDep,
    db: DbDep,
    limit: int = Query(default=60, ge=1, le=200, description="Cantidad de ordenes por pagina (1-200)"),
    offset: int = Query(default=0, ge=0, description="Paginacion (inicio)"),
):
    """
    Historial de pedidos de la cuenta autenticada, mas recientes primero. Solo
    Postgres local (sin llamadas a Sicar X) — igual que `POST /products` frente a
    `GET /products/{uuid}`.
    """
    total, orders = await list_client_orders(db, client.id, limit, offset)
    return OrderListResponse(total=total, docs=orders)

@router.get("/{order_uuid}", response_model=OrderPublic, summary="Detalle de un pedido del cliente")
async def get_my_order(client: CurrentClientDep, db: DbDep, order_uuid: str = Path()):
    """
    Detalle de un pedido (debe pertenecer al cliente autenticado, si no responde `404`).
    Si el estado local no es definitivo, intenta refrescarlo contra Sicar X antes de
    responder — igual que el refresco perezoso de `GET /products/{uuid}`.
    """
    order = await get_client_order(db, client.id, order_uuid)
    if order.status not in TERMINAL_STATUSES:
        order = await refresh_order_status_if_needed(db, order)
    return order
