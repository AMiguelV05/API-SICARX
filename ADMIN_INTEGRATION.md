# Conectando el dashboard admin a API SICARX

Guía práctica para quien construya el dashboard administrativo (dentro del mismo dominio que el
frontend de Ferretería Charly, `https://ferreteriacharly.com/`): cómo autenticarse contra
`/v1/admin/*` y qué hace cada endpoint. **No confundir con `FRONTEND_INTEGRATION.md`** — ese
documento es para el storefront de cara al comprador; este es exclusivamente para el panel admin,
un consumidor completamente distinto de esta misma API.

## URLs base

| Entorno | URL |
|---|---|
| Producción (Railway) | `https://api-production-cf7a.up.railway.app` |
| Local (dev) | `http://127.0.0.1:8000` (o el puerto que uses con `uvicorn --reload`) |

## Arquitectura obligatoria: server-to-server, nunca desde el navegador

**`X-Admin-Key` es un secreto estático compartido, sin alcance por usuario** — no es un JWT de
sesión ni tiene expiración ni identifica a un admin en particular, solo confirma que la llamada
viene de un sistema autorizado. Por eso **esta llave nunca debe llegar al navegador**: sea cual
sea el framework del dashboard, la llamada a `/v1/admin/*` debe hacerse desde el backend/servidor
del dashboard (p. ej. un Route Handler o Server Action de Next.js), no desde JavaScript del lado
del cliente. Si `X-Admin-Key` viajara en una petición hecha directamente desde el navegador,
cualquiera con acceso a las devtools del dashboard podría copiarla y llamar a `/v1/admin/*`
directamente sin pasar por ningún control de acceso propio del dashboard.

Como consecuencia de ser 100% server-to-server, **CORS no aplica aquí** — no hay ninguna petición
cross-origin del navegador hacia `api-production-cf7a.up.railway.app` para estas rutas, así que no
hace falta agregar nada a `allow_origins`/`allow_headers` en `app/main.py` para este flujo (esos
ya cubren el storefront, un caso distinto).

## Autenticación — una sola cabecera, distinta de las del storefront

```http
X-Admin-Key: <valor de ADMIN_API_KEY>
```

Es la **única** cabecera de autenticación que exige `/v1/admin/*` — a diferencia de cada ruta del
storefront (que siempre exige `x-api-key`), este router **no** valida `x-api-key` en absoluto, solo
`X-Admin-Key`. No reutilices la `x-api-key` del storefront aquí ni esperes que haga falta.

- `401` si falta la cabecera, o si `X-Admin-Key` no coincide con el valor configurado.
- El valor de `ADMIN_API_KEY` se entrega **fuera de este repo**, por un canal seguro aparte
  (mismo criterio que `FRONTEND_WEBHOOK_SECRET` en `FRONTEND_INTEGRATION.md`) — nunca se
  documenta el valor real aquí ni en ningún otro archivo del repo.
- Si `ADMIN_API_KEY` no está configurado del lado del backend (`Optional`, sin valor por
  defecto), **todas** las rutas de `/v1/admin/*` responden `401` sin importar qué se envíe —
  es el comportamiento seguro-por-defecto mientras no exista un valor real que verificar.

## Referencia de endpoints

### `GET /v1/admin/health` — estado operativo

```http
GET /v1/admin/health
X-Admin-Key: <admin-key>
```

Respuesta `200`:
```json
{
  "databaseOk": true,
  "sicarTokenPresent": true,
  "outboxCounts": { "PENDING": 0, "IN_PROGRESS": 0, "SUCCEEDED": 12, "FAILED": 0 }
}
```

`databaseOk` — resultado de un `SELECT 1` contra Postgres. `sicarTokenPresent` — si el proceso
que atendió la petición actualmente sostiene un token administrativo de Sicar X en memoria (no
confirma que el token sea válido, solo que existe uno). `outboxCounts` — conteo de
`sicar_sync_outbox` agrupado por `status`, útil para ver de un vistazo si hay algo atorado.

### `GET /v1/admin/sync/catalog-status` — última corrida del sync de catálogo

```http
GET /v1/admin/sync/catalog-status
X-Admin-Key: <admin-key>
```

Respuesta `200`:
```json
{
  "lastRunStartedAt": "2026-07-29T15:00:03Z",
  "lastRunFinishedAt": "2026-07-29T15:04:47Z",
  "lastSuccessAt": "2026-07-29T15:04:47Z",
  "productsProcessed": 124813,
  "productsDeactivated": 3,
  "lastError": null
}
```

