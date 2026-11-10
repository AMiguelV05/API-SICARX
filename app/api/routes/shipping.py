import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.core.database import DbDep
from app.core.rate_limit import limiter
from app.core.security import OptionalClientHeaderDep, validate_api_key
from app.schemas.shipping_quote import ShippingQuoteCreate, ShippingQuoteOptionPublic, ShippingQuotePublic
from app.services import shipping_quote_service
from app.services.delivery_service import resolve_delivery_address
from app.services.order_service import merge_requested_quantities

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/shipping", tags=["Shipping"], dependencies=[Depends(validate_api_key)])


@router.post("/quotes", response_model=ShippingQuotePublic, status_code=status.HTTP_201_CREATED, summary="Cotizar el envío a domicilio del carrito")
@limiter.limit("20/minute")
async def create_shipping_quote(request: Request, client: OptionalClientHeaderDep, db: DbDep, data: ShippingQuoteCreate = Body()):
    """Cotiza con envia.com el envío del carrito a la dirección dada y guarda la cotización
    (válida 30 min). Mandar `quoteUuid` + `optionId` elegidos en `POST /orders` como `shipping`.

    `status: NOT_QUOTABLE` (algún producto sin peso/medidas, o sin tarifas de envia.com): el
    envío no se cobra en línea; igual se manda `shipping.quoteUuid` (sin `optionId`) en
    `POST /orders`. `X-Client-Token` opcional: con cuenta usa `addressUuid`; sin cuenta, `address`.
    Puede tardar varios segundos (consulta varias paqueterías)."""
    quantities = merge_requested_quantities(data.products)
    if not quantities:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El carrito no contiene productos válidos.")
    _, snapshot = await resolve_delivery_address(db, client, data.address_uuid, data.address)
    quote = await shipping_quote_service.create_quote(db, client.id if client else None, quantities, snapshot)
    return ShippingQuotePublic(
        quote_uuid=quote.uuid,
        status=quote.status,
        reason=quote.reason,
        expires_at=quote.expires_at,
        options=[
            ShippingQuoteOptionPublic(
                option_id=o["optionId"], carrier=o.get("carrier"), service_description=o.get("serviceDescription"),
                delivery_estimate=o.get("deliveryEstimate"), price=float(o["price"]), currency=o.get("currency") or "MXN",
            )
            for o in quote.options or []
        ],
        missing_dimensions=quote.missing_dimensions or [],
    )
