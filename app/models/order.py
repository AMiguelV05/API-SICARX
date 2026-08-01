import uuid
from sqlalchemy import Column, Integer, String, Numeric, JSON, DateTime, ForeignKey, Index, UniqueConstraint, func, text
from sqlalchemy.orm import relationship
from app.core.database import Base

class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String, unique=True, index=True, nullable=False, default=lambda: str(uuid.uuid4()))

    # Sin ondelete=CASCADE a proposito (a diferencia de ClientAddress): una orden es un
    # registro financiero que no queremos borrar en cascada si algun dia se agrega
    # eliminacion de cuentas (no existe esa funcionalidad hoy).
    client_account_id = Column(
        Integer, ForeignKey("client_accounts.id"), nullable=False, index=True
    )

    # Identificador publico de la orden, generado localmente con uuid4()
    # (order_history_service.create_local_order) - ya NO viene de Sicar X. Nombre
    # historico ("sicar_order_id") de cuando checkout si creaba un documento ahi; se
    # conserva para no romper el contrato de URL con el frontend (POST /orders/{id}/pay,
    # /cancel) - ver CLAUDE.md, "SICAR es solo ERP de inventario".
    sicar_order_id = Column(String, unique=True, index=True, nullable=False)
    # serie_folio: sin uso por la misma razon que sicar_order_id de arriba es
    # generado localmente ahora - no hay ningun documento de Sicar X del que
    # pueda salir un folio real. Se conserva (a diferencia de sicar_document_uuid/
    # sicar_date, eliminadas) porque sigue expuesta en OrderPublic/AdminOrderPublic
    # y leida en el reintento de idempotencia de POST /orders - drop cuando se
    # limpie ese contrato tambien.
    serie_folio = Column(String, nullable=True)

    # PAID: pagado (via Mercado Pago o legado); TO_PAY: orden creada en Sicar pero el
    # pago con Mercado Pago sigue pendiente/en proceso (OXXO, wallet, tarjeta en
    # revision); CANCELLED: pago rechazado/cancelado o cancelacion manual - ver
    # order_history_service.finalize_order_payment.
    status = Column(String, nullable=False, default="TO_PAY")

    # Estado de cumplimiento/entrega segun Sicar X (dispatchStatus en document-graph/v1/graph-v2:
    # PENDING_ACCEPTANCE, PENDING, PREPARING, COMPLETE, DISPATCHED - confirmado en vivo contra
    # una orden real, ver CLAUDE.md). Distinto de `status` (arriba), que es nuestro propio
    # tracking de pago/cancelacion - dos dimensiones separadas del mismo documento en Sicar.
    dispatch_status = Column(String, nullable=True)

    branch_id = Column(Integer, nullable=True)
    total = Column(Numeric(10, 2), nullable=False)
    total_quantity = Column(Numeric(10, 2), nullable=False)

    # Placeholder para una futura integracion de costo de envio (envia.com, API mexicana
    # de tarifas de paqueteria). Intencionalmente sin usar hoy: no se calcula, no se suma
    # al cobro de Mercado Pago (payment_service.py) ni a ecOrderDto.total enviado a Sicar X
    # - ambos siguen reflejando solo el total de productos, para PICKUP y DELIVERYMAN por igual.
    delivery_cost = Column(Numeric(10, 2), nullable=True)

    # Snapshot al momento de la orden (misma estructura que produce build_order_payload,
    # no se consulta dentro de estos campos, asi que JSON simple basta - igual que
    # Product.additional_skus/additional_images). `items` es una COPIA de
    # ecOrderDto["products"] con un campo extra, `imageUrl` (Product.image_url por linea,
    # agregado en order_history_service.create_local_order) - nunca el mismo objeto
    # enviado a Sicar X, que no lleva ese campo.
    delivery_info = Column(JSON, nullable=False)
    items = Column(JSON, nullable=False)

    # Foto fija (ClientAddressPublic completo, en camelCase) de la direccion de entrega
    # tomada al crear la orden - solo para DELIVERYMAN, None para PICKUP. Deliberadamente
    # separada de delivery_info["contactInfo"]["address"] (arriba): esa es la forma que
    # Sicar X exige (district/reference, sin uuid/label/lat/lng), esta es la forma
    # ClientAddressPublic que el panel admin necesita para "Guia de envio" (ver
    # ADMIN_INTEGRATION.md). Foto fija a proposito, no un join en vivo contra la libreta de
    # direcciones: el cliente puede editar/borrar esa direccion despues de ordenar, y
    # generar una guia contra una direccion ya cambiada seria un bug silencioso.
    delivery_address_snapshot = Column(JSON, nullable=True)

    # Guia de envio generada con envia.com (ver app/services/shipping_service.py, POST
    # /admin/orders/{uuid}/shipping/generate). JSON simple, mismo criterio que
    # delivery_info/items arriba - nada consulta dentro de este campo. None hasta que se
    # genere una guia; no hay regeneracion (admin_service.generate_shipping_label rechaza
    # con 409 si ya esta poblado).
    shipping_label = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=func.now())

    # Soft-delete para DELETE /orders/{id}: antes borraba la fila por completo, pero eso
    # rompia el patron de cancelacion local-first (el worker necesita que la fila siga
    # existiendo para poder seguir intentando la cancelacion en Sicar X - ver
    # app/worker/sicar_sync_worker.py). En vez de eso se marca aqui y se filtra en las
    # consultas de cara al cliente (list_client_orders, get_client_order,
    # get_owned_order_by_sicar_id, get_order_by_uuid) para que siga desapareciendo del
    # historial exactamente como documenta FRONTEND_INTEGRATION.md. Un solo columna
    # nullable (a diferencia del par is_deleted/deleted_at de Product, que es un
    # accidente historico de dos migraciones seguidas, no un patron a propósito).
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    # Datos del pago con Mercado Pago (ver app/services/payment_service.py). Nulos
    # mientras no se haya intentado ningun cobro (p. ej. justo despues de crear la
    # orden, antes de que el Payment Brick haga submit).
    mp_payment_id = Column(String, unique=True, index=True, nullable=True)
    mp_status = Column(String, nullable=True)  # approved/pending/in_process/rejected/cancelled/refunded
    mp_status_detail = Column(String, nullable=True)
    mp_payment_method_id = Column(String, nullable=True)  # visa/oxxo/account_money/...
    mp_ticket_url = Column(String, nullable=True)  # external_resource_url (p. ej. ficha OXXO)

    # Antes solo vivia como variable local en routes/orders.py::create_order - persistido
    # para poder reconstruir OrderResponse en un reintento idempotente (ver
    # OrderIdempotencyKey) sin volver a llamar a Mercado Pago.
    mp_preference_id = Column(String, nullable=True)

    # Aceptacion administrativa (ver app/api/routes/admin.py, POST /admin/orders/{uuid}/accept).
    # `accepted_by` es texto libre (identificador/nombre de quien acepto) - no existe un
    # modelo de usuarios admin todavia, solo la llave estatica ADMIN_API_KEY, asi que no hay
    # una FK real a la que apuntar. Independiente de dispatch_status (arriba): accepted_at
    # es el gesto local de "un humano de este lado ya vio y acepto esta orden", que hoy en
    # dia no empuja nada a Sicar X todavia - ver el stub de accion "ACCEPT" en
    # app/worker/sicar_sync_worker.py.
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    accepted_by = Column(String, nullable=True)

    # Metadato local de mensajeria (ver POST /admin/orders/{uuid}/assign-delivery).
    # Deliberadamente texto libre, sin llamada a ninguna API de paqueteria (envia.com u
    # otra) - mismo espiritu de placeholder que delivery_cost arriba, para no bloquear la
    # funcionalidad admin en una integracion de courier que todavia no existe.
    delivery_company = Column(String, nullable=True)
    delivery_assigned_at = Column(DateTime(timezone=True), nullable=True)

    client_account = relationship("ClientAccount", back_populates="orders")