Si el worker de sync nunca ha corrido en ese ambiente (base de datos recién migrada), responde
`200` con todos los campos en `null` — no es un error, es un estado válido ("todavía no hay
corridas registradas"). `lastSuccessAt` **no se pisa** por una corrida fallida posterior —
`lastRunFinishedAt`/`lastError` sí reflejan siempre la corrida más reciente, exitosa o no, pero
`lastSuccessAt` solo avanza en un éxito real. Esto reemplaza tener que revisar `sync.log`
directamente (invisible en Railway, ver `CLAUDE.md`) para confirmar que el catálogo se sigue
sincronizando.

### `GET /v1/admin/sync/outbox` — cola de sincronización pendiente/fallida con Sicar X

```http
GET /v1/admin/sync/outbox?status=PENDING&status=FAILED&limit=50&offset=0
X-Admin-Key: <admin-key>
```

- `status` (repetible, opcional) — uno o más de `PENDING`/`IN_PROGRESS`/`SUCCEEDED`/`FAILED`. Si se
  omite, por defecto excluye `SUCCEEDED` (`PENDING`, `IN_PROGRESS`, `FAILED`) — normalmente lo que
  interesa ver es "qué sigue sin resolverse", no el historial completo.
- `limit` (1-200, default 50), `offset` (≥0, default 0).

Respuesta `200`:
```json
{
  "total": 1,
  "docs": [
    {
      "id": 42,
      "orderId": 187,
      "action": "ACCEPT",
      "status": "FAILED",
      "attempts": 5,
      "lastError": "Sicar X rechazo el avance de dispatchStatus a 'PENDING' para el documento ...: 409 - ...",
      "nextAttemptAt": "2026-07-29T16:10:00Z",
      "createdAt": "2026-07-29T15:50:00Z",
      "updatedAt": "2026-07-29T16:09:12Z"
    }
  ]
}
```

`action` es `"CANCEL"` (cancelación de un pedido), `"ACCEPT"` (avisar a Sicar X que
`dispatchStatus` ya avanzó de `PENDING_ACCEPTANCE` a `PENDING`, ver `POST .../accept` abajo),
`"DISPATCH"` (avisar a Sicar X que `dispatchStatus` ya avanzó a `DISPATCHED` tras generar una guía
de envío, ver `POST .../shipping/generate` más abajo), o `"SYNC_DISPATCH_STATUS"` (avisar a Sicar X
de cualquier otro avance/reversión de `dispatchStatus` disparado por `POST .../advance-status`, ver
más abajo — el `dispatchStatus` objetivo viaja en la fila misma, no está hardcodeado como en las
otras tres acciones). En los cuatro casos, el estado local (`dispatchStatus`, autoritativo desde el
dashboard admin) ya se aplicó de inmediato antes de encolar la fila — esta cola es puramente para
avisarle a Sicar X, nunca para decidir el estado. Una fila `FAILED`
significa que se agotaron los 5 intentos con backoff exponencial (1/2/4/8/16 min) — el pedido
sigue correcto del lado local, pero Sicar X todavía no se enteró; hace falta reconciliar manualmente
en el panel nativo de Sicar X, o reintentar con el siguiente endpoint una vez resuelta la causa.

### `POST /v1/admin/sync/outbox/{id}/retry` — reintentar una fila FAILED

```http
POST /v1/admin/sync/outbox/42/retry
X-Admin-Key: <admin-key>
```

Resetea la fila a `PENDING` con `nextAttemptAt` = ahora, para que el worker (corre cada minuto) la
vuelva a intentar en su siguiente ciclo. `404` si el `id` no existe o la fila no está en `FAILED`
(no tiene sentido "reintentar" una fila que ya está `PENDING`/`IN_PROGRESS`/`SUCCEEDED`). Responde
`200` con la fila actualizada (mismo shape que arriba).

### `GET /v1/admin/orders` — búsqueda de pedidos (todas las cuentas)

```http
GET /v1/admin/orders?status=TO_PAY&dispatchStatus=PENDING_ACCEPTANCE&limit=50&offset=0
X-Admin-Key: <admin-key>
```

A diferencia de `GET /v1/auth/me/orders` (storefront, acotado a una sola cuenta), esta ruta busca
sobre **todas** las cuentas. Filtros opcionales, todos combinables con `AND`:

- `status` — `TO_PAY`/`PAID`/`CANCELLED` (estado local de pago/cancelación).
- `dispatchStatus` — `PENDING_ACCEPTANCE`/`PENDING`/`PREPARING`/`COMPLETE`/`DISPATCHED` (estado de
  cumplimiento real de Sicar X).
- `clientEmail` — coincidencia exacta (no `ILIKE`), normalizado a minúsculas antes de comparar.
- `clientUuid` — el `uuid` público de `ClientAccount`, no el `id` interno.
- `includeDeleted` (bool, default `false`) — si `true`, incluye pedidos soft-deleted
  (`DELETE /v1/orders/{id}` desde el storefront) que de otra forma quedan ocultos.
- `limit` (1-200, default 50), `offset` (≥0, default 0).

Respuesta `200` — mismo shape que el detalle de abajo, dentro de `docs`, ordenado por
`createdAt` descendente:
```json
{
  "total": 3,
  "docs": [ { "uuid": "...", "sicarOrderId": "...", "...": "..." } ]
}
```

### `GET /v1/admin/orders/{orderUuid}` — detalle de un pedido (cualquier cuenta)

```http
GET /v1/admin/orders/f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6?includeDeleted=false
X-Admin-Key: <admin-key>
```

Respuesta `200`:
```json
{
  "uuid": "f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6",
  "sicarOrderId": "6a55165ada77fe7cd25d39e3",
  "serieFolio": "TL518",
  "status": "TO_PAY",
  "dispatchStatus": "PENDING_ACCEPTANCE",
  "dispatchHistory": null,
  "total": 129.99,
  "totalQuantity": 3,
  "deliveryInfo": { "contactInfo": { "name": "Juan Pérez", "phone": "3151234567", "email": "juan@example.com" }, "deliveryType": "PICKUP" },
  "items": [ { "uuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "sku": "PR2057", "description": "PORTAROLLO", "quantity": "1", "unit": "PZA", "imageUrl": "https://.../portarollo.jpg" } ],
  "createdAt": "2026-07-10T18:32:05Z",
  "deletedAt": null,
  "clientEmail": "juan@example.com",
  "clientName": "Juan Pérez",
  "acceptedAt": null,
  "acceptedBy": null,
  "deliveryCompany": null,
  "deliveryAssignedAt": null
}
```

Mismo shape base que `GET /v1/auth/me/orders/{orderUuid}` en el storefront (ver
`FRONTEND_INTEGRATION.md`), más los campos exclusivos de este panel: `clientEmail`/`clientName`
(resueltos igual que en el webhook `order-confirmed`), `deletedAt` (el storefront nunca lo
expone), y los cuatro campos nuevos de aceptación/mensajería (`acceptedAt`/`acceptedBy`/
`deliveryCompany`/`deliveryAssignedAt`). `404` si el `orderUuid` no existe (o está soft-deleted y
no se mandó `includeDeleted=true`) — esta ruta no filtra por dueño, así que un `orderUuid` válido
de cualquier cliente siempre resuelve.

### `POST /v1/admin/orders/{orderUuid}/accept` — aceptar un pedido

```http
POST /v1/admin/orders/f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6/accept
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "acceptedBy": "Miguel" }
```

`acceptedBy` es opcional y es **texto libre** (no hay todavía un sistema de usuarios admin real
detrás de `X-Admin-Key` — es un identificador para auditoría, no una FK a nada). Respuesta `200`:

```json
{
  "orderUuid": "f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6",
  "acceptedAt": "2026-07-29T16:36:45Z",
  "acceptedBy": "Miguel",
  "dispatchStatus": "PENDING",
  "syncStatus": "QUEUED",
  "note": "La aceptación ya se aplicó localmente (dispatchStatus = PENDING); se le avisa a Sicar X de forma asíncrona via sicar_sync_outbox."
}
```

**`dispatchStatus` ya es `PENDING` en esta misma respuesta** — `acceptedAt`/`acceptedBy` y
`dispatchStatus` se aplican de inmediato en Postgres (el dashboard admin es la única fuente de
verdad de este campo; Sicar X ya nunca lo sobreescribe — ver `CLAUDE.md`). Lo único asíncrono es
avisarle a Sicar X: eso ocurre en el siguiente ciclo del worker (cada minuto), vía la misma cola
`sicar_sync_outbox` que ya usa la cancelación. Si sospechas que ese aviso no llegó, consulta
`GET /v1/admin/sync/outbox?status=FAILED`. `404` si el pedido no existe (o está soft-deleted). `409`
si el pedido ya fue aceptado antes (`acceptedAt` ya tenía un valor) — no se puede "re-aceptar".

### `POST /v1/admin/orders/{orderUuid}/advance-status` — avanzar (o revertir) el estado de cumplimiento

```http
POST /v1/admin/orders/f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6/advance-status
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "dispatchStatus": "PREPARING" }
```

Cubre las transiciones que `/accept` y `/shipping/generate` no cubren — el dashboard es la única
fuente de verdad de `dispatchStatus`, así que este es el endpoint genérico para moverlo. Transiciones
legales (`dispatchStatus` actual → objetivos permitidos en esta llamada):

| Actual | Objetivos permitidos |
|---|---|
| `PENDING` | `PREPARING` |
| `PREPARING` | `PENDING` (revertir), `COMPLETE` |
| `COMPLETE` | `PREPARING` (revertir), `DISPATCHED`* |
| `DISPATCHED` | `COMPLETE` (revertir)** |

\* Solo para pedidos `DELIVERYMAN` — un `PICKUP` no tiene paso de envío, `COMPLETE` ya es su estado
terminal (el dashboard lo puede etiquetar "Listo para recoger", pero no hay una transición de
backend distinta para eso).

\*\* Bloqueado (`409`) si el pedido ya tiene `shippingLabel` real generado con envia.com — una guía
ya generada (y cobrada) no es reversible desde aquí; solo un `DISPATCHED` alcanzado manualmente por
este mismo endpoint puede revertirse.

`PENDING_ACCEPTANCE` no aparece en la tabla — salir de ahí sigue siendo trabajo exclusivo de
`/accept` (Sicar X nunca acepta `PENDING_ACCEPTANCE` como destino, así que no hay nada que revertir
hacia ese estado).

Respuesta `200`:
```json
{
  "orderUuid": "f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6",
  "dispatchStatus": "PREPARING",
  "syncStatus": "QUEUED",
  "note": "El nuevo estado ya se aplicó localmente; se le avisa a Sicar X de forma asíncrona via sicar_sync_outbox."
}
```

Igual que `/accept`, `dispatchStatus` ya queda aplicado en Postgres en esta misma respuesta — solo
avisarle a Sicar X es asíncrono (`action: "SYNC_DISPATCH_STATUS"` en `sicar_sync_outbox`). Si el
pedido ya tiene una fila `SYNC_DISPATCH_STATUS` pendiente (p. ej. el dashboard avanzó dos pasos
seguidos antes de que el worker procesara el primero), esta llamada la actualiza al nuevo objetivo
en vez de encolar una segunda — a Sicar X solo le importa el estado final, no cada paso intermedio.

`404` si el pedido no existe (o está soft-deleted). `409` si `status == "CANCELLED"`, si la
transición no está en la tabla de arriba, si se pide `DISPATCHED` sobre un `PICKUP`, o si se intenta
revertir un `DISPATCHED` respaldado por una guía real de envia.com.

Dispara una notificación al storefront (webhook `order-status-changed`, ver
`FRONTEND_INTEGRATION.md`) solo en dos casos: llegar a `COMPLETE` en un pedido `PICKUP`
("listo para recoger"), y llegar a `DISPATCHED` ("enviado"). No hay notificación al llegar a
`PREPARING`, ni al llegar a `COMPLETE` en un pedido `DELIVERYMAN` (etapa interna, el cliente no
tiene nada que hacer todavía), ni en ninguna reversión.

### `POST /v1/admin/orders/{orderUuid}/assign-delivery` — asignar mensajería/paquetería

```http
POST /v1/admin/orders/f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6/assign-delivery
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "deliveryCompany": "Estafeta" }
```

`deliveryCompany` es **texto libre obligatorio** (no valida contra ningún catálogo de
paqueterías). Respuesta `200`:
```json
{
  "orderUuid": "f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6",
  "deliveryCompany": "Estafeta",
  "deliveryAssignedAt": "2026-07-29T16:40:12Z"
}
```

**Esto es metadato puramente local** — no dispara ninguna llamada a una API de paquetería
externa (no genera guía, no calcula tarifa, no notifica al repartidor). Es solo "quién quedó
encargado de esta entrega", para que el dashboard lo muestre. `404` si el pedido no existe (o
está soft-deleted). Se puede llamar varias veces sobre el mismo pedido (reasigna sin error) — a
diferencia de `/accept`, no hay noción de "ya asignado, no se puede reasignar".

### Guía de envío con envia.com

Pantalla "Guía de envío" del panel admin: cotizar + generar una guía real con envia.com una vez
que un pedido `DELIVERYMAN` llega a `dispatchStatus: COMPLETE`.

