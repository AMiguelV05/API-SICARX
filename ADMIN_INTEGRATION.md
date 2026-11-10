# Conectando el dashboard admin a API SICARX

Contrato de `/v1/admin/*` para el panel administrativo (vive dentro de
`https://ferreteriacharly.com/`). El storefront tiene su propio documento:
`FRONTEND_INTEGRATION.md`. Son APIs distintas con ciclos de cambio propios.

| Entorno | URL |
|---|---|
| Producción | `https://api-production-cf7a.up.railway.app` |
| Local | `http://127.0.0.1:8000` |

## Reglas generales

- **Siempre servidor a servidor** (Route Handler o Server Action). El token de admin nunca debe llegar al navegador; guárdalo en una sesión de servidor del dashboard. Por eso CORS no aplica.
- La única cabecera de autenticación es `Authorization: Bearer <admin-token>`. **No** se usan `x-api-key` ni `X-Client-Token`.
- Errores: `{"detail": "..."}`. Paginación: `limit` (1-200; el default varía por ruta, 50 o 60) y `offset` ≥ 0. Listas: `{ total, docs }`.
- `productUuid` siempre es el `sicarUuid` del producto, nunca el `id` interno.
- Esta API no reintenta las llamadas fallidas del dashboard; si las necesitas, implementa tus reintentos.

## Autenticación y roles

```http
POST /v1/admin/auth/login
Content-Type: application/json

{"email": "admin@ferreteriacharly.com", "password": "..."}
```

→ `200 {"token": "<jwt>", "admin": {"uuid", "email", "name", "role", "isActive", "lastLoginAt", "createdAt"}}`.
El rate limit es de 5 por minuto por IP.

- El token dura **12 h**. Un `401` (token faltante, inválido, expirado o admin desactivado) significa que hay que volver a hacer login.
- Roles: `super_admin` (todo) y `staff` (todo **excepto** gestionar admins, ver el audit log, emitir reembolsos y hacer `DELETE` de categorías, vehículos, cupones y sinónimos). Si `staff` llama a una ruta restringida recibe `403`.
- El primer `super_admin` se crea con `create_admin_user.py`, y sus credenciales se entregan por fuera de este repo.

| Ruta | Rol | Notas |
|---|---|---|
| `GET /v1/admin/auth/me` | cualquiera | Mismo objeto `admin` |
| `POST /v1/admin/admins` `{ email, name, password, role }` | super_admin | `role`: `super_admin` \| `staff`. `409` si el correo está duplicado |
| `GET /v1/admin/admins` | super_admin | Paginado |
| `PATCH /v1/admin/admins/{uuid}` | super_admin | Parcial: `name`, `role`, `isActive`, `newPassword` (restablece directo, sin correo) |
| `GET /v1/admin/audit-log` | super_admin | Filtros opcionales: `adminUserUuid`, `action`, `resourceType`, `dateFrom`, `dateTo`; paginado |

Entrada del audit log: `{ id, adminUserUuid, adminUserName, action, resourceType, resourceId, detail, createdAt }`.
Acciones registradas: `order.accept`, `order.cancel`, `order.refund`, `admin_user.create`,
`admin_user.update`, `coupon.create`, `coupon.update`, `coupon.delete`, `category.delete`,
`vehicle.delete`, `review.moderate`, `review.delete`, `brand.rename`, `brand.delete`,
`product.brand_bulk_set`, `product.sale_set`, `product.sale_clear`, `product.sale_bulk_set`,
`product.sale_bulk_clear`, `search_synonym.create`, `search_synonym.update`,
`search_synonym.delete`.

---

## Operación

### `GET /v1/admin/health`

`{ "databaseOk": true, "sicarTokenPresent": true, "outboxCounts": { "PENDING": 0, "IN_PROGRESS": 0, "SUCCEEDED": 12, "FAILED": 0 } }`.
`sicarTokenPresent` solo indica que hay un token en memoria, no que sea válido.

### `GET /v1/admin/sync/catalog-status`

```json
{ "lastRunStartedAt": "...", "lastRunFinishedAt": "...", "lastSuccessAt": "...", "productsProcessed": 124813, "productsDeactivated": 3, "lastError": null }
```

Si nunca ha corrido, todos los campos vienen en `null`. `lastSuccessAt` solo avanza con una corrida
exitosa; `lastRunFinishedAt` y `lastError` siempre reflejan la última corrida. Es la forma de
confirmar que el catálogo se sigue sincronizando.

### Cola `sicar_sync_outbox`

A Sicar X solo se le avisan **deltas de inventario**: `ACCEPT` (descuenta stock al aceptar un
pedido) y `CANCEL` (lo devuelve al cancelar un pedido ya aceptado). El estado local ya es correcto
antes de encolar. Los reintentos usan backoff de 1/2/4/8/16 min, hasta 5 intentos, y luego la
fila queda `FAILED`.

- `GET /v1/admin/sync/outbox?status=PENDING&status=FAILED&limit=50&offset=0`: `status` se puede repetir; por defecto excluye `SUCCEEDED`. Fila: `{ id, orderId, action, status, attempts, lastError, nextAttemptAt, createdAt, updatedAt }`.
- `POST /v1/admin/sync/outbox/{id}/retry`: devuelve una fila `FAILED` a `PENDING` para el siguiente minuto. `404` si la fila no existe o no está `FAILED`.

