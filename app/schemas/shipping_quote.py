from datetime import datetime
from typing import List, Literal, Optional

from pydantic import Field, model_validator

from app.schemas.base import CamelModel
from app.schemas.orders import GuestDeliveryAddress, ProductItem

# Cotizacion de envio del checkout - ver shipping_quote_service.py. Modulo propio (no
# schemas/shipping.py) porque necesita tipos de schemas/orders.py, que a su vez importa
# schemas/shipping.py.

QuoteStatus = Literal["QUOTED", "NOT_QUOTABLE"]
NotQuotableReason = Literal["MISSING_DIMENSIONS", "NO_RATES", "UPSTREAM_ERROR"]


class ShippingQuoteCreate(CamelModel):
    products: List[ProductItem] = Field(min_length=1, description="Mismo carrito que se mandará a POST /orders.")
    address_uuid: Optional[str] = Field(default=None, description="Dirección guardada (solo cuentas, con X-Client-Token).")
    address: Optional[GuestDeliveryAddress] = Field(default=None, description="Dirección inline (solo invitados).")

    @model_validator(mode="after")
    def _exactly_one_address(self):
        if bool(self.address_uuid) == bool(self.address):
            raise ValueError("Envía addressUuid (cuenta) o address (invitado), pero no ambos ni ninguno.")
        return self


class ShippingQuoteOptionPublic(CamelModel):
    option_id: str = Field(description="Mandar en POST /orders como shipping.optionId.")
    carrier: Optional[str] = None
    service_description: Optional[str] = None
    delivery_estimate: Optional[str] = None
    price: float = Field(description="Lo que se cobra al cliente (MXN, con IVA).")
    currency: str


class ShippingQuotePublic(CamelModel):
    quote_uuid: str = Field(description="Mandar en POST /orders como shipping.quoteUuid.")
    status: QuoteStatus = Field(description="QUOTED: elegir una opción. NOT_QUOTABLE: el envío no se cobra en línea; la tienda lo cotiza y coordina aparte.")
    reason: Optional[NotQuotableReason] = Field(default=None, description="Solo NOT_QUOTABLE.")
    expires_at: datetime = Field(description="Después de esto POST /orders responde 409 y hay que volver a cotizar.")
    options: List[ShippingQuoteOptionPublic] = []
    missing_dimensions: List[str] = Field(default_factory=list, description="uuids de productos sin peso/medidas (reason MISSING_DIMENSIONS).")