**Cambio a `GET /v1/admin/orders/{orderUuid}`** — dos campos nuevos en la respuesta:
- `deliveryAddress: Address | null` — la dirección de destino resuelta para pedidos
  `DELIVERYMAN` (mismo shape que `ClientAddressPublic`: `uuid, label, street, extNumber,
  intNumber, neighborhood, city, county, state, country, zipCode, references, latitude,
  longitude, isDefault`), `null` para `PICKUP`.
  **Es una foto fija**, no un join en vivo contra la libreta de direcciones del cliente:
  `routes/orders.py` la captura (`Order.delivery_address_snapshot`) en el momento de crear el
  pedido, justo después de resolver la dirección vía `address_service.get_owned_address` — el
  cliente puede editar/borrar esa dirección después de ordenar, y generar una guía contra una
  dirección ya cambiada sería un bug silencioso. Distinta de
  `deliveryInfo.contactInfo.address` (la forma que exige Sicar X, sin `uuid`/`label`/coordenadas)
  — ambas se snapshotean por separado en el mismo momento. `/shipping/quote` y
  `/shipping/generate` usan `deliveryAddress` como destino internamente.
- `shippingLabel: ShippingLabelInfo | null` — `null` hasta que `/shipping/generate` tenga éxito
  una vez; ver shape abajo.

#### `POST /v1/admin/orders/{orderUuid}/shipping/quote` — cotizar opciones de envío

```http
POST /v1/admin/orders/f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6/shipping/quote
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "weight": 2.5, "length": 30, "width": 20, "height": 15 }
```

El backend resuelve `destination` desde `deliveryAddress` del pedido y `origin` desde una
constante propia (dirección de la tienda/almacén, configurada en este servicio, no en el
frontend), arma `packages[]` (`weightUnit: "KG"`, `lengthUnit: "CM"`) y llama a
`POST /ship/rate/` de envia.com (`Authorization: Bearer <token de envia, en el env de este
servicio>`). No persiste nada — se puede llamar repetidamente mientras el admin ajusta peso/medidas.

**Override opcional del origen** — el dashboard puede mandar un objeto `origin` con
cualquier subconjunto de sus campos; cualquier campo omitido (o mandado vacío/`null`) cae
de vuelta al valor fijo configurado en este servicio, campo por campo — no es "todo o
nada". Útil para cotizar/enviar desde una sucursal o dirección distinta a la configurada
por defecto, sin tocar las variables de entorno:

```json
{
  "weight": 2.5, "length": 30, "width": 20, "height": 15,
  "origin": { "city": "Guadalajara", "state": "Jalisco" }
}
```

Campos disponibles en `origin` (todos opcionales): `name, company, phone, email, street,
number, district, city, state, zipCode, reference` — mismos nombres que
`deliveryAddress`/`ClientAddressPublic` donde aplica. `state` pasa por la misma
normalización que ya se le aplica al valor de `.env` (nombre completo → código corto de 3
letras que envia.com exige) — no hace falta que el admin sepa el código, un nombre como
`"Jalisco"` o `"Ciudad de México"` funciona igual. `country`/`phone_code` no son
overrideables (el negocio solo envía dentro de México). El mismo objeto `origin` es
aceptado por `/shipping/generate` más abajo, con el mismo comportamiento.

Respuesta `200`:
```json
{
  "options": [
    {
      "carrier": "dhl",
      "service": "1",
      "serviceDescription": "DHL Standard",
      "deliveryEstimate": "3-5 días",
      "totalPrice": 185.50,
      "currency": "MXN"
    }
  ]
}
```

- `options: []` es una respuesta válida (`200`), no un error — significa que ningún carrier
  cubre ese código postal con ese paquete.
- Solo se devuelven opciones **puerta a puerta** (`dropOff: 0` del lado de envia.com).
  Algunos carriers/servicios exigen que el origen o el destino sea una sucursal física
  (p. ej. paquetexpress `ground_do`/`ground_od`) — envia.com pide un `branch_code`
  específico para esos, que este backend no recolecta ni soporta hoy (`/shipping/generate`
  fallaría con un `502` real de envia.com — `"Origin/Destination branch code is required"`
  — si se intentara). Se filtran aquí para que el admin nunca vea una opción que después no
  puede generarse.
- `404` si el pedido no existe. `409` si `deliveryType !== "DELIVERYMAN"` o
  `dispatchStatus !== "COMPLETE"` — el backend no confía en que el frontend ya lo validó. `422`
  si `deliveryAddress` falta o está incompleta (sin `street`/`city`/`state`/`zipCode`), o si
  `weight`/`length`/`width`/`height` faltan o no son positivos (validación de Pydantic vía
  `Field(gt=0)`, no un `400` a mano). `502` si envia.com falla, responde con error o rechaza la
  petición (token inválido, código postal no reconocido, etc.) — normalizado a
  `{"detail": "..."}` (`app/core/upstream_errors.py`, mismo helper que ya usa Sicar X/Mercado
  Pago), nunca el cuerpo crudo de envia.

#### `POST /v1/admin/orders/{orderUuid}/shipping/generate` — generar la guía

```http
POST /v1/admin/orders/f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6/shipping/generate
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "weight": 2.5, "length": 30, "width": 20, "height": 15, "carrier": "dhl", "service": "1" }
```

Re-resuelve origin/destination igual que `/quote` — incluyendo el mismo `origin` opcional
descrito arriba, si se manda aquí (usa el que se haya usado para la cotización elegida, no
necesariamente el mismo — este endpoint no recuerda nada de una llamada previa a `/quote`).
Llama a `POST /ship/generate/` de envia.com con el `service` elegido. Si tiene éxito, en una
sola transacción: persiste
`carrier, service, shipmentId, serviceDescription, trackingNumber, trackUrl, labelUrl (el
campo `label` de envia), totalPrice, currency, weight, length, width, height, generatedAt`
en el pedido, y avanza `dispatchStatus` de `COMPLETE` a `DISPATCHED`.

Respuesta `200`:
```json
{
  "orderUuid": "f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6",
  "dispatchStatus": "DISPATCHED",
  "shippingLabel": {
    "carrier": "dhl",
    "service": "1",
    "shipmentId": 179044,
    "serviceDescription": "DHL Standard",
    "trackingNumber": "1234567890",
    "trackUrl": "https://...",
    "labelUrl": "https://.../label.pdf",
    "totalPrice": 185.50,
    "currency": "MXN",
    "weight": 2.5,
    "length": 30,
    "width": 20,
    "height": 15,
    "generatedAt": "2026-07-29T18:10:00Z"
  }
}
```

`shipmentId` es el identificador que usa el dashboard de envia.com — es lo que hay que
buscar ahí para confirmar que el envío existe del lado de envia.com.

- `404` si el pedido no existe. `409` si `deliveryType !== "DELIVERYMAN"`,
  `dispatchStatus !== "COMPLETE"`, o si el pedido ya tiene `shippingLabel` (no hay regeneración
  por esta vía). `422` si `deliveryAddress` falta/está incompleta, o si las dimensiones no son
  válidas (mismo `Field(gt=0)` que `/quote`). `502` si envia.com falla (auth, `service` inválido,
  problema de cuenta con el carrier) — incluye el mensaje real de envia.com entre paréntesis.

Este endpoint **también avanza el estado en Sicar X de forma asíncrona vía `sicar_sync_outbox`**
(`action: "DISPATCH"`), igual que `ACCEPT`/`advance-status` — `dispatchStatus` se pone en
`"DISPATCHED"` **de inmediato** en Postgres, en la misma transacción que persiste `shippingLabel`
— el hecho real (guía generada, envia.com ya cobró) ya ocurrió en el momento en que este endpoint
responde `200`, mismo principio "Postgres local autoritativo de inmediato" que ya usa la
cancelación de pedidos y la confirmación de pago con Mercado Pago (ver `CLAUDE.md`). Avisarle a
Sicar X es la parte que queda asíncrona/best-effort — revisa `GET /v1/admin/sync/outbox?status=FAILED`
si sospechas que no se sincronizó. También dispara la misma notificación `order-dispatched` al
storefront que `/advance-status` dispara para un `DISPATCHED` alcanzado manualmente (ver
`FRONTEND_INTEGRATION.md`) — el cliente recibe el mismo mensaje sin importar cuál de los dos
caminos se usó.

**Advertencia de confiabilidad** (mismo espíritu que la nota de "no hay reintentos automáticos"
más abajo): esta llamada tiene un efecto real con costo — un timeout del lado del dashboard que
compite con un éxito del lado del servidor mostraría un error al admin mientras envia.com ya
generó (y cobró) una guía. Documentar esto como riesgo conocido, no agregar reintento automático
sobre timeout para "resolverlo".

### Categorías (taxonomía)

Endpoints para administrar el árbol propio de categorías (PIM, auto-referenciado vía
`parentUuid`, ver `CLAUDE.md` sección "Taxonomía") y para asignarle productos — la
única forma de poblar `product_categories`, que hasta ahora no tenía ningún endpoint
que la llenara. Para **leer** el árbol completo (navegación/filtros del storefront)
sigue usando `GET /v1/taxonomy` (público, solo necesita `x-api-key`) — estas rutas
son exclusivamente de escritura/administración, gateadas por `X-Admin-Key` como todo
lo demás en este documento.