Una fila `FAILED` hay que reconciliarla a mano en Sicar X, o reintentarla cuando se arregle la causa (casi siempre un problema de token o de red con Sicar X).

---

## Pedidos

### `GET /v1/admin/orders`

Busca en **todas** las cuentas, ordenado por `createdAt` desc (sin `sortBy`). Filtros (AND):
- `status` (`TO_PAY` / `PAID` / `CANCELLED`);
- `dispatchStatus`;
- `clientEmail` (exacto, sin distinguir mayúsculas);
- `clientUuid`;
- `includeDeleted` (default `false`; solo para auditar pedidos que el cliente "borró");
- `limit` (default 50) y `offset`.

`clientEmail` y `clientUuid` **no** encuentran pedidos de invitado: búscalos sin esos filtros y revisa `isGuest` y `guestEmail`.

### `GET /v1/admin/orders/{orderUuid}?includeDeleted=false`

`AdminOrderPublic` es el `OrderPublic` del storefront más:

```json
{
  "deletedAt": null,
  "clientEmail": "juan@example.com", "clientName": "Juan Pérez", "clientUuid": "7f3c...",
  "isGuest": false, "guestEmail": null,
  "acceptedAt": null, "acceptedBy": null,
  "deliveryCompany": null, "deliveryAssignedAt": null,
  "cancellationReason": null, "disputedAt": null,
  "deliveryAddress": { "street": "...", "extNumber": "...", "neighborhood": "...", "city": "...", "county": "...", "state": "...", "zipCode": "...", "...": "..." },
  "shippingLabel": null,
  "shippingCancellationReason": null, "shippingLabelCancelledAt": null
}
```

- `clientEmail` y `clientName` también vienen en pedidos de invitado (salen del contacto). `clientUuid` es `null` para invitados.
- `deliveryAddress` (solo `DELIVERYMAN`) es una **foto fija** tomada al crear el pedido: es el destino del envío, aunque el cliente edite su libreta después.
- `disputedAt` se llena la primera vez que hay una disputa y nunca se borra.
- `404` si el pedido no existe, o si está borrado y no mandaste `includeDeleted=true`.

### Acciones sobre un pedido

| Ruta | Body | Efecto | Errores |
|---|---|---|---|
| `POST .../{uuid}/accept` | `{ acceptedBy? }` (texto libre) | `acceptedAt`, `dispatchStatus: "PENDING"` de inmediato; encola `ACCEPT`. Dispara `order-accepted` al storefront | `404`; `409` si ya estaba aceptado |
| `POST .../{uuid}/cancel` | `{ reason }` (obligatorio, el cliente lo ve) | Reembolsa o cancela el pago en Mercado Pago, cancela y libera la reserva. Si estaba aceptado encola `CANCEL` (el stock vuelve cuando la fila se procesa). Nunca hace soft-delete | `404`; `409` si ya estaba cancelado o `dispatchStatus == DISPATCHED` |
| `POST .../{uuid}/advance-status` | `{ dispatchStatus }` | Ver tabla abajo. Solo local | `404`; `409` si la transición es ilegal, el pedido está cancelado, es `DISPATCHED` sobre `PICKUP` o es un revert con guía real |
| `POST .../{uuid}/assign-delivery` | `{ deliveryCompany }` (texto libre) | Metadato local; se puede reasignar | `404`; `409` si está cancelado o es `PICKUP` |
| `POST .../{uuid}/refund` | `{ amount, reason }` | **super_admin**. Reembolso parcial o total en Mercado Pago. No cambia `status` (sigue `PAID`) ni el inventario | `409` si no está `PAID` o no tiene pago; `400` si `amount` excede lo que falta por reembolsar |
| `GET .../{uuid}/refunds` | | Lista paginada (cualquier admin). Incluye los reembolsos automáticos por cancelación (`issuedByAdminId: null` si canceló el cliente) | |
| `GET .../{uuid}/chargebacks` | | Lista paginada; se detectan solos | |

Respuestas:
- `accept`: `{ orderUuid, acceptedAt, acceptedBy, dispatchStatus, syncStatus: "QUEUED", note }`.
- `cancel`: `{ orderUuid, cancelledAt, reason, status: "CANCELLED", syncStatus: "QUEUED" | "NOT_NEEDED", note }`. `NOT_NEEDED` significa que el pedido no estaba aceptado, así que Sicar X nunca supo de él.
- `advance-status`: `{ orderUuid, dispatchStatus, syncStatus: "QUEUED", note }`. Aquí `syncStatus` es un valor fijo: **a Sicar X no se le avisa nada** de los cambios de `dispatchStatus`.
- `assign-delivery`: `{ orderUuid, deliveryCompany, deliveryAssignedAt }`.
- `refund`: `{ id, orderId, amount, reason, mpRefundId, issuedByAdminId, createdAt }`.
- Contracargo: `{ id, orderId, mpChargebackId, mpPaymentId, amount, reason, status: "IN_PROCESS" | "WON" | "LOST", coverageEligible, documentationRequired, documentationDeadline, createdAt, resolvedAt }`. Un contracargo, un reembolso fuera de banda o una mediación nunca cambian `status`, stock ni el pedido en automático: el admin decide.

