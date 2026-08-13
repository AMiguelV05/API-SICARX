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

## Webhooks salientes hacia el dashboard admin

Ademas de las rutas de `/v1/admin/*` que el dashboard llama hacia esta API (documentadas
abajo), esta API llama **hacia el dashboard** en 3 situaciones puntuales. Implementa estas 3
rutas en el backend/servidor del dashboard (nunca las recibe el navegador, mismo criterio
"server-to-server" de arriba) para recibirlas.

**Verificacion de firma** (obligatoria, misma formula para las 3 - no se repite en cada una):

```http
POST /api/webhooks/<lo que corresponda>
Content-Type: application/json
X-Webhook-Timestamp: 1783961178
X-Webhook-Signature: 3f2a9c...  (hex, HMAC-SHA256)
```

```
manifest = "{X-Webhook-Timestamp}." + <raw request body, tal cual, sin re-serializar>
signature = hex(HMAC_SHA256(ADMIN_WEBHOOK_SECRET, manifest))
```

`ADMIN_WEBHOOK_SECRET` es un secreto **distinto** de `ADMIN_API_KEY` de arriba (ese autentica
llamadas *hacia* esta API; este firma llamadas *desde* esta API hacia el dashboard - un leak
de uno no compromete al otro) y se entrega por el mismo canal seguro fuera de este repo, nunca
documentado aqui. Recalcula `signature` con el mismo secreto y comparala contra
`X-Webhook-Signature` con una comparacion en tiempo constante (no `===`/`==`), usando el
**body crudo** para el HMAC - un JSON re-serializado puede no ser byte-a-byte identico al
original. Rechaza tambien si `X-Webhook-Timestamp` tiene mas de ~5 minutos de antiguedad
(proteccion contra replay).

**Sin reintentos de este lado para las 3** - responde `200` rapido; si tu endpoint falla o
tarda, esta API no reintenta automaticamente (mismo comportamiento que los webhooks hacia el
storefront, ver `FRONTEND_INTEGRATION.md`).

**Si `ADMIN_DASHBOARD_BASE_URL`/`ADMIN_WEBHOOK_SECRET` no estan configurados del lado de esta
API, las 3 llamadas se omiten silenciosamente** (se loguea a nivel INFO, no hay error
visible) - confirma con el equipo de backend que ambos estan configurados en produccion antes
de asumir que estas notificaciones estan llegando.

### Webhook saliente: `POST {tu dominio}/api/webhooks/order-cancelled`

Se llama en el momento exacto en que un pedido pasa a `"CANCELLED"` - mismo disparador
(cliente cancela/elimina, o pago rechazado/cancelado en Mercado Pago) y mismo body
(`OrderPublic` mas `clientEmail`/`clientName`) que el webhook homonimo hacia el storefront -
ver `FRONTEND_INTEGRATION.md` para el shape completo, no lo repetimos aqui:

```json
{
  "uuid": "f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6",
  "sicarOrderId": "d65b89dc-9690-40b3-8dfb-aa2cdde18cc0",
  "status": "CANCELLED",
  "dispatchStatus": "PENDING_ACCEPTANCE",
  "total": 129.99,
  "totalQuantity": 3,
  "items": [ { "uuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "sku": "PR2057", "description": "PORTAROLLO", "quantity": "1", "unit": "PZA", "imageUrl": "https://.../portarollo.jpg" } ],
  "createdAt": "2026-07-10T18:32:05Z",
  "cancellationReason": null,
  "clientEmail": "juan@example.com",
  "clientName": "Juan Pérez"
}
```

**Importante - si el pedido ya habia sido aceptado por un administrador, avisarle a Sicar X
puede seguir en curso cuando llega este webhook** (y si nunca fue aceptado, no se le avisa
nada a Sicar X en absoluto) - la cancelacion local ya es un hecho consumado de cualquier
forma, ver `POST /v1/orders/{order_id}/cancel` en `FRONTEND_INTEGRATION.md`.

**`cancellationReason`** es un campo nuevo en este body: `null` cuando el disparador fue el
cliente (self-cancel, `DELETE`, o pago rechazado/cancelado en Mercado Pago) o un texto libre
cuando un administrador canceló el pedido vía `POST /v1/admin/orders/{orderUuid}/cancel` (ver
arriba) — úsalo para distinguir ambos casos en la notificación que le muestres al cliente.

### Webhook saliente: `POST {tu dominio}/api/webhooks/order-sicar-sync-failed`

Senal de que `sicar_sync_outbox` agoto sus reintentos (`MAX_ATTEMPTS = 5`, backoff
exponencial 1/2/4/8/16 min) intentando avisarle a Sicar X de un `ACCEPT`/`CANCEL` de
inventario - requiere reconciliacion **manual** directamente en Sicar X, esta API ya no lo va
a reintentar sola. Usa `GET /v1/admin/sync/outbox`/`POST .../retry` mas abajo para resolverlo
desde el dashboard una vez identificado.

```json
{
  "orderUuid": "f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6",
  "sicarOrderId": "d65b89dc-9690-40b3-8dfb-aa2cdde18cc0",
  "lastError": "Connection timeout after 30s"
}
```

### Webhook saliente: `POST {tu dominio}/api/webhooks/product-stock-drift`

Senal de que uno o mas productos tienen `reserved` (unidades reservadas por pedidos locales
todavia no aceptados) por encima de `stock` (el stock real, sincronizado desde Sicar X) - pasa
cuando el stock real baja por una razon ajena a esta API (venta en tienda, otro canal)
mientras habia unidades reservadas en linea, dejando `availableStock` en 0 en los endpoints de
catalogo aunque `reserved` siga reteniendo mas de lo que fisicamente existe (ver `stock`/
`availableStock` en la seccion de productos por categoria/vehiculo/grupo de variantes/atributo
mas abajo). Se dispara desde el sync de catalogo (cada 5 minutos) cada vez que la corrida
termina con al menos un producto en ese estado - puede repetirse en corridas consecutivas
mientras la deriva no se resuelva manualmente (ajustando el stock en Sicar X o reconciliando
el pedido correspondiente).

