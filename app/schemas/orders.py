from pydantic import BaseModel, Field
from typing import List, Optional

class ProductItem(BaseModel):
    uuid: str = Field(..., description="UUID del producto en Sicar")
    quantity: float = Field(..., description="Cantidad a comprar o cancelar")

# Sub-modelos para la orden

class OrderProductItem(BaseModel):
    uuid: str = Field(..., description="UUID del producto en Sicar")
    type: int
    sku: str
    description: str
    quantity: str = Field(..., description="Cantidad comprada (en texto)")
    unit: str
    priceBaseTax: str
    priceTax: str
    amountTax: str
    taxesIds: List[str] = Field(default_factory=list)

class ContactInfo(BaseModel):
    name: str
    phone: str
    email: str

class DeliveryInfo(BaseModel):
    contactInfo: ContactInfo
    deliveryType: str = Field(..., example="PICKUP")

class EcOrderDto(BaseModel):
    uuid: str = Field(..., description="UUID de la orden web")
    timeZone: str = Field(default="America/Mexico_City")
    type: str = Field(default="SALE")
    serie: str
    isoCurrency: str = Field(default="MXN")
    decimals: int = Field(default=2)
    opMode: str = Field(default="MX")
    total: str
    products: List[OrderProductItem]
    ecOrderType: str = Field(default="REMOTE")
    deliveryInfo: DeliveryInfo

# MODELOS PRINCIPALES

class OrderCreate(BaseModel):
    contentId: str
    branchId: int = Field(default=151456)
    payload: str = Field(..., description="JWT de autenticación del cliente")
    priceListUuid: str = Field(default="0b8b0848-3880-4085-b213-3b3d30c79429")
    priceNumber: int = Field(default=1)
    totalTax: str
    totalQuantity: str
    wholesalePrices: bool = Field(default=False)
    ecOrderDto: EcOrderDto

class OrderCancel(BaseModel):
    cashRegisterUuid: str = Field(default="332a974c-6186-4135-9a14-c3fc16665156", description="UUID de la caja registradora")
    uuid: str = Field(..., description="UUID del ticket/documento de venta")
    products: List[ProductItem]

class OrderResponse(BaseModel):
    id: str = Field(..., description="ID de la orden en Sicar")
    serieFolio: str = Field(..., description="Folio del documento creado en Sicar")
    date: float = Field(..., description="Fecha y hora de la orden en Sicar")
    status: str = Field(..., description="Estado de la orden en Sicar")

class OrderCancelResponse(BaseModel):
    documentUuid: str = Field(..., description="UUID del documento cancelado")
    sicarTimestamp: float = Field(..., description="Timestamp de cancelación en Sicar")
    message: str = Field(..., description="Mensaje de confirmación de cancelación")
    status: str = Field(..., description="Estado de la orden después de la cancelación")