**Transiciones de `advance-status`** (`PENDING_ACCEPTANCE` → `PENDING` solo con `/accept`):

| Actual | Objetivos |
|---|---|
| `PENDING` | `PREPARING` |
| `PREPARING` | `PENDING` (revertir), `COMPLETE` |
| `COMPLETE` | `PREPARING` (revertir), `DISPATCHED` (solo `DELIVERYMAN`) |
| `DISPATCHED` | `COMPLETE` (revertir; bloqueado si hay guía real, usa `shipping/cancel`) |

Notifica al storefront solo cuando un `PICKUP` llega a `COMPLETE` ("listo para recoger") y cuando cualquier pedido llega a `DISPATCHED`. No hay notificación en `PREPARING`, en `COMPLETE` de un `DELIVERYMAN` ni en las reversiones.

### Guía de envío con envia.com

Solo para pedidos `DELIVERYMAN` con `dispatchStatus: "COMPLETE"`. El destino es
`deliveryAddress` y el origen es la dirección de la tienda configurada en el servidor.

**`POST .../{uuid}/shipping/quote`**: `{ weight, length, width, height, origin? }` (kg/cm, > 0). No guarda nada.

```json
{ "options": [ { "carrier": "dhl", "service": "1", "serviceDescription": "DHL Standard", "deliveryEstimate": "3-5 días", "totalPrice": 185.50, "currency": "MXN" } ] }
```

- `origin` (opcional; cualquier subconjunto de `name, company, phone, email, street, number, district, city, state, zipCode, reference`) reemplaza el origen campo por campo. `state` acepta el nombre completo.
- `options: []` es válido: ningún carrier cubre esa ruta. Solo se devuelven opciones puerta a puerta.
- `404`; `409` si no es `DELIVERYMAN` o no está `COMPLETE`; `422` si falta dirección o dimensiones; `502` si falla envia.com (con su mensaje).

**`POST .../{uuid}/shipping/generate`**: `{ weight, length, width, height, carrier, service, origin? }`. Genera una guía real (**con costo**), guarda `shippingLabel` y pasa a `DISPATCHED` en la misma transacción. Dispara `order-dispatched` al storefront con la guía. A Sicar X no se le avisa.

```json
{
  "orderUuid": "...", "dispatchStatus": "DISPATCHED",
  "shippingLabel": { "carrier": "dhl", "service": "1", "shipmentId": 179044, "serviceDescription": "DHL Standard", "trackingNumber": "1234567890", "trackUrl": "https://...", "labelUrl": "https://.../label.pdf", "totalPrice": 185.50, "currency": "MXN", "weight": 2.5, "length": 30, "width": 20, "height": 15, "generatedAt": "..." }
}
```

- Con `shipmentId` puedes buscar el envío en el dashboard de envia.com.
- Errores: `409` si ya tiene guía (no se regenera), si no es `DELIVERYMAN` o si no está `COMPLETE`; `422`; `502`.
- **Riesgo conocido**: si el dashboard hace timeout mientras envia.com sí generó la guía, el admin ve un error aunque la guía ya existe (y ya se cobró). No reintentes a ciegas: revisa primero el pedido.

**`POST .../{uuid}/shipping/cancel`**: `{ reason }` (obligatorio, interno; el cliente no lo ve). Cancela ante envia.com, deja `shippingLabel` en `null`, vuelve a `COMPLETE` y guarda `shippingCancellationReason` y `shippingLabelCancelledAt`.

- Respuesta: `{ orderUuid, dispatchStatus: "COMPLETE", shippingLabel: null, refund: { balanceReturned, balanceReturnDate } }` (`refund` es la respuesta cruda de envia.com y no se guarda).
- `409` si no hay guía; `422` si falta `reason`; `502` si envia.com la rechaza (lo más común: el carrier ya la recogió).

---

## Webhooks salientes hacia el dashboard

Esta API llama a `{ADMIN_DASHBOARD_BASE_URL}/api/webhooks/<ruta>` desde el servidor. Si
`ADMIN_DASHBOARD_BASE_URL` o `ADMIN_WEBHOOK_SECRET` no están configurados, no se manda nada (en
producción sí están).

**Verificación (obligatoria):**

```
X-Webhook-Timestamp: 1783961178
X-Webhook-Signature: <hex>
signature = hex(HMAC_SHA256(ADMIN_WEBHOOK_SECRET, "{timestamp}." + rawBody))
```

Usa el body crudo, compara en tiempo constante y rechaza timestamps de más de ~5 min.
`ADMIN_WEBHOOK_SECRET` es distinto del login y se entrega por fuera del repo. **Sin
reintentos**: responde `200` rápido.