class OrderIdempotencyKey(Base):
    """Soporte de idempotencia para POST /orders: un reintento/doble-submit del mismo
    checkout (con el mismo header `Idempotency-Key`) no debe crear una segunda orden en
    Sicar X ni una segunda fila local. Ver app/services/order_idempotency_service.py."""
    __tablename__ = "order_idempotency_keys"
    __table_args__ = (
        UniqueConstraint("client_account_id", "idempotency_key", name="ux_order_idempotency_client_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    client_account_id = Column(Integer, ForeignKey("client_accounts.id"), nullable=False, index=True)
    idempotency_key = Column(String, nullable=False)
    # NULL mientras la orden todavia se esta creando (o si un intento previo se abandono
    # a medio camino) - ver order_idempotency_service.claim_idempotency_key.
    order_uuid = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SicarSyncOutbox(Base):
    """Cola de sincronizacion asincrona hacia Sicar X: cancelar una orden localmente
    (order_history_service.prepare_local_cancellation) ya no espera a que Sicar X
    confirme - restaura stock, marca CANCELLED y encola una fila aqui en la misma
    transaccion. app/worker/sicar_sync_worker.py drena esta tabla en un job aparte,
    con reintentos, para que un Sicar X caido nunca bloquee una cancelacion. `action`
    es deliberadamente generico ("CANCEL"/"ACCEPT" hoy - ambos aplican/revierten el
    descuento de inventario via sicar_stock_service, ver admin_service.accept_order y
    order_history_service.prepare_local_cancellation) para poder reusar este mecanismo
    con otras operaciones a futuro sin necesitar otra tabla. Antes tambien existian
    "DISPATCH"/"SYNC_DISPATCH_STATUS" para mirrorear dispatch_status hacia Sicar X -
    eliminadas junto con la creacion de documentos en Sicar X (ver CLAUDE.md, "SICAR es
    solo ERP de inventario"): dispatch_status es puramente local ahora.

    `status` incluye IN_PROGRESS (ademas de PENDING/SUCCEEDED/FAILED) para que el
    worker pueda reclamar una fila y soltar el lock de inmediato en vez de mantenerlo
    abierto durante toda la llamada HTTP a Sicar X - ver sicar_sync_worker.py. Una fila
    IN_PROGRESS cuyo updated_at ya es viejo (worker caido a medio proceso) vuelve a ser
    reclamable despues de un tiempo, en vez de quedar huerfana para siempre."""
    __tablename__ = "sicar_sync_outbox"
    __table_args__ = (
        Index("ix_sicar_sync_outbox_order_id", "order_id"),
        Index("ix_sicar_sync_outbox_status_next_attempt_at", "status", "next_attempt_at"),
        Index(
            "ix_sicar_sync_outbox_one_pending_per_order_action",
            "order_id", "action",
            unique=True,
            postgresql_where=text("status in ('PENDING', 'IN_PROGRESS')"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    action = Column(String, nullable=False)  # "CANCEL"/"ACCEPT" hoy
    cash_register_uuid = Column(String, nullable=False)
    status = Column(String, nullable=False, default="PENDING")  # PENDING/IN_PROGRESS/SUCCEEDED/FAILED
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(String, nullable=True)
    next_attempt_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=func.now())
