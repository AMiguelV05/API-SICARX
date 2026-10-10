import logging
from fastapi import APIRouter, Body, Depends, Path, Request, Response, status
from app.core.database import DbDep
from app.core.rate_limit import limiter
from app.core.security import CurrentClientDep, OptionalClientHeaderDep, validate_api_key
from app.schemas.notifications import (
    StockAlertCreate, StockAlertListResponse, StockAlertPublic, StockAlertWithProduct,
    UnsubscribeRequest, UnsubscribeResponse,
)
from app.schemas.products import ProductBasic
from app.services import stock_alert_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Notifications"], dependencies=[Depends(validate_api_key)])


def _public(alert, product_uuid: str) -> StockAlertPublic:
    return StockAlertPublic(
        uuid=alert.uuid, product_uuid=product_uuid, email=alert.email,
        created_at=alert.created_at, expires_at=alert.expires_at,
    )


@router.post("/products/{product_uuid}/stock-alerts", response_model=StockAlertPublic, status_code=status.HTTP_201_CREATED,
             summary="Avisame cuando llegue (suscribirse a un producto agotado)")
@limiter.limit("10/minute")
async def create_stock_alert(
    request: Request, response: Response, db: DbDep, client: OptionalClientHeaderDep,
    product_uuid: str = Path(), data: StockAlertCreate = Body(),
):
    """`X-Client-Token` opcional: con token se usa el correo de la cuenta y se ignora el del
    body. `201` nueva; `200` si ya estaba suscrito (idempotente); `409` si el producto si
    tiene existencia; `404` si no existe; `400` sin correo; `429` por limite de frecuencia.
    Cuando vuelve a haber existencia se manda el webhook `product-back-in-stock` una sola
    vez. La suscripcion caduca a los 90 dias."""
    alert, created = await stock_alert_service.subscribe(db, product_uuid, data.email, client)
    if not created:
        response.status_code = status.HTTP_200_OK
    return _public(alert, product_uuid)


@router.get("/auth/me/stock-alerts", response_model=StockAlertListResponse, summary="Mis avisos de reabastecimiento")
async def list_my_stock_alerts(db: DbDep, client: CurrentClientDep):
    rows = await stock_alert_service.list_client_alerts(db, client)
    docs = []
    for alert, product in rows:
        visible = product.is_active and not product.is_deleted
        docs.append(StockAlertWithProduct(
            **_public(alert, product.sicar_uuid).model_dump(),
            product=ProductBasic.model_validate(product) if visible else None,
        ))
    return StockAlertListResponse(docs=docs)


@router.delete("/auth/me/stock-alerts/{product_uuid}", status_code=status.HTTP_204_NO_CONTENT, summary="Cancelar un aviso de reabastecimiento")
async def delete_my_stock_alert(db: DbDep, client: CurrentClientDep, product_uuid: str = Path()):
    """Idempotente: sin suscripcion activa tambien es `204`."""
    await stock_alert_service.cancel_client_alert(db, client, product_uuid)


@router.post("/notifications/unsubscribe", response_model=UnsubscribeResponse, summary="Darse de baja desde el enlace de un correo")
@limiter.limit("20/minute")
async def unsubscribe(request: Request, db: DbDep, data: UnsubscribeRequest = Body()):
    """Sin sesion: el token del enlace prueba la identidad. `scope: marketing` apaga
    `marketingEmails` de la cuenta; `scope: stock-alert` cancela esa suscripcion.
    Idempotente. `400` si el token no es valido."""
    scope, masked = await stock_alert_service.unsubscribe(db, data.token)
    return UnsubscribeResponse(scope=scope, email=masked)
