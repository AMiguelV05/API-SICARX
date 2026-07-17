from pydantic import EmailStr, Field
from typing import Any, List, Literal, Optional
from datetime import datetime
from app.core.config import settings
from app.schemas.base import CamelModel

class ProductItem(CamelModel):
    uuid: str = Field(description="UUID del producto en Sicar")
    quantity: float = Field(description="Cantidad a comprar o cancelar")

class ContactInfo(CamelModel):
    name: str
    phone: str
    email: Optional[EmailStr] = None

class DeliveryInfo(CamelModel):
    contactInfo: ContactInfo
    deliveryType: Literal["PICKUP"] = Field(description="Unico valor soportado hoy")

# MODELOS PRINCIPALES

class OrderCreate(CamelModel):
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

class OrderCancel(CamelModel):
    cashRegisterUuid: str = Field(default_factory=lambda: settings.CASH_REGISTER_UUID, description="UUID de la caja registradora")
    products: List[ProductItem]

class OrderResponse(CamelModel):
    id: str = Field(description="ID de la orden en Sicar")
    serieFolio: str = Field(description="Folio del documento creado en Sicar")
    date: float = Field(description="Fecha y hora de la orden en Sicar")
    status: str = Field(description="Estado de la orden en Sicar")
    orderUuid: str = Field(description="UUID local de la orden - usar con GET /auth/me/orders/{orderUuid}")

class OrderCancelResponse(CamelModel):
    documentUuid: str = Field(description="UUID del documento cancelado")
    sicarTimestamp: float = Field(description="Timestamp de cancelación en Sicar")
    message: str = Field(description="Mensaje de confirmación de cancelación")
    status: str = Field(description="Estado de la orden después de la cancelación")

# HISTORIAL DE ORDENES DEL CLIENTE (GET /auth/me/orders)

class OrderPublic(CamelModel):
    uuid: str
    sicar_order_id: str
    serie_folio: Optional[str]
    status: str
    dispatch_status: Optional[str] = Field(default=None, description="Estado de cumplimiento en Sicar X: PENDING_ACCEPTANCE, PENDING, PREPARING, COMPLETE o DISPATCHED")
    dispatch_history: Optional[list[dict[str, Any]]] = Field(default=None, description="Historial de cambios de dispatch_status")
    total: float
    total_quantity: float
    delivery_info: dict[str, Any]
    items: list[dict[str, Any]]
    created_at: datetime

class OrderListResponse(CamelModel):
    total: int
    docs: List[OrderPublic]