#### `POST /v1/admin/categories` — crear un nodo

```http
POST /v1/admin/categories
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "name": "Herramientas Eléctricas", "parentUuid": "6a4fd308da77fe7cd25d1dd9" }
```

`parentUuid` es opcional — se omite (o se manda `null`) para crear un nodo raíz. El
`slug` **no se manda**, siempre se deriva automáticamente de `name` (minúsculas, sin
acentos, guiones — con un sufijo `-2`, `-3`, ... si ya existe otra categoría con el
mismo slug). `uuid` se genera localmente (`uuid4()`), igual que cualquier otro
identificador creado por este backend (p. ej. `Order.uuid`).

Respuesta `201`:
```json
{
  "uuid": "3f9a1c2e-...",
  "name": "Herramientas Eléctricas",
  "slug": "herramientas-electricas",
  "parentUuid": "6a4fd308da77fe7cd25d1dd9",
  "updatedAt": "2026-08-01T12:00:00Z"
}
```

`404` si `parentUuid` no existe.

#### `PATCH /v1/admin/categories/{uuid}` — renombrar y/o mover un nodo

```http
PATCH /v1/admin/categories/3f9a1c2e-.../
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "name": "Herramienta Eléctrica", "parentUuid": null }
```

Actualización parcial: solo los campos incluidos en el body se tocan. `parentUuid`
ausente del body deja el padre actual sin cambios; `parentUuid: null` explícito mueve
el nodo a raíz. Renombrar recalcula el `slug` automáticamente (mismo criterio que en
`POST`, no hay forma de fijar el slug a mano). Responde el mismo shape que `POST`.

- `404` si el nodo o el `parentUuid` indicado no existen.
- `409` si se intenta mover un nodo dentro de sí mismo o de su propio subárbol (un
  padre no puede ser también su propio descendiente — se valida contra el árbol
  completo, no solo contra el hijo directo).
- `422` si `name` se manda vacío.

#### `DELETE /v1/admin/categories/{uuid}` — eliminar un nodo

```http
DELETE /v1/admin/categories/3f9a1c2e-.../
X-Admin-Key: <admin-key>
```

`204` sin cuerpo si tiene éxito. Es un borrado real (no soft-delete — a diferencia de
`Order`, esta es una tabla chica administrada por humanos, no un registro financiero).

- `404` si el nodo no existe.
- `409` si todavía tiene subcategorías, o si todavía tiene productos asignados
  (`GET .../products` de abajo para revisar cuáles) — hay que reasignar/eliminar los
  hijos y quitar los productos primero. Deliberadamente no hay cascada ni reparenteo
  automático.

#### `PUT /v1/admin/categories/{uuid}/products` — reemplazar los productos asignados

```http
PUT /v1/admin/categories/3f9a1c2e-.../products
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "productUuids": ["3Cny4OOxdX1GoSzL9rEsTZNL7un", "7Bqz2PPydY2HpTaM0sFuUANM8vo"] }
```

**Reemplazo completo, no incremental** — el conjunto de productos asignados a esta
categoría queda exactamente igual al `productUuids` mandado (una lista vacía quita
todos). No hay endpoints de agregar/quitar un producto a la vez. `productUuids` son
`sicar_uuid` de `Product` (los mismos identificadores que ya usa todo lo demás en esta
API, p. ej. `items[].uuid` de un pedido), no el `id` interno.

Respuesta `200`:
```json
{
  "categoryUuid": "3f9a1c2e-...",
  "productUuids": ["3Cny4OOxdX1GoSzL9rEsTZNL7un", "7Bqz2PPydY2HpTaM0sFuUANM8vo"]
}
```

- `404` si la categoría no existe, o si alguno de los `productUuids` no corresponde a
  un producto real y no eliminado — el detalle nombra exactamente cuáles no se
  encontraron, para que el dashboard pueda señalarlos sin adivinar.

#### `GET /v1/admin/categories/{uuid}/products` — listar productos asignados directamente

```http
GET /v1/admin/categories/3f9a1c2e-.../products?limit=60&offset=0
X-Admin-Key: <admin-key>
```

Pensado para poblar la UI de edición antes de un `PUT` (arriba) — muestra solo lo
asignado **directamente** a este nodo, sin incluir productos etiquetados en
subcategorías (a diferencia del filtro `taxonomyUuid` de `/catalog`/`/search`, que sí
incluye descendientes — ese es para navegación del storefront, este es para editar un
nodo puntual). Paginado igual que `/catalog` (`limit` 1-200, default 60; `offset`).

Respuesta `200`:
```json
{
  "total": 2,
  "docs": [
    { "sicarUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "sku": "PR2057", "name": "Taladro 1/2\"", "descriptionDetails": null, "imageUrl": "https://.../taladro.jpg", "price": 899.00, "stock": 12 }
  ]
}
```

`404` si la categoría no existe.

#### `GET /v1/admin/categories/by-product/{productUuid}` — listar las categorías de un producto

```http
GET /v1/admin/categories/by-product/3Cny4OOxdX1GoSzL9rEsTZNL7un
X-Admin-Key: <admin-key>
```

Dirección inversa de `GET .../{uuid}/products` de arriba: dado un producto, qué
categorías tiene asignadas **directamente** (sin ancestros) — pensado para precargar
la selección actual en una pantalla de "editar tags de este producto" antes de un
`PUT .../{uuid}/products`. `productUuid` es el `sicarUuid` del producto, igual que en
todos los demás endpoints de esta API.

Respuesta `200` (lista simple, sin paginar — un producto no está tageado en cientos de
categorías):
```json
[
  { "uuid": "3f9a1c2e-...", "name": "Herramientas Eléctricas", "slug": "herramientas-electricas", "parentUuid": "6a4fd308da77fe7cd25d1dd9", "updatedAt": "2026-08-01T12:00:00Z" }
]
```
`[]` si el producto existe pero no tiene ninguna categoría asignada. `404` si el
`productUuid` no corresponde a un producto real y no eliminado.

### Vehículos (compatibilidad)

Endpoints para administrar `vehicles` — un catálogo plano de fitments
make/model/year-range/engine (p. ej. "Chevrolet Aveo 2008-2016 L4 1.6L") — y para
asignarle productos vía `product_vehicles`, el equivalente de "Categorías" arriba pero
para "qué vehículos aplican a este producto" en vez de "en qué categoría está". A
diferencia de categorías, **no hay tree/reparenteo** (una fila no tiene hijos). `GET
/v1/admin/vehicles` de abajo (búsqueda libre por `make`/`model` con `ILIKE`) es la forma
de buscar vehículos ya existentes desde este panel admin — el equivalente público a `GET
/taxonomy` ya existe (`GET /v1/vehicles/makes`/`/models`/`/years`/`/engines`/``, ver
`FRONTEND_INTEGRATION.md`), pero es un selector en cascada pensado para un shopper que ya
sabe su vehículo, no una búsqueda libre como la de este panel.

`vehicles` fue sembrada una vez (2026-08-01) desde el catálogo público de referencia de
Gonher (`catalogo.grupogonher.com`), acotado a los tipos "Automotriz" y "Motocicletas"
(sin "Camiones, Tractocamiones y Fuera de Carretera" — casi enteramente maquinaria
industrial pesada sin relación con este catálogo) — **41,937 filas**, ninguna vinculada
todavía a un producto real (`product_vehicles` sigue vacía, igual que
`product_categories` al lanzar categorías). Es un catálogo de referencia genérico para
el picker del admin, con un origen heurístico (parseo de texto libre de un tercero) —
no asumas que cada fila es 100% exacta; ver `import_gonher_vehicles.py` en el repo para
el detalle completo si hace falta re-sembrar o auditar el origen de una fila en
particular.

#### `POST /v1/admin/vehicles` — crear un fitment

```http
POST /v1/admin/vehicles
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "vehicleType": "AUTOMOTIVE", "make": "Chevrolet", "model": "Aveo", "yearStart": 2008, "yearEnd": 2016, "engine": "L4 1.6L" }
```

`vehicleType` es `"AUTOMOTIVE"` o `"MOTORCYCLE"` (constantes en inglés, no las etiquetas
en español de Gonher). `yearEnd` es opcional — se omite (o se manda `null`) para un
fitment todavía vigente ("sigue en producción"). `engine` es texto libre, opcional.

Respuesta `201`:
```json
{
  "uuid": "8f2c1a4e-...",
  "vehicleType": "AUTOMOTIVE",
  "make": "Chevrolet",
  "model": "Aveo",
  "yearStart": 2008,
  "yearEnd": 2016,
  "engine": "L4 1.6L",
  "updatedAt": "2026-08-01T12:00:00Z"
}
```

`422` si `yearEnd` es menor que `yearStart`.

#### `GET /v1/admin/vehicles` — buscar/listar vehículos