| Ruta | Cuándo | Body |
|---|---|---|
| `order-cancelled` | Un pedido pasa a `CANCELLED` (cualquier origen) | `OrderPublic` + `clientEmail` + `clientName`; `cancellationReason` trae texto si canceló un admin |
| `order-sicar-sync-failed` | Una fila del outbox se agotó (`FAILED`) | `{ orderUuid, sicarOrderId, lastError }`; reconcilia a mano |
| `product-stock-drift` | Al terminar un sync, hay productos con `reserved > stock` (se repite cada 5 min mientras no se resuelva) | `{ products: [{ sicarUuid, sku, name, stock, reserved }] }` |
| `order-chargeback-received` | **Urgente**: contracargo ("Compra no reconocida") sobre un pedido pagado | `OrderPublic` + `clientEmail` + `clientName` (con `disputedAt`); `status` sigue `PAID` |
| `order-chargeback-resolved` | El contracargo se resolvió | `{ orderUuid, sicarOrderId, chargebackStatus: "WON" \| "LOST", amount, resolvedAt }` |
| `order-payment-in-mediation` | Mercado Pago abrió una mediación (aviso temprano) | Igual que `chargeback-received` |
| `order-out-of-band-refund` | Reembolsaron directamente en el panel de Mercado Pago | `{ orderUuid, sicarOrderId, amount }`; la fila `Refund` ya existe |

---

## Catálogo PIM

Todas las listas de productos del admin (`.../products`) usan `ProductAdminBasic`: `{ sicarUuid, sku, name, description, imageUrl, price, stock, availableStock, salesCount, averageRating, reviewsCount }`.
Aquí `stock` es el stock físico (de Sicar X) y `availableStock` es lo vendible (stock menos reservas).

**Patrón común de asignaciones** (categorías, vehículos, atributos, grupos de variantes, alcance de cupones):
- `PUT` **reemplaza** el conjunto completo; con una lista vacía lo vacía. **Cuidado**: `GET .../products` devuelve como máximo 200 por página, así que un `PUT` hecho desde un picker que no cargó todo **borra en silencio** lo que no se cargó. Para ediciones parciales usa `PATCH`.
- `PATCH { add, remove }` es incremental. Agregar algo que ya estaba o quitar algo que no estaba no hace nada. Responde `{ ..., added, removed, addedCount, removedCount }` (los conteos son los cambios reales). `422` si un uuid aparece en `add` y en `remove`; `404` (con la lista de cuáles) si algún uuid de `add` no existe.
- El `slug` nunca se manda: se deriva de `name` (sin acentos, con guiones y sufijo `-2`, `-3`… si ya existe) y se recalcula al renombrar.

### Categorías (árbol propio)

Lectura pública del árbol: `GET /v1/taxonomy`. Nodo: `{ uuid, name, slug, parentUuid, updatedAt }`.

| Ruta | Notas |
|---|---|
| `POST /v1/admin/categories` `{ name, parentUuid? }` | `201`. Sin `parentUuid` crea una raíz. `404` si el padre no existe |
| `PATCH /v1/admin/categories/{uuid}` `{ name?, parentUuid? }` | `parentUuid: null` explícito lo mueve a la raíz. `409` si lo mueves dentro de su propio subárbol; `422` si `name` está vacío |
| `DELETE /v1/admin/categories/{uuid}` | **super_admin**. `204`. `409` si tiene hijos, productos o cupones que la usan (sin cascada) |
| `GET /v1/admin/categories/{uuid}/products` | Solo lo asignado directo a ese nodo (sin descendientes); paginado (default 60) |
| `PUT .../{uuid}/products` `{ productUuids }` | Reemplaza → `{ categoryUuid, productUuids }` |
| `PATCH .../{uuid}/products` `{ add, remove }` | Incremental |
| `DELETE .../{uuid}/products` | Desasigna todos (no borra el nodo) → `{ categoryUuid, removedCount }`. Cualquier admin |
| `GET /v1/admin/categories/by-product/{productUuid}` | Categorías asignadas directo al producto (lista simple); `[]` si no tiene; `404` si el producto no existe |
| `GET /v1/admin/categories/export?taxonomyUuid=` | CSV `utf-8-sig`, adjunto `categorias_productos.csv`. Columnas: `category_uuid, category_path, category_slug, product_sku, product_name, product_price, product_stock`. Una fila por par categoría–producto; las categorías vacías aparecen con las columnas de producto en blanco. `category_path` = `Padre > Hijo`; `product_stock` es el disponible |

### Vehículos (compatibilidad)

Tabla plana de fitments `{ uuid, vehicleType: "AUTOMOTIVE" | "MOTORCYCLE", make, model, yearStart, yearEnd, engine, updatedAt }`
(`yearEnd: null` = sigue en producción). Se sembró a partir del catálogo de referencia de
Gonher, así que algunas filas pueden no ser exactas. El selector público está en `FRONTEND_INTEGRATION.md`.

| Ruta | Notas |
|---|---|
| `POST /v1/admin/vehicles` | `201`. `422` si `yearEnd < yearStart` |
| `GET /v1/admin/vehicles?vehicleType=&make=&model=` | `make` y `model` con `ILIKE` parcial; orden por make/model/yearStart |
| `PATCH /v1/admin/vehicles/{uuid}` | Parcial; `yearEnd: null` la marca como vigente |
| `DELETE /v1/admin/vehicles/{uuid}` | **super_admin**. `409` si tiene productos |
| `GET` / `PUT` / `PATCH /v1/admin/vehicles/{uuid}/products` | Patrón común; el `PUT` responde `{ vehicleUuid, productUuids }` |
| `GET /v1/admin/vehicles/by-product/{productUuid}` | Lista simple |
| `GET /v1/admin/vehicles/models-for-years?make=&vehicleType=&years=2012&years=2013` | Modelos que existen en **todos** esos años → `{ docs: [...] }` |
| `POST /v1/admin/vehicles/assign-by-model` `{ vehicleType?, make, model, years, engine?, productUuids }` | **Aditivo** e idempotente: asigna los productos a cada fitment del modelo cuyo rango toque alguno de los años (sin `engine`, a todos los motores). Responde `{ make, model, years, engine, vehicleUuids, productUuids, assignedCount }` (vínculos nuevos). `404` si ningún fitment coincide o algún producto no existe |

