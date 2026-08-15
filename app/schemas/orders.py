import re
from pydantic import EmailStr, Field, field_validator, model_validator
from typing import Any, List, Literal, Optional
from datetime import datetime
from app.core.config import settings
from app.schemas.base import CamelModel
from app.schemas.shipping import ShippingLabelInfo

COUPON_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

def validate_coupon_code_format(v: Optional[str]) -> Optional[str]:
    """Compartido entre OrderCreate.couponCode y CartCouponApply.code."""
    if v is not None and not COUPON_CODE_PATTERN.fullmatch(v):
        raise ValueError("Código de cupón inválido.")
    return v

class ProductItem(CamelModel):
    uuid: str = Field(description="UUID del producto en Sicar")
    quantity: float = Field(gt=0, description="Cantidad a comprar o cancelar")

class ContactInfo(CamelModel):
    name: str
    phone: str = Field(min_length=10, max_length=10, description="Exactamente 10 digitos tras normalizar - Sicar X rechaza telefonos de mas de 10 caracteres, y un telefono vacio/incompleto no debe pasar validacion en silencio (termina mandandose a envia.com en la guia de envio si nadie lo atrapa antes)")
    email: Optional[EmailStr] = None

    @field_validator("phone", mode="before")
    @classmethod
    def strip_phone_formatting(cls, v: Any) -> Any:
        # Frontend puede mandar telefono con formato (+52, espacios, guiones); nos quedamos con los ultimos 10 digitos en vez de rechazar.
        if isinstance(v, str):
            digits = re.sub(r"\D", "", v)
            return digits[-10:] if len(digits) > 10 else digits
        return v

    @field_validator("email", mode="before")
    @classmethod
    def blank_email_to_none(cls, v: Any) -> Any:
        # Campo opcional sin tocar en el frontend suele llegar como "" - EmailStr lo rechaza aunque sea opcional.
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

_ZIP_CODE_RE = re.compile(r"^\d{5}$")

class GuestDeliveryAddress(CamelModel):
    """Direccion de entrega inline para checkout de invitado (sin cuenta, sin address book).
    Definida aqui (no en schemas/client.py, que ya importa de este modulo via schemas/cart.py)
    para evitar un import circular - mismo set de campos que ClientAddressBase, pero los que
    routes/orders.py ya valida en runtime contra una direccion guardada (ver el chequeo
    `missing` del camino de cuenta) aqui son obligatorios a nivel de schema, asi que un envio
    incompleto es un 422 limpio en vez de un 400 armado a mano."""
    street: str = Field(min_length=1)
    ext_number: str = Field(min_length=1)
    int_number: Optional[str] = None
    neighborhood: str = Field(min_length=1)
    city: str = Field(min_length=1)
    county: str = Field(min_length=1)
    state: str = Field(min_length=1)
    zip_code: str
    references: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    @field_validator("zip_code")
    @classmethod
    def validate_zip_code(cls, v):
        if v is not None and not _ZIP_CODE_RE.match(v):
            raise ValueError("El código postal debe tener 5 dígitos.")
        return v

class DeliveryInfo(CamelModel):
    contactInfo: ContactInfo
    deliveryType: Literal["PICKUP", "DELIVERYMAN"] = Field(description="PICKUP: recoger en tienda. DELIVERYMAN: entrega a domicilio, requiere addressUuid (cuenta) o address (invitado).")
    addressUuid: Optional[str] = Field(default=None, description="UUID de una direccion guardada del cliente (GET /auth/me/addresses) - solo para cuentas autenticadas. Exactamente uno de addressUuid/address es obligatorio si deliveryType es DELIVERYMAN; ninguno debe enviarse si es PICKUP.")
    address: Optional[GuestDeliveryAddress] = Field(default=None, description="Direccion de entrega inline - solo para checkout de invitado (sin X-Client-Token). Exactamente uno de addressUuid/address es obligatorio si deliveryType es DELIVERYMAN.")

    @model_validator(mode="after")
    def validate_address_matches_delivery_type(self):
        # Cual de los dos es legal para ESTE llamador (cuenta vs. invitado) se decide en la
        # ruta, que si conoce el estado de auth - este validador de schema solo puede
        # exigir "exactamente uno de los dos" para DELIVERYMAN.
        if self.deliveryType == "DELIVERYMAN":
            if bool(self.addressUuid) == bool(self.address):
                raise ValueError("Para DELIVERYMAN, envía addressUuid (cuenta) o address (invitado), pero no ambos ni ninguno.")
        else:
            if self.addressUuid is not None or self.address is not None:
                raise ValueError("addressUuid/address no deben enviarse cuando deliveryType es PICKUP.")
        return self