```http
GET /v1/admin/vehicles?vehicleType=MOTORCYCLE&make=italika&model=FT&limit=50&offset=0
X-Admin-Key: <admin-key>
```

`make`/`model` son coincidencia parcial sin distinguir mayúsculas (`ILIKE`), pensados
para un cuadro de búsqueda tipo autocomplete — no coincidencia exacta como
`clientEmail` en `GET /admin/orders`. `vehicleType` sí es igualdad exacta. Paginado
igual que el resto (`limit` 1-200, default 50; `offset`).

Respuesta `200` — mismo shape que `POST`, dentro de `docs`, ordenado por
`make`/`model`/`yearStart`:
```json
{
  "total": 23,
  "docs": [ { "uuid": "...", "vehicleType": "MOTORCYCLE", "make": "ITALIKA", "model": "FT125", "yearStart": 2007, "yearEnd": 2007, "engine": "124cc", "updatedAt": "..." } ]
}
```

#### `PATCH /v1/admin/vehicles/{uuid}` — actualización parcial

```http
PATCH /v1/admin/vehicles/8f2c1a4e-.../
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "yearEnd": null }
```

Solo los campos incluidos en el body se tocan (`yearEnd: null` explícito marca el
fitment como "todavía vigente"; ausente del body lo deja sin cambios). `422` si el
rango de años resultante (mezclando lo que trae el body con lo que ya tenía la fila)
queda con `yearEnd < yearStart`. Responde el mismo shape que `POST`.

#### `DELETE /v1/admin/vehicles/{uuid}` — eliminar un fitment

```http
DELETE /v1/admin/vehicles/8f2c1a4e-.../
X-Admin-Key: <admin-key>
```

`204` sin cuerpo si tiene éxito. `409` si todavía tiene productos asignados
(`GET .../products` de abajo para revisarlos) — hay que quitarlos primero. A
diferencia de categorías no hay chequeo de "hijos" (no existen).

#### `PUT /v1/admin/vehicles/{uuid}/products` — reemplazar los productos asignados

```http
PUT /v1/admin/vehicles/8f2c1a4e-.../products
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "productUuids": ["3Cny4OOxdX1GoSzL9rEsTZNL7un"] }
```

Mismo comportamiento que el equivalente de categorías arriba: **reemplazo completo, no
incremental** (una lista vacía quita todos), `productUuids` son `sicar_uuid` de
`Product`, `404` si el vehículo no existe o si algún `productUuid` no resuelve a un
producto real y no eliminado (nombrando cuáles).

Respuesta `200`:
```json
{ "vehicleUuid": "8f2c1a4e-...", "productUuids": ["3Cny4OOxdX1GoSzL9rEsTZNL7un"] }
```

#### `GET /v1/admin/vehicles/models-for-years` — modelos disponibles en varios años a la vez

```http
GET /v1/admin/vehicles/models-for-years?make=Honda&vehicleType=AUTOMOTIVE&years=2012&years=2013&years=2014
X-Admin-Key: <admin-key>
```

Primer paso de la asignación masiva de abajo: dado `make` y una lista de `years`
(repetir el parámetro por cada año), devuelve solo los modelos que existen en **todos**
esos años a la vez (intersección, no unión) — así el admin no puede elegir un modelo
que en realidad no cubre uno de los años que seleccionó. Compárese con el selector
público en cascada (`GET /v1/vehicles/years`/`/models`), que resuelve un año a la vez;
este endpoint es admin-only y existe específicamente para alimentar la asignación
masiva de abajo.

Respuesta `200`:
```json
{ "docs": ["Civic", "Accord", "Cr-V"] }
```
Lista vacía si ningún modelo de esa marca cubre todos los años dados.

#### `POST /v1/admin/vehicles/assign-by-model` — asignar productos a un modelo a través de varios años

```http
POST /v1/admin/vehicles/assign-by-model
X-Admin-Key: <admin-key>
Content-Type: application/json

{
  "vehicleType": "AUTOMOTIVE",
  "make": "Honda",
  "model": "Civic",
  "years": [2012, 2013, 2014],
  "engine": null,
  "productUuids": ["3Cny4OOxdX1GoSzL9rEsTZNL7un"]
}
```

Resuelve **todos** los fitments de `make`/`model` (y `vehicleType` si se manda) cuyo
rango `yearStart`–`yearEnd` toque **alguno** de los `years` dados, y les agrega los
`productUuids` — pensado para "esta pastilla de freno le queda al Civic 2012-2014" sin
que el admin tenga que buscar/elegir cada `uuid` de vehículo (motor por motor, año por
año) uno por uno. `model` normalmente sale de `GET .../models-for-years` de arriba, ya
filtrado a modelos que sí cubren todos esos años.

**A diferencia de `PUT /admin/vehicles/{uuid}/products` (que reemplaza el conjunto
completo de UN vehículo), esta asignación es aditiva**: agrega los productos a cada
fitment que coincide sin tocar lo que ese fitment ya tuviera asignado de antes (de este
u otro producto) — necesario porque esta operación toca muchos vehículos a la vez, y un
reemplazo por-vehículo aquí borraría silenciosamente asignaciones de otros productos en
esos mismos fitments. Repetir la misma llamada es seguro (idempotente) — pares
vehículo-producto que ya existían de una asignación previa se ignoran, no se duplican
ni cuentan como error.

`engine` es opcional — si se omite, la asignación aplica a **todas** las variantes de
motor de ese make/model/años (p. ej. las 4 variantes de motor que puede tener el Civic
2012); si se manda, solo a los fitments con ese `engine` exacto.

Respuesta `200`:
```json
{
  "make": "Honda",
  "model": "Civic",
  "years": [2012, 2013, 2014],
  "engine": null,
  "vehicleUuids": ["...", "...", "..."],
  "productUuids": ["3Cny4OOxdX1GoSzL9rEsTZNL7un"],
  "assignedCount": 11
}
```
`vehicleUuids` son todos los fitments que coincidieron (para que el dashboard pueda
mostrar/confirmar cuáles). `assignedCount` es la cantidad de vínculos (vehículo,
producto) realmente nuevos — puede ser menor a `vehicleUuids.length * productUuids.length`
si alguna combinación ya existía de una asignación anterior.

- `404` si algún `productUuid` no resuelve a un producto real y no eliminado (nombrando
  cuáles), o si ningún fitment coincide con la combinación de marca/modelo/años(/motor)
  dada.

#### `GET /v1/admin/vehicles/{uuid}/products` — listar productos asignados

```http
GET /v1/admin/vehicles/8f2c1a4e-.../products?limit=60&offset=0
X-Admin-Key: <admin-key>
```

Mismo shape/paginación que el equivalente de categorías. `404` si el vehículo no
existe.

#### `GET /v1/admin/vehicles/by-product/{productUuid}` — listar los vehículos de un producto

```http
GET /v1/admin/vehicles/by-product/3Cny4OOxdX1GoSzL9rEsTZNL7un
X-Admin-Key: <admin-key>
```

Dirección inversa de `GET .../{uuid}/products` de arriba, mismo propósito que el
equivalente de categorías (precargar una pantalla de "editar tags de este producto").

Respuesta `200` (lista simple, sin paginar):
```json
[
  { "uuid": "8f2c1a4e-...", "vehicleType": "AUTOMOTIVE", "make": "Chevrolet", "model": "Aveo", "yearStart": 2008, "yearEnd": 2016, "engine": "L4 1.6L", "updatedAt": "2026-08-01T12:00:00Z" }
]
```
`[]` si el producto existe pero no tiene ningún vehículo asignado. `404` si el
`productUuid` no corresponde a un producto real y no eliminado.

### Atributos de producto y grupos de variantes

**Independiente de Categorías/Vehículos de arriba** — no hay ningún "tipo de producto" ni
clasificación intermedia que gatee qué atributos aplican a qué producto (se evaluó y se
descartó explícitamente, ver `CLAUDE.md`). Cualquier atributo del catálogo se puede asignar
directo a cualquier producto. Dos ideas nuevas, con su propia forma en Postgres:

- **Atributos** (`attributes`) — catálogo de *definiciones* (nombre, tipo de dato, unidad).
  Los **valores** reales de cada producto viven en una sola columna JSONB
  (`Product.attributes`, clave = `slug` del atributo) — no hay una tabla "un valor por fila".
- **Presets de atributos** (`attribute-presets`) — bundles nombrados y reusables de atributos
  (p. ej. "Llantas") puramente como atajo de captura para un lote de productos similares.
  **Nunca son obligatorios ni se validan contra un producto** — aplicar un preset solo
  pre-llena claves vacías, no impone ni exige nada después.
- **Grupos de variantes** (`variant-groups`) — vincula explícitamente SKUs *distintos* de
  Sicar X (cada uno con su propio precio/stock/documento) que son la misma pieza en
  presentaciones distintas (p. ej. color) — ver `GET /v1/products/{uuid}` en
  `FRONTEND_INTEGRATION.md` para cómo el storefront los consume.