### Atributos, presets y grupos de variantes

No dependen de categorías: cualquier atributo se puede poner en cualquier producto. Los valores
se guardan en `Product.attributes` (JSONB, con el slug como clave). Solo el detalle del producto
del storefront los expone.

**Atributo**: `{ uuid, name, slug, dataType: "TEXT" | "NUMBER" | "BOOLEAN" | "ENUM", allowedValues, unit, updatedAt }`.

| Ruta | Notas |
|---|---|
| `POST /v1/admin/attributes` | `allowedValues` es obligatorio (≥ 2) si es `ENUM`; se ignora en otro caso (`422`). `unit` es texto libre |
| `GET /v1/admin/attributes?search=&dataType=` | Orden por `name` |
| `PATCH /v1/admin/attributes/{uuid}` | `409` si renombras o cambias `dataType` y ya hay productos con valores; `422` si un `ENUM` queda con menos de 2 valores |
| `DELETE /v1/admin/attributes/{uuid}` | `409` si algún producto o preset lo usa |
| `GET /v1/admin/attributes/{uuid}/products` | `ProductAdminBasic` + `value` |
| `PUT /v1/admin/attributes/{uuid}/products` `{ values: [{ productUuid, value }] }` | Reemplaza **solo esta clave** en el conjunto (a los que no vengan se les quita); valida todo antes de escribir → `{ attributeUuid, docs }` |
| `PATCH /v1/admin/attributes/{uuid}/products` `{ upsert: [{ productUuid, value }], remove: [productUuid] }` | Incremental → `{ attributeUuid, upserted, removed, upsertedCount, removedCount }` |
| `GET /v1/admin/products/{productUuid}/attributes` | `{ productUuid, docs: [{ attributeUuid, name, slug, dataType, unit, value }] }` |
| `PUT /v1/admin/products/{productUuid}/attributes` `{ values: [{ attributeUuid, value }] }` | Reemplaza todos los atributos del producto |

Validación de `value`: `404` si el atributo no existe; `422` si el tipo no coincide o si un `ENUM`
recibe un valor fuera de `allowedValues`. Nada se escribe si falla una sola fila.

**Presets** (`/v1/admin/attribute-presets`, objeto `{ uuid, name, slug, updatedAt }`): son atajos de captura y nunca validan nada.
- CRUD con `search`. El `DELETE` nunca se bloquea.
- `PUT` / `GET .../{uuid}/attributes` `{ items: [{ attributeUuid, isRequired, displayOrder }] }`: reemplaza los atributos del preset. `isRequired` es solo una pista para la UI.
- `POST .../{uuid}/apply` `{ productUuids }`: agrega con valor `null` las claves que le falten a cada producto (nunca sobrescribe) → `{ presetUuid, productUuids, scaffoldedCount }`. `422` si el preset no tiene atributos.

**Grupos de variantes** (`/v1/admin/variant-groups`, objeto `{ uuid, name, variantAttributeSlug, updatedAt }`) unen SKUs distintos que son la misma pieza en otra presentación.
- `variantAttributeSlug` es texto libre opcional: le indica al storefront qué atributo usar para pintar el selector.
- CRUD con `search`. `DELETE` da `409` si el grupo tiene productos.
- `GET` / `PUT` / `PATCH .../{uuid}/products`: patrón común. El `PATCH` con `add` mueve el producto aunque ya estuviera en otro grupo; con `remove` solo lo quita si sigue en este grupo.
- `PATCH /v1/admin/products/{productUuid}/variant-group` `{ variantGroupUuid | null }` → `{ productUuid, variantGroupUuid }`.

### Información propia del producto

`PATCH /v1/admin/products/{productUuid}/info` `{ description?, brand?, bulletPoints?, technicalSpecs?, contents? }`.
Es parcial: un campo omitido no se toca y uno mandado como `null` se borra. Responde los cinco campos
con su valor final. Todos son texto libre (`\n` para listas). `brand` se recorta y `""` cuenta como
`null`. `description` también aparece en los listados del storefront; los otros cuatro solo en el
detalle. `404` si el producto no existe.

### Envío cobrado en el checkout

Cuando el cliente pagó el envío, el pedido trae `deliveryCost` (ya incluido en `total`) y `shippingQuote`:
`{ quoteUuid, status: "QUOTED" | "MANUAL", reason, optionId, carrier, service, serviceDescription, deliveryEstimate, carrierPrice, price, currency, packages }`.
- `QUOTED`: el cliente eligió y pagó esa paquetería/servicio. `carrierPrice` es la tarifa de envia.com; `price` lo cobrado (con margen). `packages`: las cajas estimadas (`weightKg`, `lengthCm`, `widthCm`, `heightCm`, `pieces`).
- `MANUAL`: el envío no se cobró (`reason`: `MISSING_DIMENSIONS`, `NO_RATES`, `UPSTREAM_ERROR`); se cotiza a mano como antes.
- `null`: recoger en tienda o pedido anterior.