```json
{
  "products": [
    { "sicarUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "sku": "PR2057", "name": "Taladro 1/2\"", "stock": 2.0, "reserved": 3.0 }
  ]
}
```

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
  "deliveryAssignedAt": null,
  "cancellationReason": null
}
```

Mismo shape base que `GET /v1/auth/me/orders/{orderUuid}` en el storefront (ver
`FRONTEND_INTEGRATION.md`), más los campos exclusivos de este panel: `clientEmail`/`clientName`
(resueltos igual que en el webhook `order-confirmed`), `deletedAt` (el storefront nunca lo
expone), y los cuatro campos nuevos de aceptación/mensajería (`acceptedAt`/`acceptedBy`/
`deliveryCompany`/`deliveryAssignedAt`). `404` si el `orderUuid` no existe (o está soft-deleted y
no se mandó `includeDeleted=true`) — esta ruta no filtra por dueño, así que un `orderUuid` válido
de cualquier cliente siempre resuelve. `cancellationReason` (también expuesto en
`GET /v1/auth/me/orders/{orderUuid}` del storefront, no solo aquí) es `null` salvo que la orden
haya sido cancelada vía `POST .../cancel` de abajo — una cancelación hecha por el propio cliente
deja este campo en `null`.

**Cambio a `GET /v1/admin/orders`/`GET .../{orderUuid}` (2026-08-11 — checkout de
invitado)**: `POST /v1/orders` en el storefront ya no requiere cuenta (ver
`FRONTEND_INTEGRATION.md`, "Checkout de invitado") — un pedido de invitado aparece aquí igual
que cualquier otro, con dos diferencias:
- Dos campos nuevos en la respuesta: `isGuest: boolean` (`true` mientras el pedido no tenga
  cuenta asociada) y `guestEmail: string | null` (el correo capturado en el checkout de
  invitado; `null` para un pedido de cuenta, o una vez que el pedido se vincula
  retroactivamente a una cuenta — ver más abajo).
- `clientEmail`/`clientName` siguen viniendo poblados igual para un pedido de invitado
  (resueltos vía `deliveryInfo.contactInfo`, mismo mecanismo que ya usa el webhook
  `order-confirmed`) — no hace falta revisar `isGuest` solo para mostrar esos dos campos.
- Los filtros `clientEmail`/`clientUuid` de `GET /v1/admin/orders` hacen `JOIN` contra
  `ClientAccount` y por lo tanto **nunca** encuentran un pedido todavía sin cuenta — para
  ubicar pedidos de invitado hoy hay que buscar sin esos filtros y revisar `isGuest`/
  `guestEmail` en los resultados; no existe un filtro dedicado `guestEmail`/`isGuest` todavía.
- Si el invitado luego crea una cuenta con el mismo correo y la verifica, el pedido se
  vincula solo (`isGuest` pasa a `false`, `guestEmail` a `null`, y a partir de ahí sí aparece
  en los filtros `clientEmail`/`clientUuid`) — no requiere ninguna acción desde este panel.

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

### `POST /v1/admin/orders/{orderUuid}/cancel` — cancelar un pedido como administrador

```http
POST /v1/admin/orders/f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6/cancel
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "reason": "Producto agotado en tienda" }
```

`reason` es **obligatorio y no puede estar vacío** — a diferencia de `acceptedBy` en `/accept`,
esto no es solo auditoría interna: queda guardado en la orden (`cancellationReason`, visible en
`GET .../orders/{orderUuid}` de arriba y en `GET /v1/auth/me/orders/{orderUuid}` del storefront)
y viaja en el webhook `order-cancelled` de abajo — es lo que el cliente ve como motivo de su
cancelación. No hay `cancelledBy` en este contrato — no existe todavía un sistema de usuarios
admin real detrás de `X-Admin-Key` para identificar quién canceló (mismo motivo por el que
`acceptedBy` en `/accept` es texto libre y opcional).

Respuesta `200`:
```json
{
  "orderUuid": "f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6",
  "cancelledAt": "2026-08-07T19:35:29Z",
  "reason": "Producto agotado en tienda",
  "status": "CANCELLED",
  "syncStatus": "QUEUED",
  "note": "La cancelación ya se aplicó localmente; se le avisa a Sicar X de forma asíncrona vía sicar_sync_outbox."
}
```

Mismo mecanismo de fondo que `POST /v1/orders/{order_id}/cancel` del storefront (ver
`FRONTEND_INTEGRATION.md`): stock/reserva local restaurados de inmediato, cualquier pago de
Mercado Pago pendiente/aprobado se reembolsa/cancela primero, y — a diferencia de
`DELETE /v1/orders/{order_id}` del storefront — **esta ruta nunca hace soft-delete**: la orden
sigue apareciendo en el historial del cliente, ahora como `CANCELLED` con su `reason`. `syncStatus`
es `"QUEUED"` si la orden ya había sido aceptada (`POST .../accept`, así que Sicar X ya sabía de
ella y hay que avisarle de forma asíncrona vía `sicar_sync_outbox`, igual que `/accept`) o
`"NOT_NEEDED"` si nunca fue aceptada (Sicar X nunca se enteró de esta orden, no hay nada que
sincronizar). `404` si el pedido no existe (o está soft-deleted). `409` si ya está `CANCELLED`, o
si `dispatchStatus` ya es `DISPATCHED` (`COMPLETE` sigue siendo cancelable — también es el estado
terminal de pedidos `PICKUP`, que nunca se "enviaron" a ningún lado).

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
diferencia de `/accept`, no hay noción de "ya asignado, no se puede reasignar" (corregir el
nombre de la mensajería es un caso de uso legítimo).

**Nuevo (2026-08-13)**: sí se agregaron dos guardas de estado, mismo criterio que
`/accept`/`/cancel`/`advance-status` para un estado genuinamente inválido (no para "ya
asignado", que sigue permitido): `409` si el pedido ya está `CANCELLED`, y `409` si el
pedido es `PICKUP` (recolección en tienda no tiene mensajería que asignar — mismo chequeo
que el `409` de `DISPATCHED` en `advance-status` para pedidos `PICKUP`).

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

**Corrección (2026-08-11): a diferencia de lo que este párrafo afirmaba antes, este endpoint
NO encola nada en `sicar_sync_outbox`.** Confirmado contra el código real: `admin_service.
generate_shipping_label` no crea ninguna fila de outbox, y `sicar_sync_worker.py` solo tiene
ramas para `action == "ACCEPT"`/`"CANCEL"` (ambas atadas al espejo de inventario, no a envío) —
no existe ni ha existido un `action: "DISPATCH"`. `dispatchStatus` se pone en `"DISPATCHED"`
**de inmediato** en Postgres, en la misma transacción que persiste `shippingLabel`, y ahí
termina — Sicar X nunca se entera de que existe una guía de envío, ni de forma síncrona ni
asíncrona (mismo espíritu "Sicar X es solo ERP de inventario" de `CLAUDE.md`, llevado un paso
más allá aquí). Si se necesita reflejar el envío en Sicar X, es un proceso manual fuera de este
backend. Sí dispara la misma notificación `order-dispatched` al storefront que `/advance-status`
dispara para un `DISPATCHED` alcanzado manualmente (ver `FRONTEND_INTEGRATION.md`) — el cliente
recibe el mismo mensaje sin importar cuál de los dos caminos se usó.

**Actualización (2026-08-11)**: este mismo `shippingLabel` (el objeto de arriba, con
`trackUrl` incluido) ahora también se expone directamente al cliente vía `OrderPublic` —
`GET /v1/auth/me/orders/{orderUuid}` y el body del webhook `order-dispatched` que se acaba de
mencionar, ambos reusan ese schema. No hubo que agregar un webhook nuevo ni una columna nueva:
solo se agregó el campo a `OrderPublic` (antes solo vivía en `AdminOrderPublic`, admin-only) —
la notificación ya se disparaba en el momento correcto, solo le faltaba el dato. Cuando el
`DISPATCHED` se alcanzó manualmente (sin guía real de por medio), `shippingLabel` sigue siendo
`null` tanto para el admin como para el cliente.

**Advertencia de confiabilidad** (mismo espíritu que la nota de "no hay reintentos automáticos"
más abajo): esta llamada tiene un efecto real con costo — un timeout del lado del dashboard que
compite con un éxito del lado del servidor mostraría un error al admin mientras envia.com ya
generó (y cobró) una guía. Documentar esto como riesgo conocido, no agregar reintento automático
sobre timeout para "resolverlo".

#### `POST /v1/admin/orders/{orderUuid}/shipping/cancel` — cancelar la guía

```http
POST /v1/admin/orders/f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6/shipping/cancel
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "reason": "Dirección incorrecta, se regenerará la guía" }
```

El backend reconstruye la petición a envia.com a partir del `shippingLabel` ya persistido —
solo necesita `carrier`/`trackingNumber` (los únicos campos que envia.com exige para
`POST /ship/cancel/`), el admin nunca los vuelve a capturar. Llama a envia.com y, si tiene
éxito, en una sola transacción: `shippingLabel` vuelve a `null` y `dispatchStatus` revierte de
`DISPATCHED` a `COMPLETE` — es la **única** forma de desbloquear ese camino, ya que
`/advance-status` se niega explícitamente a revertir `DISPATCHED → COMPLETE` mientras
`shippingLabel` siga poblado. Con la orden de vuelta en `COMPLETE`, `/shipping/quote` y
`/shipping/generate` vuelven a estar disponibles normalmente sobre la misma orden.

`reason` se persiste (`Order.shipping_cancellation_reason`/`shippingLabelCancelledAt`, visibles
en `GET /admin/orders/{uuid}`) — mismo patrón que `AdminOrderCancelRequest.reason` en
`POST /orders/{uuid}/cancel` — pero, a diferencia de ese, **no** se le notifica al cliente ni se
manda en ningún webhook: es una corrección administrativa interna, no un evento de cara al
cliente. Igual que `/shipping/generate`, esta cancelación es puramente local — no toca
`sicar_sync_outbox` ni avisa nada a Sicar X.

Respuesta `200`:
```json
{
  "orderUuid": "f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6",
  "dispatchStatus": "COMPLETE",
  "shippingLabel": null,
  "refund": { "balanceReturned": true, "balanceReturnDate": "2026-08-15T00:00:00Z" }
}
```

`refund` es la respuesta cruda de envia.com al cancelar, pasada tal cual para que el admin sepa
si hay devolución de saldo y cuándo — no se persiste en ningún lado (no hay dónde, una vez que
`shippingLabel` queda en `null`).

- `404` si el pedido no existe. `409` si el pedido no tiene `shippingLabel` (nada que cancelar) —
  espejo del `409` inverso de `/shipping/generate`. `422` si `reason` falta o está vacío. `502` si
  envia.com rechaza la cancelación — el caso real más probable según su documentación es que la
  guía ya fue recogida por el carrier o entró a su red (`"the shipment must not have been picked
  up or entered the carrier network"`) — incluye el mensaje real de envia.com entre paréntesis,
  mismo tratamiento de `meta:"error"` que `/shipping/generate`.

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