Todo empieza vacío hoy — ningún atributo/preset/grupo existe todavía, y ningún producto tiene
`attributes`/`variantGroupUuid` asignado (89,763 productos activos sin clasificar al momento de
construir esto). Es trabajo de contenido, no algo que este backend intente adivinar solo.

#### `POST /v1/admin/attributes` — crear una definición de atributo

```http
POST /v1/admin/attributes
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "name": "Color", "dataType": "ENUM", "allowedValues": ["Rojo", "Azul", "Verde"], "unit": null }
```

`dataType` es `"TEXT"` / `"NUMBER"` / `"BOOLEAN"` / `"ENUM"`. `allowedValues` es **requerido
(mínimo 2 valores) cuando `dataType` es `"ENUM"`**, ignorado en cualquier otro caso — `422` si
falta o trae menos de 2. `unit` es opcional, texto libre de display (p. ej. `"V"`, `"mm"`,
`"L"`). El `slug` **no se manda**, siempre se deriva de `name` (mismo criterio de
categorías/vehículos: minúsculas, sin acentos, guiones, sufijo `-2`/`-3`... si ya existe).

Respuesta `201`:
```json
{ "uuid": "8bdb99f9-9c96-4c37-a32c-1f000f38569b", "name": "Color", "slug": "color", "dataType": "ENUM", "allowedValues": ["Rojo", "Azul", "Verde"], "unit": null, "updatedAt": "2026-08-04T17:00:00Z" }
```

#### `GET /v1/admin/attributes` — buscar/listar atributos

```http
GET /v1/admin/attributes?search=color&dataType=ENUM&limit=50&offset=0
X-Admin-Key: <admin-key>
```

`search` es `ILIKE` parcial contra `name`, `dataType` es igualdad exacta — ambos opcionales y
combinables. Paginado igual que el resto (`limit` 1-200, default 50; `offset`).

Respuesta `200` — mismo shape que `POST`, dentro de `docs`, ordenado por `name`:
```json
{ "total": 1, "docs": [ { "uuid": "8bdb99f9-...", "name": "Color", "slug": "color", "dataType": "ENUM", "allowedValues": ["Rojo", "Azul", "Verde"], "unit": null, "updatedAt": "2026-08-04T17:00:00Z" } ] }
```

#### `PATCH /v1/admin/attributes/{uuid}` — actualización parcial

```http
PATCH /v1/admin/attributes/8bdb99f9-.../
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "unit": "cm" }
```

Solo los campos incluidos en el body se tocan. Renombrar (`name`, recalcula `slug`) o cambiar
`dataType` mientras el atributo **ya tiene valores guardados en algún producto** responde
`409` — ambos cambios romperían esos valores existentes (la clave JSONB dejaría de coincidir,
o el tipo guardado dejaría de tener sentido). Quita los valores existentes primero
(`PUT /v1/admin/products/{uuid}/attributes` de abajo) o crea un atributo nuevo en su lugar.
`422` si el `allowedValues`/`dataType` resultante (mezclando body + lo ya guardado) deja un
`ENUM` con menos de 2 valores permitidos.

#### `DELETE /v1/admin/attributes/{uuid}` — eliminar

```http
DELETE /v1/admin/attributes/8bdb99f9-.../
X-Admin-Key: <admin-key>
```

`204` sin cuerpo si tiene éxito. Borrado real. `409` si algún producto todavía tiene esta clave
en `attributes`, o si el atributo sigue asignado a algún preset — quítalo de ambos primero.

#### `GET /v1/admin/attributes/{uuid}/products` — listar productos que tienen este atributo guardado

```http
GET /v1/admin/attributes/8bdb99f9-.../products?limit=60&offset=0
X-Admin-Key: <admin-key>
```

Dirección inversa de `GET /v1/admin/products/{uuid}/attributes` — dado un atributo, qué
productos tienen esa clave guardada en `attributes` (contención JSONB, no una tabla pivote
como categorías/vehículos, pero mismo propósito/shape que
`GET /v1/admin/categories/{uuid}/products`/`GET /v1/admin/vehicles/{uuid}/products`).
Paginado igual que el resto (`limit` 1-200, default 60; `offset`). **Nota**: a diferencia de
esas dos rutas, no existe (ni se planea) un `GET /v1/admin/attributes/by-product/{productUuid}`
— esa dirección ya la cubre `GET /v1/admin/products/{uuid}/attributes` de arriba, que además
trae el `value` guardado de cada atributo, no solo cuáles están asignados.

Respuesta `200` — mismo shape que el equivalente de categorías/vehículos:
```json
{
  "total": 2,
  "docs": [
    { "sicarUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "sku": "PR2057", "name": "PORTAROLLO ROJO", "descriptionDetails": null, "imageUrl": null, "price": 8.62, "stock": 2.0 }
  ]
}
```

`404` si el atributo no existe.

#### `POST /v1/admin/attribute-presets` / `GET` / `PATCH /{uuid}` / `DELETE /{uuid}`

Mismo patrón CRUD que atributos (`{ "name": "Llantas" }` al crear, `slug` derivado
automáticamente, `search` en el listado). A diferencia de atributos/grupos de variantes,
**`DELETE` nunca se bloquea** — un preset no gatea ni valida nada de un producto, borrarlo solo
quita el atajo de captura, nunca afecta valores ya guardados.

```json
{ "uuid": "c1c2c3c4-...", "name": "Llantas", "slug": "llantas", "updatedAt": "2026-08-04T17:00:00Z" }
```

#### `PUT /v1/admin/attribute-presets/{uuid}/attributes` — reemplazar los atributos de un preset

```http
PUT /v1/admin/attribute-presets/c1c2c3c4-.../attributes
X-Admin-Key: <admin-key>
Content-Type: application/json

{
  "items": [
    { "attributeUuid": "8bdb99f9-...", "isRequired": true, "displayOrder": 1 },
    { "attributeUuid": "2380964d-...", "isRequired": false, "displayOrder": 2 }
  ]
}
```

**Reemplazo completo, no incremental** (mismo criterio que `PUT .../categories/{uuid}/products`)
— una lista vacía quita todos. `isRequired` es **solo asesorio para la UI del preset**, nunca se
valida contra lo que un producto realmente tenga guardado. `404` si algún `attributeUuid` no
resuelve (nombra cuáles). Respuesta `200` con el conjunto ya aplicado:

```json
{ "presetUuid": "c1c2c3c4-...", "docs": [ { "attributeUuid": "8bdb99f9-...", "isRequired": true, "displayOrder": 1, "attribute": { "uuid": "8bdb99f9-...", "name": "Color", "slug": "color", "dataType": "ENUM", "allowedValues": ["Rojo", "Azul", "Verde"], "unit": null, "updatedAt": "..." } } ] }
```

`GET /v1/admin/attribute-presets/{uuid}/attributes` devuelve el mismo shape (sin cuerpo de
request) — pensado para precargar la UI de edición antes de un `PUT`.

#### `POST /v1/admin/attribute-presets/{uuid}/apply` — aplicar preset a un lote de productos

```http
POST /v1/admin/attribute-presets/c1c2c3c4-.../apply
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "productUuids": ["3Cny4OOxdX1GoSzL9rEsTZNL7un", "7Bqz2PPydY2HpTaM0sFuUANM8vo"] }
```

**Esto es un scaffold, no una asignación de valores.** Agrega las claves de atributos del
preset a cada producto con valor `null` — **solo para las claves que el producto todavía no
tiene**, un valor ya guardado (de este preset o de cualquier otra vía) nunca se sobreescribe.
Pensado para "estos productos van a necesitar `color`/`talla`" sin tener que abrir cada uno y
elegir manualmente qué atributos aplican; llenar los valores reales es un paso aparte
(`PUT /v1/admin/products/{uuid}/attributes` uno por uno, o la hoja `Atributos` del `.xlsx`
más abajo). `404` si algún `productUuid` no resuelve, `422` si el preset no tiene ningún
atributo asignado todavía.

Respuesta `200`:
```json
{ "presetUuid": "c1c2c3c4-...", "productUuids": ["3Cny4OOxdX1GoSzL9rEsTZNL7un", "7Bqz2PPydY2HpTaM0sFuUANM8vo"], "scaffoldedCount": 4 }
```
`scaffoldedCount` son pares (producto, atributo) realmente agregados — un producto que ya
tenía alguna de las claves del preset cuenta menos que `productUuids.length × atributos del preset`.

#### `GET /v1/admin/products/{productUuid}/attributes` — ver los atributos guardados de un producto

```http
GET /v1/admin/products/3Cny4OOxdX1GoSzL9rEsTZNL7un/attributes
X-Admin-Key: <admin-key>
```