En `POST .../shipping/quote` y `.../shipping/generate`, `weight/length/width/height` ahora son opcionales (las cuatro o ninguna, `422` si vienen incompletas): omitidas, se usan las cajas de `shippingQuote.packages`. En `generate`, `carrier`/`service` omitidos toman los que eligió el cliente (`422` si el pedido no tiene opción elegida). Si la guía real cuesta distinto de `carrierPrice`, la diferencia la absorbe la tienda (queda en el log). `shippingLabel.packages` lista todas las cajas cuando hubo más de una.

### Peso y medidas de empaque

Para cotizar envíos con datos reales del producto. Son las del **empaque de una pieza** (unidad de venta).

- `PUT /v1/admin/products/{productUuid}/package-dimensions` `{ weightKg, lengthCm, widthCm, heightCm }`: las cuatro son obligatorias (una cotización no sirve con medidas parciales) y reemplazan las anteriores. Cada una `> 0`; tope 1500 kg y 600 cm por lado. Se redondean a 3 decimales (kg) y 2 (cm); un valor positivo más chico sube al mínimo (0.001 kg / 0.01 cm). Responde `{ productUuid, packageDimensions: { weightKg, lengthCm, widthCm, heightCm } }`. `422` si alguna falta o está fuera de rango, `404` si el producto no existe.
- `DELETE /v1/admin/products/{productUuid}/package-dimensions` → `204`, idempotente.
- Para leerlas: `packageDimensions` en `GET /v1/products/{uuid}` (`null` si no tiene).
- Carga masiva: hoja `Medidas` de la importación por Excel (abajo).

### Marcas

Todas comparan sin distinguir mayúsculas y trabajan sobre productos no eliminados. Para filtrar
"Sin marca" en el storefront existe `hasBrand: false`.

| Ruta | Notas |
|---|---|
| `GET /v1/admin/brands` | `{ docs: [{ name, productCount, variants }], unbrandedCount }`. Si `variants` trae más de una grafía, es un duplicado que conviene fusionar |
| `PATCH /v1/admin/products/brand` `{ productUuids (1-500), brand \| null }` | → `{ updated, notFound }` |
| `POST /v1/admin/brands/rename` `{ from, to }` | Renombra o fusiona (también sirve para corregir mayúsculas) → `{ updated }`. `404` si `from` no existe; `422` si alguno viene vacío |
| `DELETE /v1/admin/brands?name=X` | Quita la marca a todos sus productos → `{ updated }`. Idempotente; `422` si falta `name` |

### Precio de oferta

`compareAtPrice` es el precio **anterior** con IVA (el tachado). El precio de venta sigue siendo
el `price` de Sicar X; lo normal es bajarlo allá y capturar aquí el anterior. La oferta se apaga
sola al pasar `saleEndsAt` o si `price` sube hasta igualar `compareAtPrice`. Cualquier admin
puede usar estas rutas. `saleEndsAt` es opcional, debe ser futura y, sin zona horaria, se toma
como UTC.

- `PUT /v1/admin/products/{productUuid}/sale` `{ compareAtPrice, saleEndsAt? }` → `{ productUuid, price, compareAtPrice, saleEndsAt, isOnSale }`. `422` si `compareAtPrice ≤ price` o la fecha ya pasó.
- `DELETE /v1/admin/products/{productUuid}/sale`: `204`, idempotente.
- `PUT /v1/admin/products/sale`: exactamente uno de `productUuids` (1-500) o `categoryUuid` (incluye descendientes), **y** exactamente uno de `compareAtPrice` o `compareAtPercent` (0 < p < 100; precio anterior = `price / (1 - p/100)`) → `{ updated, notFound, skipped }`. `skipped` son productos donde no quedaría mayor que `price`, o con `price` en 0.
- `DELETE /v1/admin/products/sale` `{ productUuids | categoryUuid }` → `{ updated, notFound }`.

### Sinónimos del buscador

El motor ya tolera typos, plurales, acentos y unidades. Los sinónimos son para palabras
distintas con el mismo significado y se aplican sin reindexar.

- **Multidirección** (sin `root`): todas las palabras se encuentran entre sí (`allen` ↔ `hexagonal`).
- **Una dirección** (con `root`): buscar `root` también trae las palabras de `synonyms`, pero no al revés (`inalambrico` → `bateria`, `20v`).
- Las palabras se normalizan como las búsquedas (`"Baterías"` → `bateria`, `"20 V"` → `20v`) y la respuesta devuelve la forma guardada. Se permiten frases.

Objeto: `{ uuid, root, synonyms, kind: "MULTI_WAY" | "ONE_WAY", createdAt, updatedAt }`.