**Cuidado con categorías con más de 200 productos asignados**: `GET .../products` (abajo)
está limitado a `limit ≤ 200`. Si el dashboard carga una sola página para poblar el picker
y la categoría tiene más productos asignados que esa página, un `PUT` guardado desde esa
UI **sobreescribe silenciosamente** el conjunto completo con solo lo que se cargó — los
productos no cargados se pierden sin ningún error. Para ese caso, usar el `PATCH` de abajo
en vez de este `PUT`.

#### `PATCH /v1/admin/categories/{uuid}/products` — agregar/quitar productos de forma incremental

```http
PATCH /v1/admin/categories/3f9a1c2e-.../products
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "add": ["3Cny4OOxdX1GoSzL9rEsTZNL7un"], "remove": ["7Bqz2PPydY2HpTaM0sFuUANM8vo"] }
```

**Incremental** — a diferencia del `PUT` de arriba, no necesita conocer el conjunto
completo asignado: solo agrega los `productUuids` de `add` y quita los de `remove`,
dejando todo lo demás intacto. Seguro de usar aunque la categoría tenga más productos
asignados que el límite de `GET .../products`. `add` de un producto ya asignado es un
no-op (idempotente); `remove` de un producto no asignado o inexistente también se ignora
(no-op tolerante) — solo se valida la existencia real de los `productUuids` de `add`.

