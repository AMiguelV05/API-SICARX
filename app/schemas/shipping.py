from typing import List, Literal, Optional
from datetime import datetime
from pydantic import Field
from app.schemas.base import CamelModel

# Extraida de admin.py para que app/schemas/orders.py pueda importarla sin crear un
# ciclo (admin.py -> client.py -> cart.py -> orders.py).


class ShippingLabelInfo(CamelModel):
    carrier: str
    service: str
    shipment_id: Optional[int] = Field(default=None, description="ID del envio en envia.com - buscar por este valor en su dashboard")
    service_description: Optional[str] = None
    tracking_number: Optional[str] = None
    track_url: Optional[str] = None
    label_url: Optional[str] = None
    total_price: float
    currency: str
    weight: float
    length: float
    width: float
    height: float
    generated_at: datetime
    # Todas las cajas si el envio llevo mas de una (los campos planos de arriba son la primera).
    # Null en guias generadas antes de las cajas multiples.
    packages: Optional[List[dict[str, float]]] = None


class OrderShippingQuotePublic(CamelModel):
    """Opcion de envio elegida en el checkout, vista por el cliente (Order.shipping_quote sin la
    tarifa base de envia.com ni las cajas). status QUOTED = se cobro `price`; MANUAL = envio sin
    costo, la tienda lo cotiza y coordina aparte."""
    status: Literal["QUOTED", "MANUAL"]
    reason: Optional[str] = Field(default=None, description="Solo MANUAL: MISSING_DIMENSIONS, NO_RATES o UPSTREAM_ERROR.")
    carrier: Optional[str] = None
    service_description: Optional[str] = None
    delivery_estimate: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = None


class OrderShippingSelection(CamelModel):
    """`shipping` de POST /orders: la opcion elegida de POST /shipping/quotes."""
    quote_uuid: str = Field(description="quoteUuid de POST /shipping/quotes.")
    option_id: Optional[str] = Field(default=None, description="optionId elegido. Obligatorio si la cotización es QUOTED.")