| Ruta | Notas |
|---|---|
| `GET /v1/admin/search/synonyms?q=&limit=&offset=` | `q` busca en la raíz y en las palabras; los más recientes primero |
| `POST /v1/admin/search/synonyms` `{ root?, synonyms }` | `201` + `syncedToSearch` |
| `PATCH /v1/admin/search/synonyms/{uuid}` | Parcial; `root: null` explícito la vuelve multidirección |
| `DELETE /v1/admin/search/synonyms/{uuid}` | **super_admin**, `204` |

- `422`: multidirección con menos de 2 palabras distintas ya normalizadas, raíz repetida en su propia lista, lista vacía, más de 20 palabras o alguna de más de 50 caracteres.
- `409` si ya existe una entrada idéntica.
- `syncedToSearch: false` no es un error: se guardó y se aplica en ≤ 5 min. Muestra "Guardado; se aplicará en unos minutos".

### Importación masiva por Excel

- `GET /v1/admin/bulk-import/template`: descarga `plantilla_importacion_masiva.xlsx` con las mismas hojas y columnas que espera la carga, comentarios y filas de ejemplo ficticias (bórralas antes de subir).
- `POST /v1/admin/bulk-import/products` (`multipart/form-data`, campo `file`): cualquier subconjunto de estas hojas:

| Hoja | Columnas | Semántica al re-subir |
|---|---|---|
| `Categorias` | `sku`, `categorySlug` (uno o varios separados por `,` o `;`) | Aditiva |
| `Vehiculos` | `sku`, `make`, `model`, `year` (un año), `vehicleType?`, `engine?` | Aditiva; sin `engine` aplica a todos los motores |
| `Atributos` | `sku`, `attributeSlug`, `value` | Merge (un valor corregido sí se aplica) |
| `Variantes` | `sku`, `variantGroupSlug` (slug del `name` del grupo) | Reemplaza (gana la última fila) |
| `InfoProducto` | `sku` + cualquiera de `description`, `brand`, `bulletPoints`, `technicalSpecs`, `contents` | Merge por campo. Una celda vacía no toca nada; el texto `NULL` borra el campo. Alt+Enter para saltos de línea |
| `Medidas` | `sku`, `weightKg`, `lengthCm`, `widthCm`, `heightCm` (las cuatro obligatorias) | Reemplaza (gana la última fila). Acepta `16 cm` o coma decimal (`4,29`). Fila inválida → `INVALID_DIMENSIONS`. Para borrar, usa el `DELETE` de arriba |

- `sku` se busca en `Product.sku` y luego en `additionalSkus`, sin distinguir mayúsculas (igual que `make`, `model` y `engine`). `categorySlug` sí distingue mayúsculas.
- En `BOOLEAN` se aceptan `TRUE`/`FALSE`, `si`/`no` y `1`/`0`.
- Subir el mismo archivo dos veces es seguro.
- **Éxito parcial**: las filas inválidas se reportan y no detienen las demás.

```json
{
  "categories":  { "found": true,  "processedRows": 120, "assignedCount": 118, "errors": [ { "sheet": "Categorias", "row": 47, "reasonCode": "SKU_NOT_FOUND", "reason": "SKU no encontrado: ABC-999.", "sku": "ABC-999" } ] },
  "vehicles":    { "found": true,  "processedRows": 340, "assignedCount": 336, "errors": [] },
  "attributes":  { "found": true,  "processedRows": 80,  "assignedCount": 78,  "errors": [] },
  "variants":    { "found": false, "processedRows": 0,   "assignedCount": 0,   "errors": [] },
  "productInfo": { "found": true,  "processedRows": 45,  "assignedCount": 52,  "errors": [] }
}
```

- `found: false` = la hoja no venía.
- `assignedCount` significa: vínculos nuevos en categorías y vehículos; pares producto–campo escritos en atributos e info (puede superar el número de filas); productos asignados en variantes.
- `reasonCode`: `MISSING_FIELDS`, `SKU_NOT_FOUND`, `CATEGORY_SLUG_NOT_FOUND`, `INVALID_YEAR`, `VEHICLE_NOT_FOUND`, `ATTRIBUTE_SLUG_NOT_FOUND`, `VALUE_TYPE_MISMATCH`, `VARIANT_GROUP_SLUG_NOT_FOUND`.
- Rechazan el archivo completo: `400` si no es un `.xlsx` válido, pesa más de 4 MB, no trae ninguna de las hojas o alguna hoja pasa de 20,000 filas; `422` si a una hoja le falta una columna requerida.

---

## Cupones

- `discountType`: `PERCENTAGE` (con tope opcional `maxDiscountAmount`) o `FIXED_AMOUNT`.
- `scopeType`: `ORDER`, `CATEGORY` (incluye descendientes) o `PRODUCT`. Un cupón `CATEGORY`/`PRODUCT` sin alcance asignado no descuenta nada.
- Límites combinables: `maxTotalUses`, `maxUsesPerClient`, `firstPurchaseOnly` y una lista de clientes elegibles (vacía = público). También `startsAt`, `endsAt` y `minOrderAmount`.
- **Anti-abuso**: cuando un pedido llega a `PAID`, el uso queda consumido para siempre, aunque después se cancele o se reembolse. Solo un pedido cancelado mientras estaba `TO_PAY` libera el uso.