Respuesta `200`:
```json
{ "productUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "docs": [ { "attributeUuid": "8bdb99f9-...", "name": "Color", "slug": "color", "dataType": "ENUM", "unit": null, "value": "Rojo" } ] }
```
`docs: []` si el producto existe pero no tiene ningún atributo guardado todavía (no es un
error). `404` si `productUuid` no corresponde a un producto real y no eliminado. Mismo shape
que expone `GET /v1/products/{uuid}` al storefront (ver `FRONTEND_INTEGRATION.md`), solo que
sin filtrar por `isActive`.

#### `PUT /v1/admin/products/{productUuid}/attributes` — reemplazar los atributos guardados de un producto

```http
PUT /v1/admin/products/3Cny4OOxdX1GoSzL9rEsTZNL7un/attributes
X-Admin-Key: <admin-key>
Content-Type: application/json

{
  "values": [
    { "attributeUuid": "8bdb99f9-...", "value": "Rojo" },
    { "attributeUuid": "2380964d-...", "value": 12.5 }
  ]
}
```

**Reemplazo completo, no incremental** — el conjunto de atributos guardados del producto queda
exactamente igual a `values` (una lista vacía borra todos). `value` es un solo campo
polimórfico en el wire (`string`/`number`/`boolean`/`null`) — se valida server-side contra el
`dataType`/`allowedValues` del `attributeUuid` referenciado **antes de escribir nada** (si
cualquier valor falla, no se guarda ninguno):

- `404` si algún `attributeUuid` no resuelve contra el catálogo (nombra cuáles).
- `422` si algún `value` no coincide con el `dataType` del atributo (p. ej. texto para un
  `NUMBER`), o si un `ENUM` recibe un valor fuera de `allowedValues` (nombra el atributo y,
  para `ENUM`, la lista de valores válidos).

Respuesta `200` — mismo shape que `GET` de arriba, con el conjunto ya aplicado.

#### `PATCH /v1/admin/products/{productUuid}/variant-group` — asignar/quitar el grupo de variantes de un producto

```http
PATCH /v1/admin/products/3Cny4OOxdX1GoSzL9rEsTZNL7un/variant-group
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "variantGroupUuid": "b52bf1c5-873a-45f6-b341-930106d669ed" }
```

Convenience de un solo producto — para asignar/reasignar varios a la vez de golpe, usa
`PUT /v1/admin/variant-groups/{uuid}/products` de abajo. `variantGroupUuid: null` quita al
producto de cualquier grupo. `404` si el producto o el `variantGroupUuid` dado no existen.

Respuesta `200`:
```json
{ "productUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "variantGroupUuid": "b52bf1c5-873a-45f6-b341-930106d669ed" }
```

#### `POST /v1/admin/variant-groups` / `GET` / `PATCH /{uuid}` / `DELETE /{uuid}`

Mismo patrón CRUD que atributos/presets:

```http
POST /v1/admin/variant-groups
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "name": "Portarollo acero inoxidable", "variantAttributeSlug": "color" }
```

`variantAttributeSlug` es **opcional y texto libre** (no se valida contra `attributes.slug`) —
solo indica al storefront qué atributo distingue a los miembros del grupo para pintar el
selector correcto (ver `GET /v1/products/{uuid}` en `FRONTEND_INTEGRATION.md`); se puede dejar
`null` si el grupo no tiene un atributo distintivo claro. Respuesta `201`:

```json
{ "uuid": "b52bf1c5-...", "name": "Portarollo acero inoxidable", "variantAttributeSlug": "color", "updatedAt": "2026-08-04T17:00:00Z" }
```

`GET` acepta `search` (`ILIKE` parcial contra `name`) + paginación. `PATCH` es parcial
(`exclude_unset`, mismo criterio que el resto). `DELETE` responde `409` si algún producto
todavía apunta a este grupo (`GET .../products` de abajo para revisarlos) — quítalos primero.

#### `PUT /v1/admin/variant-groups/{uuid}/products` — reemplazar los miembros de un grupo de variantes

```http
PUT /v1/admin/variant-groups/b52bf1c5-.../products
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "productUuids": ["3Cny4OOxdX1GoSzL9rEsTZNL7un", "7Bqz2PPydY2HpTaM0sFuUANM8vo"] }
```

**Reemplazo completo, no incremental** — a diferencia de categorías/vehículos (N:M vía tabla
pivote), `variantGroupUuid` es una columna directa en `Product`, así que "reemplazar" reasigna
esa columna: limpia el grupo de cualquier producto que ya no esté en la lista y lo asigna a
los que sí. `productUuids` son `sicarUuid`, igual que en todos los demás endpoints de esta
API. `404` si el grupo no existe o si algún `productUuid` no resuelve (nombrando cuáles).

Respuesta `200`:
```json
{ "variantGroupUuid": "b52bf1c5-...", "productUuids": ["3Cny4OOxdX1GoSzL9rEsTZNL7un", "7Bqz2PPydY2HpTaM0sFuUANM8vo"] }
```

`GET /v1/admin/variant-groups/{uuid}/products` (paginado, mismo shape que el equivalente de
categorías/vehículos) lista los miembros actuales — pensado para poblar la UI de edición antes
de un `PUT`.

### Importación masiva por Excel

#### `GET /v1/admin/bulk-import/template` — descargar una plantilla de ejemplo

```http
GET /v1/admin/bulk-import/template
X-Admin-Key: <admin-key>
```

