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

`action` es `"CANCEL"` (cancelación de un pedido) o `"ACCEPT"` (avanzar `dispatchStatus` de
`PENDING_ACCEPTANCE` a `PENDING` en Sicar X, ver `POST .../accept` abajo). Una fila `FAILED`
significa que se agotaron los 5 intentos con backoff exponencial (1/2/4/8/16 min) — el pedido
sigue correcto del lado local (el estado local ya se aplicó de inmediato cuando se disparó la
acción), pero Sicar X todavía no se enteró; hace falta reconciliar manualmente en el panel nativo
de Sicar X, o reintentar con el siguiente endpoint una vez resuelta la causa.

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
  "syncStatus": "QUEUED",
  "note": "La aceptacion local ya se aplico; el avance de dispatchStatus en Sicar X se procesa de forma asincrona via sicar_sync_outbox (normalmente en menos de un minuto)."
}
```

**Esto es asíncrono, no instantáneo del lado de Sicar X** — `acceptedAt`/`acceptedBy` se aplican
de inmediato en Postgres (autoritativo desde el punto de vista de este backend), pero avanzar el
`dispatchStatus` real en Sicar X (`PENDING_ACCEPTANCE` → `PENDING`) ocurre en el siguiente ciclo
del worker (cada minuto), vía la misma cola `sicar_sync_outbox` que ya usa la cancelación. Si
necesitas confirmar que ya se sincronizó, vuelve a pedir `GET /v1/admin/orders/{orderUuid}` un poco
después y revisa `dispatchStatus` (debería pasar de `PENDING_ACCEPTANCE` a `PENDING`), o consulta
`GET /v1/admin/sync/outbox?status=FAILED` si sospechas que algo no se sincronizó. `404` si el
pedido no existe (o está soft-deleted). `409` si el pedido ya fue aceptado antes (`acceptedAt` ya
tenía un valor) — no se puede "re-aceptar".

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

## Notas y advertencias

- **No hay reintentos automáticos si tu backend falla al llamar estas rutas** — a diferencia del
  worker interno (que sí reintenta `ACCEPT`/`CANCEL` contra Sicar X), un error de red o un `5xx`
  al llamar `/v1/admin/*` desde el dashboard no se reintenta solo; implementa tu propio reintento
  si lo necesitas.
- **`dispatchStatus` puede seguir en `PENDING_ACCEPTANCE` un rato después de aceptar** — es
  esperado mientras el worker no haya corrido su siguiente ciclo (hasta ~1 minuto). Si sigue ahí
  después de varios minutos, revisa `GET /v1/admin/sync/outbox?status=FAILED` — probablemente el
  pedido ya está cancelado en Sicar X (un `409 "Document is canceled"` es la causa más común) o
  hay un problema de token/red con Sicar X.
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