Objeto: `{ uuid, code, discountType, discountValue, maxDiscountAmount, scopeType, minOrderAmount, startsAt, endsAt, isActive, maxTotalUses, maxUsesPerClient, firstPurchaseOnly, createdAt, updatedAt }`.

| Ruta | Notas |
|---|---|
| `POST /v1/admin/coupons` | `201`. `code` se guarda en mayúsculas y sin espacios. `422` si mandas `maxDiscountAmount` con `FIXED_AMOUNT` o un `PERCENTAGE` > 100; `409` si el código está duplicado |
| `GET /v1/admin/coupons?code=&isActive=` | `code` es parcial, sin distinguir mayúsculas |
| `GET` / `PATCH /v1/admin/coupons/{uuid}` | `PATCH` parcial, con las mismas validaciones |
| `DELETE /v1/admin/coupons/{uuid}` | **super_admin**, `204`. `409` si tiene algún uso registrado (para retirarlo usa `PATCH {isActive: false}`). Borra en cascada su alcance y sus clientes |
| `PUT` / `PATCH` / `GET .../{uuid}/categories` | `{ categoryUuids }`. `409` si `scopeType` no es `CATEGORY`. `GET` sin paginar |
| `PUT` / `PATCH` / `GET .../{uuid}/products` | `{ productUuids }`. `409` si no es `PRODUCT`. `GET` paginado |
| `PUT` / `PATCH` / `GET .../{uuid}/clients` | Por **email** (`clientEmails`). `404` si un email no existe. `GET` paginado `{ uuid, email, name }` |
| `GET .../{uuid}/redemptions` | `{ id, clientAccountId, clientEmail, orderUuid, status, createdAt, updatedAt }`. `status`: `PENDING` (en espera de pago), `CONFIRMED` (consumido), `RELEASED` (cancelado antes de pagar) |

## Dashboard de ventas

`startDate` y `endDate` (`YYYY-MM-DD`, inclusivos; por defecto los últimos 30 días). Solo cuenta pedidos `PAID` no borrados.

- `GET /v1/admin/dashboard/summary` → `{ startDate, endDate, totalRevenue, orderCount, averageOrderValue, totalUnitsSold, daily: [{ date, revenue, orderCount, unitsSold }] }`. `daily` solo trae días con ventas (rellena los huecos con 0).
- `GET /v1/admin/dashboard/top-products?sortBy=revenue|quantity&limit=20&offset=0` (`limit` 1-100) → `{ total, docs: [{ productUuid, sku, name, imageUrl, unitsSold, revenue }] }`. `total` = productos distintos. Nombre, SKU e imagen son los del momento de la venta.
- `GET /v1/admin/dashboard/top-categories` (mismos parámetros) → `{ total, docs: [{ categoryUuid, categoryName, unitsSold, revenue }] }`. Usa el árbol propio. Un producto en varias categorías suma completo en cada una, así que **no sumes las filas** para obtener un total (usa `summary`). Los productos sin categoría no aparecen.

## Reseñas (moderación)

Objeto: `{ uuid, productUuid, clientUuid, clientEmail, clientName, rating, comment, isVerifiedPurchase, isHidden, hiddenReason, helpfulCount, adminReply, adminReplyAt, createdAt, updatedAt }`.

| Ruta | Notas |
|---|---|
| `GET /v1/admin/reviews` | Filtros: `productUuid`, `productSku` (exacto, sin distinguir mayúsculas), `clientEmail`, `clientUuid`, `rating`, `isHidden`, `hasReply`. Incluye las ocultas |
| `PATCH /v1/admin/reviews/{uuid}` `{ isHidden?, hiddenReason? }` | Ocultar la saca de la vista pública y del promedio (se recalcula al momento). Es reversible; el autor la sigue viendo |
| `DELETE /v1/admin/reviews/{uuid}` | Borrado permanente (`204`), para contenido abusivo |
| `PUT` / `DELETE /v1/admin/reviews/{uuid}/reply` `{ comment }` | Una sola respuesta oficial por reseña (el `PUT` la reemplaza) |

## Clientes: historial de consentimiento de marketing

`GET /v1/admin/clients/{clientUuid}/consents` (cualquier admin; `404` si la cuenta no existe). Sirve para solicitudes ARCO o quejas. El `clientUuid` viene en los pedidos.

```json
{
  "clientUuid": "7f3c…", "email": "ana@example.com", "name": "Ana López",
  "marketingEmails": false, "marketingConsentAt": "2026-11-02T09:14:55Z",
  "events": [ { "value": false, "source": "one_click_unsubscribe", "privacyNoticeVersion": null, "ipAddress": "189.203.11.40", "userAgent": "Mozilla/5.0 …", "changedAt": "2026-11-02T09:14:55Z" } ]
}
```

- `events`: historial completo, del más reciente al más antiguo, sin paginar y sin posibilidad de editarse. `marketingEmails` siempre coincide con el primer evento; `changedAt` está en UTC.
- `source`: `register_checkbox`, `google_signup`, `account_toggle`, `email_unsubscribe`, `one_click_unsubscribe`, `admin` (reservado, hoy sin uso) y `migrated` (estado previo al historial, sin versión, IP ni user agent).
- `privacyNoticeVersion`, `ipAddress` y `userAgent` pueden venir en `null`.
