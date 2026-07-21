from pydantic import EmailStr, Field, model_validator
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
    deliveryType: Literal["PICKUP", "DELIVERYMAN"] = Field(description="PICKUP: recoger en tienda. DELIVERYMAN: entrega a domicilio, requiere addressUuid.")
    addressUuid: Optional[str] = Field(default=None, description="UUID de una direccion guardada del cliente (GET /auth/me/addresses) - obligatorio si deliveryType es DELIVERYMAN, no debe enviarse si es PICKUP")

    @model_validator(mode="after")
    def validate_address_uuid_matches_delivery_type(self):
        if self.deliveryType == "DELIVERYMAN" and not self.addressUuid:
            raise ValueError("addressUuid es obligatorio cuando deliveryType es DELIVERYMAN.")
        if self.deliveryType == "PICKUP" and self.addressUuid is not None:
            raise ValueError("addressUuid no debe enviarse cuando deliveryType es PICKUP.")
        return self

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
    status: str = Field(description="Estado de la orden en Sicar justo despues de crearla (antes de cobrar, tipicamente TO_PAY)")
    orderUuid: str = Field(description="UUID local de la orden - usar con GET /auth/me/orders/{orderUuid} y con POST /orders/{id}/pay")
    preferenceId: Optional[str] = Field(default=None, description="ID de preferencia de Mercado Pago para initialization.preferenceId del Payment Brick (null si Mercado Pago no respondio)")
    amount: float = Field(description="Total autoritativo de la orden - usar en initialization.amount del Payment Brick")

class OrderCancelResponse(CamelModel):
    documentUuid: str = Field(description="UUID del documento cancelado")
    sicarTimestamp: float = Field(description="Timestamp de cancelación en Sicar")
    message: str = Field(description="Mensaje de confirmación de cancelación")
    status: str = Field(description="Estado de la orden después de la cancelación")

# PAGO CON MERCADO PAGO (POST /orders/{id}/pay)

class PayerIdentification(CamelModel):
    type: Optional[str] = None
    number: Optional[str] = None

class PaymentPayer(CamelModel):
    email: Optional[EmailStr] = None
    identification: Optional[PayerIdentification] = None

class PaymentSubmit(CamelModel):
    """Shape del `formData` que entrega el `onSubmit` del Payment Brick (ver
    payments_submissions/cards.md y other-payment-methods.md) - se reenvia tal cual."""
    token: Optional[str] = Field(default=None, description="Token de tarjeta generado por el Brick (ausente en OXXO/otros metodos sin tarjeta)")
    paymentMethodId: str = Field(description="p. ej. visa, oxxo, etc.")
    issuerId: Optional[str] = None
    installments: Optional[int] = Field(default=1)
    payer: PaymentPayer

class OrderPayResponse(CamelModel):
    orderUuid: str = Field(description="UUID local de la orden")
    status: str = Field(description="Estado local de la orden despues del intento de cobro: TO_PAY, PAID o CANCELLED")
    mpPaymentId: Optional[str] = Field(default=None, description="ID del pago en Mercado Pago")
    mpStatus: Optional[str] = Field(default=None, description="Estado crudo de Mercado Pago: approved/pending/in_process/rejected/cancelled")
    mpStatusDetail: Optional[str] = Field(default=None, description="Detalle del estado, p. ej. motivo de rechazo")
    ticketUrl: Optional[str] = Field(default=None, description="Liga a la ficha/barcode de pago (OXXO y similares) si aplica")

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