class OrderCreate(CamelModel):
    """Contrato simplificado: el frontend solo envia carrito + datos de entrega; precio/sku/totales se calculan en el backend (ver build_order_payload)."""
    products: List[ProductItem]
    deliveryInfo: DeliveryInfo
    contentId: Optional[str] = None
    branchId: Optional[int] = Field(default=None, description="Por defecto 151456 si se omite")
    priceListUuid: Optional[str] = Field(default=None, description="Por defecto settings.SICAR_PRICE_LIST_ID si se omite")
    wholesalePrices: bool = Field(default=False)
    couponCode: Optional[str] = Field(default=None, description="Código de cupón a aplicar. Se revalida y bloquea aquí (no en el carrito) - nunca se confía en el descuento ya mostrado por GET /cart.")
    termsAcceptedAt: Optional[datetime] = Field(default=None, description="Momento (ISO 8601) en que el comprador aceptó los Términos y Condiciones - principalmente para checkout de invitado, que no tiene ClientAccount donde guardarlo. Opcional, no rompe compatibilidad si se omite.")
    termsAcceptedVersion: Optional[str] = Field(default=None, description="Versión de los Términos y Condiciones aceptada (texto libre, lo decide el frontend).")

    @field_validator("couponCode")
    @classmethod
    def validate_coupon_code(cls, v):
        return validate_coupon_code_format(v)

class OrderCancel(CamelModel):
    cashRegisterUuid: str = Field(default_factory=lambda: settings.CASH_REGISTER_UUID, description="UUID de la caja registradora")
    products: List[ProductItem] = Field(default_factory=list, description="Aceptado por compatibilidad, pero ignorado - el stock se restaura desde la orden guardada localmente, no desde este campo.")

class OrderResponse(CamelModel):
    id: str = Field(description="ID publico canonico de la orden - generado localmente, ya no proviene de Sicar X. Usar con POST /orders/{id}/pay y /cancel. Mismo valor que `sicarOrderId` en OrderPublic.")
    serieFolio: Optional[str] = Field(default=None, description="Ya no aplica - Sicar X no crea ningun documento al hacer checkout. Siempre null.")
    date: Optional[float] = Field(default=None, description="Ya no aplica - Sicar X no crea ningun documento al hacer checkout. Siempre null.")
    status: str = Field(description="Estado local de la orden justo despues de crearla (siempre TO_PAY)")
    orderUuid: str = Field(description="UUID local de la orden - usar con GET /auth/me/orders/{orderUuid} y con POST /orders/{id}/pay")
    preferenceId: Optional[str] = Field(default=None, description="ID de preferencia de Mercado Pago para initialization.preferenceId del Payment Brick (null si Mercado Pago no respondio)")
    amount: float = Field(description="[DEPRECATED, usar `total`] Alias historico de `total` - mismo valor, se conserva por compatibilidad. Nuevas integraciones deben leer `total`.")
    total: float = Field(validation_alias="amount", serialization_alias="total", description="[Recomendado] Total autoritativo de la orden (YA con el descuento del cupón aplicado, si hubo uno) - mismo valor que `amount` y que OrderPublic.total. Usar en initialization.amount del Payment Brick.")
    discountAmount: float = Field(default=0.0, description="Monto descontado por el cupón aplicado, ya restado de `total`/`amount`. 0 si no se usó cupón.")