Respuesta `200`: el archivo `.xlsx` en sí (`Content-Type:
application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, no JSON), con
`Content-Disposition: attachment; filename=plantilla_importacion_masiva.xlsx` — un botón
"Descargar plantilla" en el dashboard puede apuntar directo aquí. Trae las mismas cuatro
hojas/columnas que `POST .../products` espera (nunca puede desalinearse: ambos lados
comparten las mismas constantes en `bulk_import_service.py`), con encabezados en negritas,
comentarios de celda explicando las columnas menos obvias (`categorySlug`, `make`/`model`
insensibles a mayúsculas, `year` como año único no rango, `engine`/`vehicleType`
opcionales, formato de `value` según `dataType`, `variantGroupSlug` derivado del `name` del
grupo), y una o dos filas de ejemplo con datos **claramente ficticios** (`sku`
`"SKU-EJEMPLO-1"`, marca `"Marca-Ejemplo"`, etc.) — hay que reemplazarlas o borrarlas
antes de subir datos reales; si se sube tal cual sin editar, cada fila de ejemplo
simplemente sale como un error por fila (nada coincide con datos ficticios), no un fallo
del archivo completo.

`POST /v1/admin/bulk-import/products` — asigna categorías, compatibilidad de vehículos,
valores de atributos y/o grupos de variantes a muchos productos de una sola vez desde un
archivo `.xlsx`, en vez de una asignación a la vez vía los endpoints de arriba. Pensado
para poblar `product_categories`/`product_vehicles`/`Product.attributes`/
`Product.variantGroupUuid` sobre el catálogo real (hoy todas siguen vacías/sin clasificar
— ver CLAUDE.md).

```http
POST /v1/admin/bulk-import/products
X-Admin-Key: <admin-key>
Content-Type: multipart/form-data

file: <archivo .xlsx>
```

El archivo puede traer cualquier subconjunto de estas cuatro hojas (por nombre exacto, sin
importar en qué orden estén dentro del archivo); si falta alguna, se trata como "0 filas"
para esa hoja, no como error:

- **`Categorias`** — columnas `sku`, `categorySlug`.
- **`Vehiculos`** — columnas `sku`, `make`, `model`, `year`, y opcionalmente `vehicleType`
  (`AUTOMOTIVE`/`MOTORCYCLE`) y `engine`.
- **`Atributos`** — columnas `sku`, `attributeSlug`, `value`. `value` según el `dataType` del
  atributo: `TEXT`/`ENUM` texto tal cual (`ENUM` debe ser uno de sus `allowedValues`),
  `NUMBER` numérico, `BOOLEAN` acepta `TRUE`/`FALSE` (también `si`/`no`, `1`/`0`).
- **`Variantes`** — columnas `sku`, `variantGroupSlug`. `variantGroupSlug` se deriva del
  `name` del grupo (mismo slugify que categorías/atributos — `VariantGroup` no tiene una
  columna `slug` propia, ver `GET /v1/admin/variant-groups` arriba para los nombres
  existentes).

Cada fila de `Vehiculos`/`Atributos`/`Variantes` es una sola asignación (formato largo). En `Categorias`, la
celda `categorySlug` puede traer **un solo slug o varios separados por coma o punto y
coma** (p. ej. `"herramientas, jardineria"`) para asignar varias categorías al mismo
producto sin repetir la fila — cada slug de la lista se resuelve por separado, así que
si uno no existe los demás de esa misma fila igual se asignan (se reporta un error solo
por el slug que falló, no por toda la fila). `sku` se busca primero contra `Product.sku`
y, si no aparece ahí, contra
`Product.additionalSkus` — nunca contra el `sicarUuid` interno. La comparación de `sku`,
`make`, `model` y `engine` **no distingue mayúsculas/minúsculas** (el catálogo de
`vehicles` tiene casing inconsistente entre filas, p. ej. `"ITALIKA"` vs. `"Chevrolet"`,
y no tendría sentido que un admin fallara por escribir "honda" en vez de "Honda").
`categorySlug` sí distingue mayúsculas/minúsculas (los slugs ya salen siempre en
minúscula del slugify existente, así que no hay nada realista que pueda desalinearse ahí).

En la hoja `Vehiculos`, `year` es **un solo año** (no un rango `yearStart`/`yearEnd`) —
se resuelve contra los fitments ya existentes cuyo rango contenga ese año, exactamente
igual que `POST /admin/vehicles/assign-by-model` de arriba. Si `engine` se omite, la fila
aplica a **todas** las variantes de motor de esa marca/modelo/año.

**Semántica de re-subida distinta por hoja — no asumas que las cuatro se comportan igual:**

- **`Categorias`/`Vehiculos` son ADITIVAS** (igual que `assign-by-model`): los vínculos ya
  existentes de un producto (de esta u otra carga) nunca se eliminan, solo se agregan los
  nuevos. Subir el mismo archivo dos veces es seguro — la segunda vez `assignedCount` da `0`
  en ambas hojas sin duplicar nada ni fallar (`ON CONFLICT DO NOTHING` sobre las mismas PKs
  compuestas que usa `assign-by-model`).
- **`Atributos` hace MERGE** — a diferencia de las dos de arriba, un valor **corregido** en una
  corrida posterior SÍ se aplica (no se ignora como un vínculo ya existente); solo se
  preservan las claves que esta hoja no menciona, de una carga anterior o de
  `PUT /v1/admin/products/{uuid}/attributes`. Subir el mismo archivo dos veces sigue siendo
  seguro (mismo valor → sin cambio real), pero no es "ignorar si ya existe" como
  `Categorias`/`Vehiculos`.
- **`Variantes` REEMPLAZA** — `variantGroupUuid` es un solo valor por producto (columna
  directa, no un tag vía tabla pivote), así que no existe "aditivo" aquí: la fila más
  reciente para un `sku` dado gana, y esa carga sobreescribe lo que el producto ya tuviera.

**Éxito parcial, no todo-o-nada** — una fila inválida (SKU inexistente, slug de categoría/
atributo/grupo inexistente, valor con tipo incorrecto, ningún vehículo coincide) no bloquea
el resto del archivo: se omite esa fila (o, en `Categorias` con varios slugs en una celda,
solo el slug que falló) y se reporta en `errors`, el resto se aplica igual. Solo un problema
a nivel de archivo completo (no es un `.xlsx` real, falta cualquiera de las cuatro hojas, o
una hoja presente le falta una columna requerida) rechaza la solicitud entera.

Respuesta `200`:
```json
{
  "categories": {
    "found": true,
    "processedRows": 120,
    "assignedCount": 118,
    "errors": [
      { "sheet": "Categorias", "row": 47, "reasonCode": "SKU_NOT_FOUND", "reason": "SKU no encontrado: ABC-999.", "sku": "ABC-999" }
    ]
  },
  "vehicles": {
    "found": true,
    "processedRows": 340,
    "assignedCount": 336,
    "errors": [
      { "sheet": "Vehiculos", "row": 12, "reasonCode": "VEHICLE_NOT_FOUND", "reason": "No se encontro ningun vehiculo para Honda Civic 1990.", "sku": "PR2057" }
    ]
  },
  "attributes": {
    "found": true,
    "processedRows": 80,
    "assignedCount": 78,
    "errors": [
      { "sheet": "Atributos", "row": 9, "reasonCode": "VALUE_TYPE_MISMATCH", "reason": "'voltaje' espera un numero (NUMBER).", "sku": "PR2057" }
    ]
  },
  "variants": {
    "found": false,
    "processedRows": 0,
    "assignedCount": 0,
    "errors": []
  }
}
```
`found: false` en cualquiera de las cuatro (en vez de `errors: []`) significa que esa hoja no
venía en el archivo, distinto de "venía pero con 0 filas de datos". `assignedCount` significa
algo ligeramente distinto por hoja: en `categories`/`vehicles` son vínculos **nuevos**
realmente insertados (no cuenta pares que ya existían); en `attributes` son pares
(producto, atributo) **escritos** en total (incluye valores corregidos sobre una clave que ya
existía, no solo claves nuevas); en `variants` es la cantidad de **productos** cuyo
`variantGroupUuid` se aplicó. En cualquier caso puede ser menor a `processedRows` incluso sin
ningún error (fila válida mandada dos veces, incluye la fila de ejemplo de la plantilla, etc.).

Errores de fila (`errors[].reasonCode`): `MISSING_FIELDS` (celda requerida vacía),
`SKU_NOT_FOUND`, `CATEGORY_SLUG_NOT_FOUND`, `INVALID_YEAR` (`year` no es un entero),
`VEHICLE_NOT_FOUND` (ningún fitment coincide con marca/modelo/año/motor — también cubre
un `vehicleType` mal escrito, que da el mismo resultado observable que no coincidir),
`ATTRIBUTE_SLUG_NOT_FOUND`, `VALUE_TYPE_MISMATCH` (`value` no coincide con el `dataType` del
atributo, o no está entre sus `allowedValues` si es `ENUM`), `VARIANT_GROUP_SLUG_NOT_FOUND`.

Errores de archivo completo (rechazan toda la solicitud, no generan `errors` por fila):
- `400` si el archivo no es un `.xlsx` válido, o pesa más de 4 MB.
- `400` si no contiene ninguna hoja `Categorias`, `Vehiculos`, `Atributos` ni `Variantes`.
- `400` si alguna hoja excede 20,000 filas de datos.
- `422` si una hoja presente no tiene todas sus columnas requeridas (nombra la hoja y las
  columnas que faltan).

## Notas y advertencias

- **El dashboard admin es la única fuente de verdad de `dispatchStatus`** — Sicar X ya nunca lo
  sobreescribe. Todas las mutaciones (`/accept`, `/advance-status`, `/shipping/generate`) aplican
  el nuevo valor en Postgres de inmediato, en la misma respuesta; avisarle a Sicar X siempre queda
  como un paso asíncrono/best-effort vía `sicar_sync_outbox`, nunca al revés. El storefront
  (`GET /v1/auth/me/orders/{orderUuid}`) tampoco consulta a Sicar X en vivo — sirve exactamente lo
  que este panel haya dejado en Postgres.
- **No hay reintentos automáticos si tu backend falla al llamar estas rutas** — a diferencia del
  worker interno (que sí reintenta `ACCEPT`/`CANCEL`/`DISPATCH`/`SYNC_DISPATCH_STATUS` contra
  Sicar X), un error de red o un `5xx` al llamar `/v1/admin/*` desde el dashboard no se reintenta
  solo; implementa tu propio reintento si lo necesitas.
- **Si `sicar_sync_outbox` acumula filas `FAILED`** para un pedido, revisa
  `GET /v1/admin/sync/outbox?status=FAILED` — probablemente el pedido ya está cancelado en Sicar X
  (un `409 "Document is canceled"` es la causa más común) o hay un problema de token/red con Sicar
  X. El estado local ya es correcto en cualquier caso; esto solo afecta si Sicar X se enteró.
- **`GET /v1/admin/orders` no tiene `sortBy`** — siempre ordena por `createdAt` descendente, sin
  opción de cambiarlo hoy.
- **`includeDeleted` existe para reconciliación, no para uso normal del dashboard** — un pedido
  soft-deleted (`DELETE /v1/orders/{id}` desde el storefront) ya no es accionable por el cliente,
  pero la fila persiste para que el worker siga pudiendo avisarle a Sicar X. Úsalo solo si
  necesitas ver/auditar algo que el cliente ya "borró" de su propio historial.
- **Ningún endpoint de este documento acepta ni necesita `x-api-key`, `Authorization` o
  `X-Client-Token`** — esas son cabeceras del storefront (ver `FRONTEND_INTEGRATION.md`), no
  tienen ningún efecto aquí.
- **Este es un panel interno, no el contrato del storefront** — si algo de aquí parece
  relacionarse con un endpoint de `FRONTEND_INTEGRATION.md` (p. ej. el shape de un pedido), son
  API's distintas con su propio ciclo de cambios; no asumas que un cambio en una se refleja
  automáticamente en la otra.
- **Atributos/presets/grupos de variantes son PIM propio, independiente de Categorías y
  Vehículos** — no hay ninguna clasificación intermedia ("tipo de producto") que gatee qué
  atributos aplican a qué producto; cualquier atributo se puede asignar a cualquier producto
  directamente. Los tres catálogos (`attributes`, `attribute-presets`, `variant-groups`) están
  vacíos hoy — clasificar el catálogo real es trabajo de contenido del dashboard, no algo que
  este backend intente adivinar. `GET /v1/products/{uuid}` en `FRONTEND_INTEGRATION.md` es la
  única ruta del storefront donde `attributes`/`variantGroup` aparecen — ni `POST /v1/products`
  ni `POST /v1/search` los exponen.