Respuesta `200`:
```json
{
  "categoryUuid": "3f9a1c2e-...",
  "added": ["3Cny4OOxdX1GoSzL9rEsTZNL7un"],
  "removed": ["7Bqz2PPydY2HpTaM0sFuUANM8vo"],
  "addedCount": 1,
  "removedCount": 1
}
```
`addedCount`/`removedCount` son los vínculos realmente afectados (no un eco de lo
mandado) — p. ej. un `add` de un producto ya asignado no incrementa `addedCount`.

- `422` si un mismo `productUuid` aparece en `add` y `remove` a la vez.
- `404` si la categoría no existe, o si algún `productUuid` de `add` no resuelve a un
  producto real y no eliminado (nombra cuáles).

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

Cada producto trae **ambos** números de stock (a diferencia de `/catalog`/`/search`, que
solo exponen `stock` = disponible para venta): `stock` es el físico crudo sincronizado
desde Sicar X, `availableStock` es lo que realmente queda vendible ahora mismo (`stock`
menos reservas de pedidos locales todavía no aceptados — ver el webhook
`product-stock-drift` arriba para cuando `availableStock` cae a 0 por una razón ajena a
esta API).

Respuesta `200`:
```json
{
  "total": 2,
  "docs": [
    { "sicarUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "sku": "PR2057", "name": "Taladro 1/2\"", "descriptionDetails": null, "imageUrl": "https://.../taladro.jpg", "price": 899.00, "stock": 12, "availableStock": 9, "salesCount": 34 }
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

#### `GET /v1/admin/categories/export` — descargar CSV de categorías + productos

```http
GET /v1/admin/categories/export
X-Admin-Key: <admin-key>
```

Opcionalmente `?taxonomyUuid=<uuid>` (antes `categoryUuid` — renombrado 2026-08-13 para no
chocar con el `categoryUuid` de `/products`/`/search`, que filtra por un campo
completamente distinto, `Product.category_uuid` sincronizado de Sicar X; este endpoint es
admin-only y no forma parte del contrato con el frontend, así que el parámetro se pudo
renombrar directo sin capa de compatibilidad) para acotar el export a ese nodo y sus
descendientes (mismo criterio que `taxonomyUuid` en `/catalog`/`/search`); omitido exporta
el árbol completo. Un botón "Exportar CSV" en el dashboard puede apuntar directo aquí.

Respuesta `200`: el CSV en sí (`Content-Type: text/csv`, no JSON), codificado
`utf-8-sig` (con BOM) para que los acentos se vean bien al abrirlo directo en Excel, con
`Content-Disposition: attachment; filename=categorias_productos.csv`. Una fila por par
(categoría, producto) — columnas `category_uuid, category_path, category_slug,
product_sku, product_name, product_price, product_stock`:

```csv
category_uuid,category_path,category_slug,product_sku,product_name,product_price,product_stock
3f9a1c2e-...,Herramientas > Electricas,herramientas-electricas,102959,"Compresor de aire, silencioso libre de aceite, 50 L, 2 HP",5250.00,1.000
6a4fd308-...,Ferreteria General,ferreteria-general,,,,
```

- `category_path` es la cadena completa de ancestros (`Padre > Hijo`), no solo el nombre
  del nodo — necesario porque dos categorías pueden compartir nombre bajo padres
  distintos. Se resuelve para **todas** las categorías, no solo las exportadas, así un
  export acotado con `taxonomyUuid` igual muestra los nombres reales de los ancestros
  aunque esos ancestros mismos queden fuera del subárbol exportado.
- Una categoría sin productos asignados (o cuyos productos asignados están todos
  eliminados) igual aparece, con las cuatro columnas `product_*` en blanco — no se omite
  del CSV.
- `product_stock` es `availableStock` (vendible ahora mismo, no el físico crudo) — mismo
  criterio que `ProductBasic.stock` en el storefront.
- `404` si `taxonomyUuid` no corresponde a una categoría real.

### Cupones (descuentos)

Endpoints para administrar códigos de cupón/descuento. Un cupón es `PERCENTAGE` (con tope
opcional `maxDiscountAmount`) o `FIXED_AMOUNT`, y su `scopeType` decide sobre qué parte del
carrito aplica el descuento: `ORDER` (el carrito completo), `CATEGORY` (solo las líneas cuyo
producto está en alguna de las categorías asignadas al cupón — vía `PUT .../categories`,
descendiente-inclusivo igual que el filtro `taxonomyUuid` de `/catalog`/`/search`) o
`PRODUCT` (solo las líneas de los productos asignados vía `PUT .../products`). Un cupón
`CATEGORY`/`PRODUCT` sin nada asignado todavía no es un error — simplemente no descuenta
nada hasta que se le asigne un alcance.

Cuatro mecanismos de límite de uso, combinables entre sí en el mismo cupón:
`maxTotalUses` (tope global), `maxUsesPerClient` (tope por cliente — usar `1` para "una vez
por cliente"), `firstPurchaseOnly` (solo clientes sin ninguna orden `PAID` previa), y
"código asignado" (vía `PUT .../clients`: si la lista de clientes elegibles no está vacía,
solo esos clientes pueden redimirlo — vacía significa público). También soporta ventana de
vigencia (`startsAt`/`endsAt`, ambos opcionales) y `minOrderAmount`.

**Política anti-abuso deliberada**: una vez que una orden llega a `PAID`, el uso del cupón
queda **consumido para siempre**, incluso si esa orden se cancela o reembolsa después — así
un cliente no puede aplicar-pagar-cancelar-reaplicar el mismo código repetidamente. Solo un
uso que nunca llegó a `PAID` (la orden se canceló/eliminó mientras seguía `TO_PAY`) libera el
cupo de vuelta. `GET .../redemptions` (abajo) muestra el historial completo con su `status`
(`PENDING`/`CONFIRMED`/`RELEASED`) para auditar esto.

El storefront solo ve el código en dos puntos: `POST`/`DELETE /v1/cart/coupon` (preview en el
carrito, no autoritativo) y `couponCode` en `POST /v1/orders` (donde se valida y bloquea de
verdad) — ver `FRONTEND_INTEGRATION.md`.

#### `POST /v1/admin/coupons` — crear un cupón

```http
POST /v1/admin/coupons
X-Admin-Key: <admin-key>
Content-Type: application/json

