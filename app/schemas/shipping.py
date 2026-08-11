from typing import Optional
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