class OrderCancelResponse(CamelModel):
    documentUuid: str = Field(description="[DEPRECATED, usar `orderId`] Alias historico - el nombre viene de cuando esto era un UUID de documento real de Sicar X; hoy simplemente hace eco del `order_id` (id o orderUuid) que recibió esta llamada. Se conserva por compatibilidad.")
    orderId: str = Field(validation_alias="documentUuid", serialization_alias="orderId", description="[Recomendado] Mismo valor que `documentUuid` (el `id`/`orderUuid` de la orden cancelada) bajo un nombre no ambiguo.")
    sicarTimestamp: float = Field(description="Momento (epoch ms) en que la cancelación se aceptó localmente. Ya no es una confirmación de Sicar X: la sincronización con Sicar X ahora es asíncrona (ver sicar_sync_outbox) y ocurre después de esta respuesta.")
    message: str = Field(description="Mensaje de confirmación de cancelación")
    status: str = Field(description="Estado de la orden después de la cancelación")

class PayerIdentification(CamelModel):
    type: Optional[str] = None
    number: Optional[str] = None

class PaymentPayer(CamelModel):
    email: Optional[EmailStr] = None
    identification: Optional[PayerIdentification] = None

class PaymentSubmit(CamelModel):
    """Shape del `formData` del onSubmit del Payment Brick de Mercado Pago - se reenvia tal cual."""
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

class OrderPublic(CamelModel):
    uuid: str
    sicar_order_id: str = Field(description="[DEPRECATED, usar `id`] Alias historico - el nombre viene de cuando este id provenia de Sicar X (ya no es el caso, ver CLAUDE.md). Se conserva por compatibilidad.")
    id: str = Field(validation_alias="sicar_order_id", serialization_alias="id", description="[Recomendado] Mismo valor que `sicarOrderId`, bajo el mismo nombre que OrderResponse.id - usar con POST /orders/{id}/pay y /cancel.")
    serie_folio: Optional[str]
    status: str
    dispatch_status: Optional[str] = Field(default=None, description="Estado de cumplimiento en Sicar X: PENDING_ACCEPTANCE, PENDING, PREPARING, COMPLETE o DISPATCHED")
    total: float
    total_quantity: float
    delivery_info: dict[str, Any]
    items: list[dict[str, Any]]
    created_at: datetime
    cancellation_reason: Optional[str] = Field(default=None, description="Motivo de cancelación si un admin canceló la orden (POST /admin/orders/{uuid}/cancel). Null si nunca se canceló o si fue el cliente quien canceló.")
    coupon_code: Optional[str] = Field(default=None, description="Código del cupón aplicado, si hubo uno.")
    discount_amount: Optional[float] = Field(default=None, description="Monto descontado por el cupón. Null si no se usó cupón.")
    subtotal: Optional[float] = Field(default=None, description="Total antes del descuento del cupón. Null en órdenes anteriores a esta funcionalidad - tratar como igual a `total` en ese caso.")
    shipping_label: Optional[ShippingLabelInfo] = Field(default=None, description="Guía de envío real (envia.com) si un admin ya la generó - incluye trackUrl para que el cliente siga su paquete. Null hasta entonces; solo aplica a pedidos DELIVERYMAN.")
    terms_accepted_at: Optional[datetime] = Field(default=None, description="Momento en que se aceptaron los Términos y Condiciones al hacer checkout como invitado. Null si no se mandó o si la orden es de una cuenta (ver ClientPublic.termsAcceptedAt en ese caso).")
    terms_accepted_version: Optional[str] = Field(default=None, description="Versión de los Términos y Condiciones aceptada al hacer checkout como invitado.")

class OrderListResponse(CamelModel):
    total: int
    docs: List[OrderPublic]