{
  "code": "WELCOME10",
  "discountType": "PERCENTAGE",
  "discountValue": 10,
  "maxDiscountAmount": 200,
  "scopeType": "ORDER",
  "minOrderAmount": 300,
  "maxUsesPerClient": 1,
  "firstPurchaseOnly": true
}
```

`code` se normaliza a mayúsculas/sin espacios al guardar y comparar — no importa cómo lo
teclee el cliente en el carrito. `maxDiscountAmount` solo es válido con
`discountType: "PERCENTAGE"` (`422` si se manda con `FIXED_AMOUNT`); `discountValue` no puede
exceder `100` para `PERCENTAGE` (`422`). `409` si ya existe un cupón con ese código.

Respuesta `201`: mismo shape que `GET /{uuid}` de abajo.

#### `GET /v1/admin/coupons` — buscar/listar cupones

```http
GET /v1/admin/coupons?isActive=true&code=WELCOME&limit=60&offset=0
X-Admin-Key: <admin-key>
```

Paginado (`limit`/`offset`, mismo estilo que el resto de este documento). `code` es
coincidencia parcial sin distinguir mayúsculas; `isActive` filtra exacto.

```json
{
  "total": 1,
  "docs": [
    {
      "uuid": "b2e4b3f0-...",
      "code": "WELCOME10",
      "discountType": "PERCENTAGE",
      "discountValue": 10,
      "maxDiscountAmount": 200,
      "scopeType": "ORDER",
      "minOrderAmount": 300,
      "startsAt": null,
      "endsAt": null,
      "isActive": true,
      "maxTotalUses": null,
      "maxUsesPerClient": 1,
      "firstPurchaseOnly": true,
      "createdAt": "2026-08-10T12:00:00Z",
      "updatedAt": null
    }
  ]
}
```

#### `GET /v1/admin/coupons/{uuid}` — detalle de un cupón

Mismo shape que un elemento de `docs` arriba. `404` si no existe.

#### `PATCH /v1/admin/coupons/{uuid}` — actualización parcial

Igual que categorías/vehículos: solo los campos incluidos en el body se tocan
(`exclude_unset`). Reutiliza las mismas validaciones de `POST` (`422` si el nuevo shape de
`discountType`/`maxDiscountAmount`/`discountValue`/fechas queda inconsistente; `409` si el
nuevo `code` ya lo usa otro cupón).

#### `DELETE /v1/admin/coupons/{uuid}` — eliminar un cupón

`204` sin cuerpo. `409` si el cupón **ya tiene algún uso registrado** (cualquier
`status` — `PENDING`, `CONFIRMED` o `RELEASED`) — preserva la integridad histórica de las
órdenes que lo usaron. Para retirar un cupón que ya se usó, usa `PATCH {"isActive": false}`
en vez de borrarlo.

Borrar un cupón **sí elimina en cascada** sus propias filas de alcance/elegibilidad
(`coupon_categories`/`coupon_products`/`coupon_assigned_clients`) — no hace falta vaciarlas
a mano primero con los `PUT` de abajo. Esto es distinto de borrar una categoría/producto que
un cupón todavía usa: eso sí se bloquea (`409`, ver `DELETE /v1/admin/categories/{uuid}`
arriba) — quitarlo del alcance del cupón primero.

#### `PUT`/`PATCH`/`GET /v1/admin/coupons/{uuid}/categories` — alcance de categorías

```http
PUT /v1/admin/coupons/b2e4b3f0-.../categories
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "categoryUuids": ["3f9a1c2e-..."] }
```

`PUT` — solo válido si el cupón tiene `scopeType: "CATEGORY"` (`409` si no). Reemplazo
completo del conjunto, no incremental. `404` si algún `categoryUuid` no existe.

**Nuevo (2026-08-13)**: `PATCH /v1/admin/coupons/{uuid}/categories` agrega/quita de forma
incremental, mismo patrón que `PATCH /v1/admin/categories/{uuid}/products` — pensado para
no arriesgar sobreescribir el alcance de un cupón con muchas categorías asignadas si el
picker del dashboard nunca cargó el conjunto completo:

```http
PATCH /v1/admin/coupons/b2e4b3f0-.../categories
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "add": ["3f9a1c2e-..."], "remove": ["6a4fd308-..."] }
```

Respuesta `200`:
```json
{ "couponUuid": "b2e4b3f0-...", "added": ["3f9a1c2e-..."], "removed": ["6a4fd308-..."], "addedCount": 1, "removedCount": 1 }
```

Mismo `409` de `scopeType` que el `PUT`. `add` ya asignadas se ignoran (idempotente);
`remove` no asignadas o inexistentes también se ignoran (no-op tolerante). `422` si una
misma categoría aparece en `add` y `remove` a la vez. `404` si alguna de `add` no resuelve a
una categoría real.

`GET /v1/admin/coupons/{uuid}/categories` — lectura del alcance actual (sin paginar, acotada
por naturaleza), para poblar la UI de edición antes de un `PUT`/`PATCH`:

```json
{ "docs": [ { "uuid": "3f9a1c2e-...", "name": "Herramientas Eléctricas", "slug": "herramientas-electricas", "parentUuid": null, "updatedAt": "2026-08-01T12:00:00Z" } ] }
```

#### `PUT`/`PATCH`/`GET /v1/admin/coupons/{uuid}/products` — alcance de productos

Mismo comportamiento que el de categorías arriba, pero para `scopeType: "PRODUCT"` — el `PUT`
recibe `productUuids` (resueltos por `sicar_uuid`, `404` si alguno no existe), y el nuevo
`PATCH` (2026-08-13) recibe `add`/`remove` con el mismo shape/semántica que el de categorías
(mismo `409` de `scopeType`). A diferencia del `GET` de categorías, `GET
/v1/admin/coupons/{uuid}/products` **sí está paginado** (`limit`/`offset`, respuesta
`{total, docs}`) — un cupón `PRODUCT` puede tener muchos productos asignados.

#### `PUT`/`PATCH`/`GET /v1/admin/coupons/{uuid}/clients` — lista de clientes elegibles

```http
PUT /v1/admin/coupons/b2e4b3f0-.../clients
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "clientEmails": ["vip@example.com"] }
```

`PUT` — resuelto por **email**, no por `uuid` de cliente — es lo que un admin tiene a mano.
Lista vacía = cupón público (cualquier cliente puede intentar redimirlo, sujeto a las demás
reglas); no vacía = solo esos clientes. `404` si algún email no corresponde a una cuenta
existente.

**Nuevo (2026-08-13)**: `PATCH /v1/admin/coupons/{uuid}/clients` agrega/quita de forma
incremental (`add`/`remove`, mismo shape que categorías/productos arriba, también resuelto
por email) — a diferencia de esas dos, no tiene chequeo de `scopeType` (la elegibilidad por
cliente es independiente del alcance de descuento). `add` sobre un cupón que todavía era
público (sin nadie asignado) lo vuelve restringido a esos clientes, mismo efecto que el
`PUT` — la diferencia es que no hace falta reenviar a nadie más ya asignado.

`GET /v1/admin/coupons/{uuid}/clients` — lectura paginada de los clientes elegibles
actuales (una campaña puede apuntar a muchos clientes):

```json
{ "total": 1, "docs": [ { "uuid": "a1b2c3-...", "email": "vip@example.com", "name": "Juan Pérez" } ] }
```

#### `GET /v1/admin/coupons/{uuid}/redemptions` — historial de usos

```http
GET /v1/admin/coupons/b2e4b3f0-.../redemptions?limit=60&offset=0
X-Admin-Key: <admin-key>
```

```json
{
  "total": 2,
  "docs": [
    {
      "id": 41,
      "clientAccountId": 7,
      "clientEmail": "juan@example.com",
      "orderUuid": "f1a2b3c4-...",
      "status": "CONFIRMED",
      "createdAt": "2026-08-10T12:05:00Z",
      "updatedAt": "2026-08-10T12:08:00Z"
    }
  ]
}
```

`status`: `PENDING` (orden creada, esperando llegar a `PAID`), `CONFIRMED` (orden llegó a
`PAID` — uso consumido para siempre), `RELEASED` (la orden se canceló/eliminó antes de
pagarse — el cupo se liberó y el cliente puede volver a usar el código, sujeto a los demás
límites).

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

**Cuidado con vehículos con más de 200 productos asignados**: mismo riesgo que en
categorías (ver nota arriba) — `GET .../products` está limitado a `limit ≤ 200`, y un
`PUT` guardado desde un picker que solo cargó una página sobreescribe silenciosamente el
resto. Usar el `PATCH` de abajo para ediciones incrementales.

#### `PATCH /v1/admin/vehicles/{uuid}/products` — agregar/quitar productos de forma incremental

```http
PATCH /v1/admin/vehicles/8f2c1a4e-.../products
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "add": ["3Cny4OOxdX1GoSzL9rEsTZNL7un"], "remove": [] }
```

Mismo comportamiento incremental que el equivalente de categorías arriba: agrega/quita
sin tocar el resto del conjunto asignado. `add` de un producto ya asignado es un no-op;
`remove` de uno no asignado o inexistente también se ignora.

Respuesta `200`:
```json
{ "vehicleUuid": "8f2c1a4e-...", "added": ["3Cny4OOxdX1GoSzL9rEsTZNL7un"], "removed": [], "addedCount": 1, "removedCount": 0 }
```

- `422` si un mismo `productUuid` aparece en `add` y `remove` a la vez.
- `404` si el vehículo no existe, o si algún `productUuid` de `add` no resuelve.

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
— esa dirección ya la cubre `GET /v1/admin/products/{uuid}/attributes` de arriba.

Respuesta `200` — mismo shape que el equivalente de categorías/vehículos, **más `value`** (el
valor guardado de este atributo para cada producto — a diferencia de categorías/vehículos,
que son solo membresía sin valor), pensado para precargar la UI de edición antes de un `PUT`
de abajo:
```json
{
  "total": 2,
  "docs": [
    { "sicarUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "sku": "PR2057", "name": "PORTAROLLO ROJO", "descriptionDetails": null, "imageUrl": null, "price": 8.62, "stock": 2.0, "availableStock": 2.0, "salesCount": 15.0, "value": "Rojo" }
  ]
}
```

`404` si el atributo no existe.

#### `PUT /v1/admin/attributes/{uuid}/products` — asignar este atributo a un lote de productos

```http
PUT /v1/admin/attributes/8bdb99f9-.../products
X-Admin-Key: <admin-key>
Content-Type: application/json

{
  "values": [
    { "productUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "value": "Rojo" },
    { "productUuid": "7Bqz2PPydY2HpTaM0sFuUANM8vo", "value": "Azul" }
  ]
}
```

**Dirección atributo-primero** — asigna/actualiza este atributo en un lote de productos de
una sola llamada, en vez de un `PUT /v1/admin/products/{uuid}/attributes` por producto.
Complementa esa ruta (dirección producto-primero, reemplaza *todos* los atributos de un
producto): esta ruta **solo toca la clave de este atributo** en cada producto — cualquier
otro atributo que el producto ya tuviera guardado no se toca.

**Reemplazo completo, no incremental** (mismo criterio que categorías/vehículos/grupos de
variantes) — el conjunto de productos con este atributo asignado queda exactamente igual a
`values`: un producto que ya lo tenía y no aparece aquí **pierde la clave** (sus demás
atributos siguen intactos), y los que sí aparecen quedan con el `value` dado (nuevo o
actualizado). Una lista vacía (`{ "values": [] }`) quita el atributo de todos los productos
que lo tuvieran.

Cada `value` se valida server-side contra el `dataType`/`allowedValues` de este atributo
**antes de escribir nada** — igual que `PUT /v1/admin/products/{uuid}/attributes`:

- `404` si algún `productUuid` no resuelve a un producto real y no eliminado (nombra cuáles).
- `422` si algún `value` no coincide con el `dataType`, o (para `ENUM`) no está en
  `allowedValues` (nombra el/los producto(s) y por qué).

Respuesta `200`:
```json
{
  "attributeUuid": "8bdb99f9-...",
  "docs": [
    { "productUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "value": "Rojo" },
    { "productUuid": "7Bqz2PPydY2HpTaM0sFuUANM8vo", "value": "Azul" }
  ]
}
```
`docs` es un eco minimalista de lo que quedó aplicado (sin datos de producto — usa `GET
.../products` de arriba, que sí los trae, para la UI de listado/edición).

**Cuidado con atributos con más de 200 productos asignados**: mismo riesgo que en
categorías/vehículos (ver notas arriba) — `GET .../products` está limitado a `limit ≤
200`, y un `PUT` guardado desde un picker que solo cargó una página le quita
silenciosamente la clave a los productos no cargados. Usar el `PATCH` de abajo para
ediciones incrementales.

#### `PATCH /v1/admin/attributes/{uuid}/products` — asignar/actualizar o quitar el valor de este atributo, de forma incremental

```http
PATCH /v1/admin/attributes/8bdb99f9-.../products
X-Admin-Key: <admin-key>
Content-Type: application/json

{
  "upsert": [{ "productUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "value": "Verde" }],
  "remove": ["7Bqz2PPydY2HpTaM0sFuUANM8vo"]
}
```

**Incremental** — a diferencia del `PUT` de arriba, no necesita conocer el conjunto
completo: `upsert` asigna/actualiza el `value` de este atributo en cada producto listado
(sin tocar sus demás atributos guardados), `remove` le quita la clave a los productos
listados (sin tocar el resto). `remove` de un producto que no tenga la clave, o que no
exista, se ignora (no-op tolerante).

Respuesta `200`:
```json
{
  "attributeUuid": "8bdb99f9-...",
  "upserted": [{ "productUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "value": "Verde" }],
  "removed": ["7Bqz2PPydY2HpTaM0sFuUANM8vo"],
  "upsertedCount": 1,
  "removedCount": 1
}
```

- `422` si un mismo `productUuid` aparece en `upsert` y `remove` a la vez, o si algún
  `value` no coincide con el `dataType`/`allowedValues` de este atributo (nombra
  cuáles) — no escribe nada hasta que todos pasen.
- `404` si el atributo no existe, o si algún `productUuid` de `upsert` no resuelve a un
  producto real y no eliminado.

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

**Cuidado con grupos con más de 200 productos asignados**: mismo riesgo que en
categorías/vehículos/atributos (ver notas arriba) — un `PUT` guardado desde un picker que
solo cargó una página de `GET .../products` expulsa silenciosamente del grupo a los
productos no cargados. Usar el `PATCH` de abajo para ediciones incrementales.

#### `PATCH /v1/admin/variant-groups/{uuid}/products` — agregar/quitar productos de forma incremental

```http
PATCH /v1/admin/variant-groups/b52bf1c5-.../products
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "add": ["3Cny4OOxdX1GoSzL9rEsTZNL7un"], "remove": ["7Bqz2PPydY2HpTaM0sFuUANM8vo"] }
```

**Incremental** — a diferencia del `PUT` de arriba, no necesita conocer el conjunto
completo de miembros. `add` reasigna `variantGroupUuid` incondicionalmente (si un
producto ya pertenecía a otro grupo, esta llamada gana). `remove` solo quita la
pertenencia si el producto **sigue perteneciendo a este grupo** en el momento de la
llamada — a diferencia de vaciar y reasignar todo el grupo como hace el `PUT`, esta
guarda evita que un `remove` le quite por accidente la pertenencia a un producto que
mientras tanto ya fue movido a otro grupo por una llamada distinta.

Respuesta `200`:
```json
{ "variantGroupUuid": "b52bf1c5-...", "added": ["3Cny4OOxdX1GoSzL9rEsTZNL7un"], "removed": ["7Bqz2PPydY2HpTaM0sFuUANM8vo"], "addedCount": 1, "removedCount": 1 }
```

- `422` si un mismo `productUuid` aparece en `add` y `remove` a la vez.
- `404` si el grupo no existe, o si algún `productUuid` de `add` no resuelve.

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

### Dashboard de ventas

Tres endpoints de solo lectura para alimentar un dashboard de ventas: KPIs/serie diaria,
productos más vendidos y ventas por categoría — todos filtrables por rango de fechas.
Ninguno persiste nada; todos agregan en vivo sobre `orders` (y, para categorías, también
`products`/`product_categories`/`categories`) en cada llamada.

**Rango de fechas, común a los tres**: `startDate`/`endDate` (`YYYY-MM-DD`), ambos
opcionales. Si se omiten, por defecto cubre los **últimos 30 días** (`endDate` = hoy UTC,
`startDate` = `endDate - 29 días`). El rango es inclusivo de ambos extremos (incluye todo
`endDate` completo, no solo hasta medianoche). Los tres solo consideran pedidos
`status: "PAID"` y no eliminados (`deletedAt: null`) — pedidos `TO_PAY`/`CANCELLED` nunca
cuentan como venta.

#### `GET /v1/admin/dashboard/summary` — KPIs y serie diaria

```http
GET /v1/admin/dashboard/summary?startDate=2026-07-01&endDate=2026-08-05
X-Admin-Key: <admin-key>
```

Respuesta `200`:
```json
{
  "startDate": "2026-07-01",
  "endDate": "2026-08-05",
  "totalRevenue": 458320.50,
  "orderCount": 612,
  "averageOrderValue": 748.89,
  "totalUnitsSold": 2140,
  "daily": [
    { "date": "2026-07-01", "revenue": 12500.00, "orderCount": 18, "unitsSold": 64 }
  ]
}
```

`averageOrderValue` es `0` (no un error) si `orderCount` es `0` en el rango pedido.
`daily` viene ordenado por fecha ascendente y solo incluye días con al menos un pedido
`PAID` — un día sin ventas simplemente no aparece en el arreglo (el dashboard debe rellenar
huecos con `0` si necesita un eje continuo para graficar).

#### `GET /v1/admin/dashboard/top-products` — productos más vendidos

```http
GET /v1/admin/dashboard/top-products?startDate=2026-07-01&endDate=2026-08-05&sortBy=revenue&limit=20&offset=0
X-Admin-Key: <admin-key>
```

- `sortBy` — `"revenue"` (default) o `"quantity"`.
- `limit` (1-100, default 20), `offset` (≥0, default 0).

Respuesta `200`:
```json
{
  "total": 314,
  "docs": [
    { "productUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "sku": "PR2057", "name": "Taladro 1/2\"", "imageUrl": "https://.../taladro.jpg", "unitsSold": 42, "revenue": 37758.00 }
  ]
}
```

`total` es la cantidad de productos **distintos** vendidos en el rango (para paginar), no
la cantidad de unidades. `name`/`sku`/`imageUrl` vienen de la foto fija guardada en cada
pedido al momento de venderse (`Order.items`), **no** del catálogo actual — un producto
renombrado, re-precificado o marcado como eliminado después de venderse sigue apareciendo
aquí con los datos vigentes al momento de cada venta.

#### `GET /v1/admin/dashboard/top-categories` — ventas por categoría

```http
GET /v1/admin/dashboard/top-categories?startDate=2026-07-01&endDate=2026-08-05&sortBy=revenue&limit=20&offset=0
X-Admin-Key: <admin-key>
```

Mismos parámetros que `top-products` (`sortBy`/`limit`/`offset`). Agrupa por el árbol de
categorías propio (`GET/POST/PATCH /v1/admin/categories`), no por el `departmentUuid`/
`categoryUuid` sincronizado de Sicar X.

Respuesta `200`:
```json
{
  "total": 18,
  "docs": [
    { "categoryUuid": "3f9a1c2e-...", "categoryName": "Herramientas Eléctricas", "unitsSold": 310, "revenue": 89250.00 }
  ]
}
```

Dos comportamientos a tener en cuenta al construir la UI:
- **Un producto asignado a varias categorías suma su revenue/unidades completas a cada
  una** — esto es un desglose por faceta (como los filtros del storefront), no una
  partición del total. Sumar `revenue` de todas las filas de `docs` **no** debe usarse
  como "revenue total" (para eso está `GET .../summary`).
- **Productos sin ninguna categoría asignada quedan fuera de este reporte por completo**
  — no hay una fila "Sin categoría". Revenue de esos productos solo aparece en
  `/top-products` y `/summary`.

### Reseñas (moderación)

Cualquier cliente autenticado puede reseñar cualquier producto (no se exige compra —
`isVerifiedPurchase` es un badge informativo, no un requisito). Estas rutas son la
única forma de moderar ese contenido: ocultar/mostrar, responder oficialmente, o
eliminar de forma permanente. Ver `FRONTEND_INTEGRATION.md` para las rutas
storefront-facing (listar, crear, editar, marcar útil).

#### `GET /v1/admin/reviews` — buscar/listar reseñas (todos los productos)

```http
GET /v1/admin/reviews?productSku=HV-3617&isHidden=false&limit=50&offset=0
X-Admin-Key: <admin-key>
```

Filtros, todos opcionales y combinables: `productUuid`, `productSku` (coincidencia
exacta contra `Product.sku`, sin distinguir mayúsculas/minúsculas — útil cuando el
admin tiene el código del producto a la mano pero no su `uuid`), `clientEmail`,
`clientUuid`, `rating` (1-5), `isHidden`, `hasReply`. Sin filtros, devuelve todas las
reseñas de todos los productos (incluidas las ocultas), más recientes primero.

Respuesta `200`:
```json
{
  "total": 1,
  "docs": [
    {
      "uuid": "c2a8ff9a-63c4-43d2-9ce3-57aebc0e8be6",
      "productUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un",
      "clientUuid": "3e0b5f17-f658-4090-a6e5-269ad4d5cb75",
      "clientEmail": "juan@example.com",
      "clientName": "Juan Pérez",
      "rating": 4,
      "comment": "Buen producto, cumple lo esperado.",
      "isVerifiedPurchase": true,
      "isHidden": false,
      "hiddenReason": null,
      "helpfulCount": 3,
      "adminReply": null,
      "adminReplyAt": null,
      "createdAt": "2026-08-10T14:58:23.190116Z",
      "updatedAt": null
    }
  ]
}
```

#### `PATCH /v1/admin/reviews/{reviewUuid}` — ocultar/mostrar una reseña

```http
PATCH /v1/admin/reviews/c2a8ff9a-.../
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "isHidden": true, "hiddenReason": "Lenguaje inapropiado" }
```

Actualización parcial (`isHidden`/`hiddenReason`, ambos opcionales — solo se toca lo
enviado). Ocultarla la excluye de inmediato de la vista pública del producto **y** de
`averageRating`/`reviewsCount`/`ratingBreakdown` (se recalculan en la misma llamada) —
el propio autor la sigue viendo en su propio historial (`GET /v1/auth/me/reviews`).
Reversible: volver a mandar `isHidden: false` la restaura.

#### `DELETE /v1/admin/reviews/{reviewUuid}` — eliminar permanentemente

`204` sin cuerpo. A diferencia de `PATCH .../isHidden`, esto es un borrado real, no
reversible — para contenido genuinamente abusivo o ilegal, no para moderación
cotidiana (para eso usa ocultar).

#### `PUT` / `DELETE /v1/admin/reviews/{reviewUuid}/reply` — respuesta oficial

Una sola respuesta por reseña (no un hilo de comentarios) — `PUT` con `{"comment": "..."}`
crea o **reemplaza** la respuesta existente si ya había una; `DELETE` la quita. Ambos
responden el mismo shape que `GET`/`PATCH` de arriba.

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
