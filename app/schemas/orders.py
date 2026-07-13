from pydantic import BaseModel, Field
from typing import List, Optional
from app.core.config import settings

class ProductItem(BaseModel):
    uuid: str = Field(..., description="UUID del producto en Sicar")
    quantity: float = Field(..., description="Cantidad a comprar o cancelar")

class ContactInfo(BaseModel):
    name: str
    phone: str
    email: Optional[str] = None

class DeliveryInfo(BaseModel):
    contactInfo: ContactInfo
    deliveryType: str = Field(..., example="PICKUP")

# MODELOS PRINCIPALES

class OrderCreate(BaseModel):
    """
    Contrato simplificado: el frontend solo envía el carrito (uuid + cantidad) y los
    datos de entrega. Precios, impuestos, sku, descripción, unidad y totales se calculan
    en el backend (ver order_service.build_order_payload) a partir de datos de Sicar X y
    del catálogo local.
    """
    products: List[ProductItem]
    deliveryInfo: DeliveryInfo
    contentId: Optional[str] = None
    branchId: Optional[int] = Field(default=None, description="Por defecto 151456 si se omite")
    priceListUuid: Optional[str] = Field(default=None, description="Por defecto settings.SICAR_PRICE_LIST_ID si se omite")
    wholesalePrices: bool = Field(default=False)

class OrderCancel(BaseModel):
    cashRegisterUuid: str = Field(default_factory=lambda: settings.CASH_REGISTER_UUID, description="UUID de la caja registradora")
    uuid: str = Field(..., description="El `id` devuelto por OrderResponse al crear la orden (no un UUID real de Sicar; el backend lo resuelve al uuid interno antes de cancelar)")
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