# Conectando el frontend a API SICARX

Contrato entre el storefront (Next.js) de Ferretería Charly y esta API. El panel admin tiene su
propio documento: `ADMIN_INTEGRATION.md`.

## Generalidades

| Entorno | URL |
|---|---|
| Producción | `https://api-production-cf7a.up.railway.app` |
| Local | `http://127.0.0.1:8000` |

- Todas las rutas van bajo `/v1`.
- **CORS**: el origen debe estar en `origins` (`app/main.py`). Hoy: `http://localhost`, `http://localhost:8000`, `https://ferreteriacharly.com`, `https://api-production-cf7a.up.railway.app`. Pide que se agregue cualquier otro (p. ej. `localhost:3000`, previews).
- Respuestas siempre en camelCase. Los bodies todavía aceptan snake_case, pero no dependas de eso.
- Errores: `{"detail": "..."}`. **`detail` siempre es string**, también en `422` (p. ej. `"products: Field required; deliveryInfo: Field required"`).
- `price` es el precio final **con IVA**, con 2 decimales exactos.
- `stock` en cualquier respuesta del storefront es el **disponible** (ya descontado lo reservado por pedidos en curso).
- Paginación típica: `limit` 1-200 (default 60), `offset` ≥ 0; fuera de rango → `422`.

## Autenticación

| Mecanismo | Dónde |
|---|---|
| `x-api-key: <valor de backend>` | **Todas** las rutas. Sin él → `403`. (Única excepción: `POST /v1/payments/webhook`, que llama Mercado Pago, no el frontend.) |
| `Authorization: <token de cliente>` (`Bearer` opcional) | `/v1/auth/me*`, `/v1/auth/resend-verification`, `POST /v1/cart/merge`, reseñas (escritura), `/v1/wishlist/*`, `/v1/auth/me/stock-alerts*` |
| `X-Client-Token: <token de cliente>` | **Opcional** en `/v1/orders*`, `/v1/cart*` (excepto merge) y `POST /v1/products/{uuid}/stock-alerts`. Ausente = invitado/anónimo; presente pero inválido/expirado = `401`. |
| Cookie `charly_cart_token` (httpOnly, path `/v1/cart`) | Carrito anónimo. La maneja el navegador; **toda** llamada a `/v1/cart*` debe usar `credentials: "include"` (cookie cross-site). |

El token de cliente es el `token` de `register`/`login`/`google`/`reset-password`. Revisa con
cuidado qué cabecera usa cada ruta: no es lo mismo `Authorization` que `X-Client-Token`.

Si tu frontend hace render del lado del servidor, manda `X-Frontend-Auth` válido junto con
`X-Client-IP` y `X-Client-User-Agent` del visitante (se usan para rate limiting y para el
historial de consentimiento).

## Flujo de compra

```
1. (opcional) POST /v1/auth/register | /login | /google   → token de cliente
2. POST /v1/products, POST /v1/search, GET /v1/products/{uuid}
2b. (solo DELIVERYMAN) POST /v1/shipping/quotes → opciones de envío con precio; el cliente elige una
3. POST /v1/orders                     → reserva el pedido (TO_PAY), devuelve id/orderUuid/total (con envío)
4. Payment Brick con initialization.amount = total
5. POST /v1/orders/{id}/pay            → cobra (tarjeta/OXXO) desde el onSubmit del Brick
6. (si aplica) POST /v1/orders/{id}/cancel
```

`POST /v1/orders` **no cobra**. `/v1/cart` es persistencia opcional; `/v1/orders` recibe el
carrito en su propio body y no lee `/v1/cart`. La disponibilidad se valida contra el catálogo
local (se sincroniza con Sicar X cada 5 min), así que un `409` en checkout es normal.

---

## Cuentas de cliente

Objeto `client` (`GET /v1/auth/me` y respuestas de auth):

```json
{
  "uuid": "f6bacfb9-...",
  "name": "Juan Pérez",
  "email": "juan@example.com",
  "phone": "3151234567",
  "isVerified": false,
  "authProvider": "local",
  "marketingEmails": false,
  "marketingConsentAt": null,
  "addresses": [ { "...": "ver Direcciones" } ]
}
```

- `authProvider`: `"local"` | `"google"`. Las cuentas Google no tienen contraseña; oculta "cambiar contraseña".
- `isVerified` no bloquea nada; úsalo para un banner.
- El correo no distingue mayúsculas.

### `POST /v1/auth/register` → `201`

```json
{
  "name": "Ana López", "email": "ana@example.com", "phone": "3151234567",
  "password": "********",
  "cartToken": "5a5c479d-...",
  "termsAccepted": true, "termsVersion": "2026-10-12",
  "marketingEmails": true,
  "marketingConsentSource": "register_checkbox",
  "privacyNoticeVersion": "2026-10-12"
}
```

- `password` ≥ 8 caracteres (`422`).
- `cartToken` (opcional): fusiona un carrito anónimo; si es inválido se ignora.
- Responde `{ token, client, cart }` y deja la sesión iniciada. `cart` siempre viene (con `cartToken: null`).
- Dispara el webhook `verification-requested`.
- `409` si el correo ya existe (mensaje distinto si está ligado a Google).

### `POST /v1/auth/login` → `200`

`{ email, password, cartToken? }` → `{ token, client, cart }`. `401` si las credenciales son incorrectas, `403` si la cuenta está desactivada, `429` después de 5 intentos por minuto por IP.

### `POST /v1/auth/google` → `200`

`{ idToken, cartToken?, marketingEmails?, marketingConsentSource?: "google_signup", privacyNoticeVersion? }`.
`idToken` es el ID token que el frontend obtiene de Google Identity Services. Si no existe la
cuenta, la crea. `409` si el correo ya tiene cuenta local con contraseña (no se fusionan; el
usuario debe entrar con contraseña). `403` si la cuenta está desactivada. 10 por minuto.

### Verificación de correo

- `POST /v1/auth/verify-email` `{ token }`: sin sesión. Responde el `client` con `isVerified: true`. Es idempotente; `401` si el token es inválido o venció (vigencia de 24 h). 10 por minuto.
- `POST /v1/auth/resend-verification` (`Authorization`): `204`; `400` si ya está verificada. 5 por minuto.
- Al verificarse, los pedidos de invitado hechos con ese correo se ligan solos a la cuenta.

### Recuperar contraseña

- `POST /v1/auth/forgot-password` `{ email }`: **siempre** `200` `{ "detail": "Si el correo existe, se enviará un enlace de recuperación." }`. Si la cuenta existe, dispara `password-reset-requested`. Un nuevo pedido invalida los tokens anteriores. 5 por minuto.
- `POST /v1/auth/reset-password` `{ token, newPassword }`: sin sesión. Responde `{ token, client, cart }` con la sesión iniciada. `400` si el token es inválido, ya se usó o venció (30 min). Cierra las demás sesiones de la cuenta. 10 por minuto.

### `GET` / `PATCH /v1/auth/me` (`Authorization`)

- `GET`: el objeto `client` con `addresses` incluidas. `401` → manda al login.
- `PATCH` (todo opcional): `name`, `phone`, `marketingEmails`, `marketingConsentSource?: "account_toggle"`, `privacyNoticeVersion?`. Para cambiar la contraseña manda `currentPassword` + `newPassword` (≥ 8): `401` si la actual no coincide, `400` si la cuenta es Google. El cambio de contraseña cierra las demás sesiones. No cambia `email`. 10 por minuto.

### Direcciones `/v1/auth/me/addresses` (`Authorization`)

```json
{
  "uuid": "51cbf02f-...", "label": "Casa",
  "street": "Av. Siempre Viva", "extNumber": "123", "intNumber": null,
  "neighborhood": "Centro", "city": "Culiacán", "county": "Culiacán",
  "state": "Sinaloa", "country": "México", "zipCode": "80000",
  "references": null, "latitude": 24.809062, "longitude": -107.394012,
  "isDefault": true
}
```

- `GET`: lista.
- `POST`: `201`. Solo `street` es obligatorio. `zipCode`, si viene, debe tener 5 dígitos (`422`).
- `PATCH /{uuid}`: parcial.
- `DELETE /{uuid}`: `204`.
- `404` si el uuid no existe o es de otra cuenta.
- `isDefault: true` quita el default anterior.
- Para usar una dirección en entrega a domicilio deben estar llenos `street`, `extNumber`, `neighborhood`, `city`, `county`, `state` y `zipCode` (el pedido da `400` si falta alguno).
- La API no geocodifica. Resuelve estado, municipio, colonias y coordenadas desde el navegador con `GET https://geocodes.envia.com/zipcode/{country}/{zipcode}` (sin key, con CORS) y manda los campos ya resueltos.

---

## Catálogo

Objeto `ProductBasic` (listados, búsqueda, feeds y favoritos):

```json
{
  "sicarUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un",
  "sku": "PR2057", "name": "PORTAROLLO", "description": null, "brand": null,
  "imageUrl": null, "price": 10.0,
  "compareAtPrice": null, "saleEndsAt": null,
  "stock": 2.0, "salesCount": 0.0,
  "availability": "IN_STOCK", "backorderLeadDays": null,
  "averageRating": null, "reviewsCount": 0
}
```

- `imageUrl`: la foto de Sicar X; si no hay, la del proveedor (URREA). Lo mismo en carrito, pedidos, sugerencias y webhooks. Las de URREA vienen de `uhpproductosdev.blob.core.windows.net`: agrégalo a `images.remotePatterns` de Next.
- `salesCount`: unidades vendidas en pedidos pagados. Es numérico y puede tener decimales.
- **Venta sobre pedido**: `availability` es `IN_STOCK` (hay existencia), `BACKORDER` (no hay, pero se puede pedir al proveedor) u `OUT_OF_STOCK` (no se puede comprar). `backorderLeadDays` no nulo significa que el cliente puede pedir **más** de `stock` (también con `IN_STOCK`) y que lo que exceda tarda unos N días hábiles extra. Badge sugerido para `BACKORDER`: "Sobre pedido · entrega en ~N días". `stock` sigue siendo la existencia real; el filtro `inStock` no incluye productos sobre pedido. Cuando la venta sobre pedido está apagada, `backorderLeadDays` es `null` y no hay `BACKORDER`.
- `averageRating`: `null` si no hay reseñas (no es 0 estrellas).
- `compareAtPrice`/`saleEndsAt`: precio anterior (tachado, con IVA) y fin de la oferta. Son `null` sin oferta vigente; cuando hay, `compareAtPrice > price`. La oferta se apaga sola. Porcentaje: `round((1 - price/compareAtPrice) * 100)`. Siempre se cobra `price`.

### `POST /v1/products`: listado

```json
{
  "limit": 60, "offset": 0,
  "departmentUuid": null, "categoryUuid": null, "taxonomyUuid": null, "vehicleUuid": null,
  "brand": null, "hasBrand": null, "tag": null,
  "inStock": false, "onSale": null,
  "minPrice": null, "maxPrice": null,
  "sortBy": "relevance"
}
```

Respuesta: `{ "total": 124149, "docs": [ProductBasic], "priceRange": { "min": 12.0, "max": 8450.0 } }`.

| Filtro | Significado |
|---|---|
| `departmentUuid` / `categoryUuid` | Clasificación cruda de Sicar X |
| `taxonomyUuid` | Nodo del árbol de `GET /v1/taxonomy`; incluye descendientes |
| `vehicleUuid` | Fitment resuelto en `GET /v1/vehicles` |
| `brand` | Exacto, sin distinguir mayúsculas (no substring). Usa valores de `GET /v1/products/brands` |
| `hasBrand` | `false` = solo "Sin marca"; `true` = con marca |
| `tag` | Coincidencia exacta contra `tags` |
| `inStock` | Solo con existencia |
| `onSale` | Solo con oferta vigente |
| `minPrice` / `maxPrice` | Pesos con IVA contra `price` (nunca `compareAtPrice`), inclusivos; `422` si son negativos o si `min > max` |

`sortBy`: `"relevance"` | `"price_asc"` | `"price_desc"` | `"name_asc"` (otro valor → `422`).
`relevance` ordena: con existencia y con foto → con existencia sin foto → agotado con foto →
agotado sin foto; dentro de cada grupo, `salesCount` desc y luego nombre. Todos los órdenes son
estables entre páginas; sin `sortBy` el orden es estable pero arbitrario.

`priceRange` es el mínimo y máximo de `price` (solo > 0) del conjunto filtrado **sin**
`minPrice`/`maxPrice`. Es `null` si ese conjunto está vacío.

### `POST /v1/search`

Mismo body que `/v1/products` más `q` (obligatorio, no vacío). Misma respuesta.

- Tolera typos, mayúsculas, acentos, plural y singular, `10mm` = `10 mm`, `1-1/2` = `1 1/2`, orden de palabras y los sinónimos que administra el equipo.
- Encuentra palabras completas o su inicio, no texto dentro de otra palabra (`pvc` no trae CPVC).
- Un SKU exacto siempre sale primero.
- `sortBy` por defecto es `"relevance"`: coincidencia de texto > el nombre empieza con una palabra buscada > con existencia > con foto > `salesCount`. Cualquier otro `sortBy` reemplaza este orden. **Usa `relevance` como default en resultados de búsqueda** (hoy el selector de la tienda no ofrece esa opción).
- Si el motor no está disponible, responde con el mismo formato usando la búsqueda de base de datos.

### `GET /v1/search/suggest?q=tru&limit=6`

Autocompletado (llámalo con debounce de ~200 ms). `q` de 1 a 100 caracteres; `limit` de 1 a 10.

```json
{
  "products": [ { "sicarUuid": "...", "sku": "16704", "name": "Martillo ... TRUPER", "imageUrl": "https://...", "price": 289.0, "compareAtPrice": null, "saleEndsAt": null } ],
  "brands": [ { "name": "Truper", "count": 6305 } ]
}
```

`brands` (hasta 4) son marcas que empiezan con lo escrito y sirven como filtro `brand`. Viene `[]` si el motor no está disponible.

### `GET /v1/products/{uuid}`: detalle

`404` si no existe o está descontinuado u oculto. Incluye los campos de `ProductBasic` más:

```json
{
  "id": 40213, "additionalSkus": null, "tags": ["oferta"], "additionalImages": null,
  "salesUnitUuid": "0b8b...", "unitShortName": "PZA",
  "departmentUuid": "...", "categoryUuid": "...",
  "isBulk": false, "isActive": true, "isDeleted": false,
  "lastSyncId": "...", "detailsUpdatedAt": "...", "deletedAt": null,
  "bulletPoints": "-Fabricado en acero.\n-Soporte fijo.",
  "technicalSpecs": "-Capacidad: 26 - 65\"\n-Color: Negro",
  "contents": "Manual de usuario",
  "packageDimensions": { "weightKg": 2.06, "lengthCm": 16.0, "widthCm": 66.0, "heightCm": 4.0 },
  "supplierMedia": { "videoUrl": "https://www.youtube.com/embed/aSYB_sNFd_U", "datasheetUrl": "https://...pdf", "manualUrl": null },
  "attributes": [
    { "attributeUuid": "...", "name": "Voltaje", "slug": "voltaje", "dataType": "NUMBER", "unit": "V", "value": 12.5 }
  ],
  "variantGroup": {
    "uuid": "...", "name": "Portarollo acero inoxidable", "variantAttributeSlug": "color",
    "siblings": [ { "uuid": "...", "sku": "PR2058", "name": "...", "imageUrl": null, "price": 8.62, "stock": 4.0, "value": "Azul" } ]
  }
}
```

- `tags`, `additionalImages` y `additionalSkus` pueden ser `null`. `id` y `lastSyncId` son internos.
- La primera vez (o si `detailsUpdatedAt` tiene más de 24 h) tarda un poco más, porque refresca datos de Sicar X.
- `description`, `brand`, `bulletPoints`, `technicalSpecs` y `contents` los captura el admin y suelen venir en `null`. `bulletPoints` y `technicalSpecs` traen `\n`: respétalos al mostrarlos.
- `attributes` es `[]` si no hay. El tipo de `value` depende de `dataType` (`TEXT`/`ENUM` → string, `NUMBER` → number, `BOOLEAN` → boolean).
- `variantGroup` es `null` si el producto no pertenece a un grupo. `siblings` excluye al producto actual. `value` es el valor de `variantAttributeSlug` de cada sibling, para armar el selector de variantes; si `variantAttributeSlug` es `null`, usa `name`/`sku`.
- `packageDimensions`: peso (kg) y medidas (cm) del empaque de una pieza; `null` si el producto no las tiene. Sirve para mostrarlas en la ficha y, a futuro, para cotizar el envío.
- `availability`/`backorderLeadDays` como en `ProductBasic`.
- `supplierMedia`: video y PDFs del proveedor; `null` si no hay ninguno, y cada campo puede ser `null`. `videoUrl` ya es una URL de embed de YouTube (va directo en un `<iframe>`). `datasheetUrl` (ficha técnica) y `manualUrl` son PDFs, en `uhpproductosdev.blob.core.windows.net` o `medios.urrea.com`: ábrelos en otra pestaña.
- `attributes`, `variantGroup`, `bulletPoints`, `technicalSpecs`, `contents`, `packageDimensions` y `supplierMedia` **solo** vienen en esta ruta.

### `GET /v1/products/{uuid}/availability?quantity=N`: ¿se puede comprar esa cantidad?

Para el selector de cantidad cuando el cliente pide más de `stock` y `backorderLeadDays` no es nulo. Llámalo **desde el navegador** después de pintar la página (no en SSR): si hace falta, consulta al proveedor (hasta ~2 s; la respuesta se reutiliza 10 min). Límite: 30 por minuto. `404` si el producto no existe o no está a la venta.

```json
{ "quantity": 5, "stock": 2, "backorderQuantity": 3, "available": true, "leadDays": 5 }
```

- `available: true`: se puede; `backorderQuantity` piezas irán sobre pedido y tardan `leadDays` días hábiles extra. Si `quantity <= stock`, responde `true` sin consultar al proveedor.
- `available: false`: no se puede esa cantidad (`backorderQuantity: 0`). Limita el selector a `stock`.
- `available: null`: no se pudo confirmar con el proveedor ahora. Permite seguir; `POST /v1/orders` vuelve a confirmar.

### Feeds de portada (GET, sin paginar, sin `total`)

- `GET /v1/products/best-sellers?limit=10`: `limit` de 1 a 50. Filtros: `departmentUuid`, `categoryUuid`, `taxonomyUuid`, `vehicleUuid`, `inStock`. Solo productos con `salesCount > 0`, de mayor a menor.
- `GET /v1/products/available-now?limit=12`: mismos filtros (sin `inStock`) más `sortBy`. Solo productos con existencia **y** foto.
- `GET /v1/products/{uuid}/related?limit=8` ("También te puede servir"): `limit` de 1 a 24. Orden: comprados juntos (pedidos pagados de los últimos 180 días) → misma categoría o su padre → misma marca → mismos vehículos. Excluye el producto y su grupo de variantes; primero los que tienen existencia. `404` con el mismo criterio que el detalle.

Todos responden `{ "docs": [ProductBasic] }` y pueden venir vacíos: en ese caso oculta la sección.

### `GET /v1/products/brands?taxonomyUuid=...`

`{ "docs": ["Bosch", "Pretul", "Truper"] }`: marcas con al menos un producto activo, sin
duplicados por mayúsculas y en orden alfabético. `taxonomyUuid` (opcional) las acota a un nodo
del árbol, incluidos sus descendientes.

### `GET /v1/taxonomy`

```json
{ "categories": [ { "uuid": "...", "name": "FERRETERÍA", "slug": "ferreteria", "children": [ { "uuid": "...", "name": "VIDAL", "slug": "vidal", "children": [] } ] } ] }
```

Árbol de profundidad arbitraria, ordenado alfabéticamente. Usa el `uuid` de cualquier nodo como `taxonomyUuid`.

### Selector de vehículo: `GET /v1/vehicles*`

Cascada: tipo (`AUTOMOTIVE` | `MOTORCYCLE`, fijo en el frontend) → marca → **año** → modelo → motor.

| Ruta | Params | Respuesta |
|---|---|---|
| `/v1/vehicles/makes` | `vehicleType` | `{ "docs": ["Chevrolet", ...] }` |
| `/v1/vehicles/years` | `vehicleType`, `make` | `{ "docs": [2016, 2015, ...] }` (desc) |
| `/v1/vehicles/models` | + `year` | `{ "docs": ["Aveo", ...] }` |
| `/v1/vehicles/engines` | + `model` | `{ "docs": ["L4 1.6L"] }`; si viene vacío, sáltate el paso |
| `/v1/vehicles` | cualquier subconjunto + `engine` | `{ total, docs: [{ uuid, vehicleType, make, model, yearStart, yearEnd, engine, updatedAt }] }` |

Usa `uuid` como `vehicleUuid`. No existe `GET /v1/vehicles/{uuid}`: si guardas la selección ("mi garage"), guarda el objeto completo.

---

## Carrito `/v1/cart*`

Guarda solo `{uuid, quantity}`. Precio, nombre, stock e imagen se leen en vivo en cada consulta. No valida disponibilidad; eso pasa en `/v1/orders`.

**Identidad**: con `X-Client-Token` válido se usa el carrito de la cuenta; sin él, la cookie
anónima (siempre `credentials: "include"`).

Respuesta (`GET`, `PUT`, `PATCH /items`, `merge` y cupón):

```json
{
  "items": [ { "productUuid": "...", "sku": "PR2057", "name": "PORTAROLLO", "imageUrl": null, "price": 8.62, "compareAtPrice": null, "saleEndsAt": null, "stock": 2.0, "quantity": 2, "lineTotal": 17.24, "available": true } ],
  "subtotal": 17.24, "totalQuantity": 2,
  "cartToken": "5a5c479d-...", "updatedAt": "...",
  "couponCode": null, "couponValid": false, "couponInvalidReason": null,
  "discountAmount": 0.0, "total": 17.24
}
```

- `availability`, `backorderQuantity` y `backorderLeadDays` por línea: `backorderQuantity` son las piezas que exceden `stock` y van sobre pedido (`0` si alcanza o si el producto no acepta pedido). Es una estimación: el proveedor se confirma al hacer el pedido.
- Una línea `available: false` (producto eliminado o inactivo) trae `sku`, `name`, `price`, `stock` y `lineTotal` en `null`. Muéstrala como "ya no disponible". No cuenta en `subtotal`, pero sí en `totalQuantity`.
- `total = subtotal - discountAmount` (nunca negativo). Es lo que se muestra como "a pagar".
- `cartToken` solo viene en carritos anónimos. Guárdalo **en memoria** para mandarlo como `cartToken` en login, registro o merge (la cookie no viaja a `/v1/auth/*`).

| Ruta | Comportamiento |
|---|---|
| `GET /v1/cart` | Nunca crea un carrito; sin carrito responde vacío |
| `PUT /v1/cart` `{ items: [{uuid, quantity}] }` | Reemplaza todo. Crea un carrito anónimo y su cookie si no había. `422` si una línea es inválida (rechaza todo) |
| `PATCH /v1/cart/items` `{ productUuid, delta }` | Suma o resta; si la cantidad queda ≤ 0, quita la línea. Un `delta ≤ 0` sobre algo inexistente no hace nada (`200`) |
| `DELETE /v1/cart` | `204` siempre; también limpia la cookie |
| `POST /v1/cart/coupon` `{ code }` | Guarda el cupón y responde el carrito con el descuento calculado. `400` sin carrito o con carrito vacío; `404` si el código no existe o está inactivo; `409` si no aplica (motivo en `detail`) |
| `DELETE /v1/cart/coupon` | `200` con el carrito sin cupón |
| `POST /v1/cart/merge` `{ cartToken }` (**`Authorization`**) | Para un usuario ya logueado que arma un carrito anónimo nuevo. Suma las cantidades por producto. `404` si el token no corresponde a un carrito anónimo |

El cupón del carrito es solo una vista previa; el descuento real se valida al hacer el pedido (`couponCode` en `/v1/orders`).

---

## Pedidos

### `POST /v1/orders`: reservar (no cobra)

```http
POST /v1/orders
x-api-key: <api-key>
X-Client-Token: <opcional>
Idempotency-Key: <uuid generado una vez por clic>
Content-Type: application/json
```
```json
{
  "products": [ { "uuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "quantity": 1 } ],
  "deliveryInfo": {
    "contactInfo": { "name": "Juan Pérez", "phone": "3151234567", "email": "juan@example.com" },
    "deliveryType": "DELIVERYMAN",
    "addressUuid": "51cbf02f-..."
  },
  "couponCode": "WELCOME10",
  "shipping": { "quoteUuid": "9b2e...", "optionId": "2" }
}
```

- `deliveryType`: `"PICKUP"` | `"DELIVERYMAN"`.
- `DELIVERYMAN` necesita **exactamente uno** de `addressUuid` (dirección guardada; solo cuentas) o `address` (objeto escrito a mano; solo invitados). Ambos o ninguno → `422`; el que no corresponde a tu caso → `400`. `PICKUP` no lleva dirección.
- `contactInfo.phone`: se limpia (espacios, guiones, `+52`…) y debe quedar en **exactamente 10 dígitos** (`422`).
- `contactInfo.email`: opcional para cuentas, **obligatorio para invitados** (`400`). `""` cuenta como ausente.
- `couponCode` (solo cuentas): aquí es donde el cupón se valida y bloquea de verdad.
- Campos opcionales que casi nunca hacen falta: `contentId`, `branchId`, `priceListUuid`, `wholesalePrices`.
- **Sobre pedido**: una línea puede pedir más que `stock` si el producto tiene `backorderLeadDays`. El backend confirma el faltante con el proveedor antes de crear el pedido (puede tardar ~2 s más). El pedido guarda por línea `fulfillment: "BACKORDER"` y `backorderQuantity`.
- `shipping` (solo `DELIVERYMAN`; con `PICKUP` → `400`): la opción elegida en `POST /v1/shipping/quotes` (ver abajo). Su precio se suma a `total`. Es obligatorio cuando la cotización salió `QUOTED`; con `NOT_QUOTABLE` manda solo `quoteUuid`. Si algún producto no tiene peso/medidas no hace falta.

Respuesta `201`:

```json
{
  "id": "d65b89dc-9690-40b3-8dfb-aa2cdde18cc0",
  "orderUuid": "f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6",
  "status": "TO_PAY",
  "total": 299.99, "amount": 299.99, "discountAmount": 13.0, "deliveryCost": 170.0,
  "serieFolio": null, "date": null
}
```

- `id` (UUID v4) es el `{order_id}` de `/pay`, `/cancel` y `DELETE`. `orderUuid` sirve para `GET /v1/auth/me/orders/{orderUuid}`. Guarda ambos.
- `total` (alias `amount`) ya trae el descuento restado y el envío sumado: úsalo tal cual en el Brick. El cupón solo descuenta productos, nunca el envío.
- `deliveryCost`: envío cobrado; `null` si no se cobró (recoger en tienda, o envío que la tienda cotiza aparte).
- `serieFolio` y `date` siempre son `null`.
- **Idempotency-Key**: reenvía el mismo valor en los reintentos automáticos. Si el pedido ya existe, responde `200` con el mismo body; si sigue en proceso, `409` (reintenta en unos segundos).

Errores:
- `401`: `X-Client-Token` inválido.
- `400`: carrito vacío; dirección guardada incompleta; invitado sin email, con `addressUuid` o con `couponCode`.
- `404`: `addressUuid` que no existe o es de otra cuenta; cupón inexistente.
- `503`: no se pudo confirmar con el proveedor una línea sobre pedido. Pide reintentar en unos minutos o bajar la cantidad a `stock`.
- `409`: un producto sin disponibilidad (incluye una línea sobre pedido que el proveedor ya no puede surtir); cupón que ya no aplica; falta `shipping` en un carrito cotizable, o la cotización expiró / el carrito o la dirección cambiaron / el `optionId` no existe (el `detail` lo dice: vuelve a cotizar).

### Envío a domicilio: `POST /v1/shipping/quotes`

Cotiza con envia.com el carrito a la dirección de entrega. Llámalo cuando el cliente elige `DELIVERYMAN` y tiene dirección, y otra vez si cambia el carrito o la dirección. Tarda unos segundos (consulta ~20 paqueterías): muestra un indicador de carga. Límite: 20 por minuto.

```http
POST /v1/shipping/quotes
x-api-key: <api-key>
X-Client-Token: <opcional>
```
```json
{ "products": [ { "uuid": "3Cny4...", "quantity": 2 } ], "addressUuid": "51cbf02f-..." }
```

Mismo carrito que mandarás a `POST /v1/orders`. Cuenta → `addressUuid`; invitado → `address` (mismo objeto que en `deliveryInfo.address`). Respuesta `201`:

```json
{
  "quoteUuid": "9b2e...", "status": "QUOTED", "reason": null,
  "expiresAt": "2026-11-10T21:30:00Z",
  "options": [
    { "optionId": "1", "carrier": "dhl", "serviceDescription": "DHL Ground", "deliveryEstimate": "2-3 días", "price": 115.0, "currency": "MXN" },
    { "optionId": "2", "carrier": "fedex", "serviceDescription": "FedEx Express", "deliveryEstimate": "Día siguiente", "price": 170.0, "currency": "MXN" }
  ],
  "missingDimensions": []
}
```

- `status: "QUOTED"`: muestra las opciones (máx. 5, la más barata de cada paquetería, ya con IVA) y manda la elegida en `POST /v1/orders` como `shipping: { quoteUuid, optionId }`.
- `status: "NOT_QUOTABLE"`: el envío **no se cobra en línea**; la tienda lo cotiza y te contacta. Muestra algo como "Envío por confirmar" y manda `shipping: { quoteUuid }` sin `optionId`. `reason`: `MISSING_DIMENSIONS` (algún producto sin peso/medidas, ver `missingDimensions`), `NO_RATES` (ninguna paquetería cubre ese envío, p. ej. artículos muy pesados) o `UPSTREAM_ERROR` (envia.com no respondió).
- La cotización vence en 30 min (`expiresAt`). Cotizar dos veces el mismo carrito y dirección en 10 min devuelve la misma cotización.
- Errores: `400`/`404`/`422` de dirección (mismas reglas que `deliveryInfo`), `409` si un producto ya no está disponible.

### Checkout de invitado

Omite `X-Client-Token` en `/v1/orders`, `/pay`, `/cancel` y `DELETE`. Email obligatorio, sin cupones, y para `DELIVERYMAN` la dirección va inline:

```json
"address": {
  "street": "Av Siempre Viva", "extNumber": "123", "intNumber": null,
  "neighborhood": "Centro", "city": "Guadalajara", "county": "Guadalajara",
  "state": "Jalisco", "zipCode": "44100",
  "references": null, "latitude": null, "longitude": null
}
```

(`street`, `extNumber`, `neighborhood`, `city`, `county`, `state` y `zipCode` son obligatorios → `422`.)

- La pertenencia se prueba con el `id` u `orderUuid` del pedido (cualquiera de los dos). **Persístelos** en el navegador (localStorage o la URL de confirmación): sin ellos el pedido es irrecuperable.
- `GET /v1/orders/guest/{order_id}` devuelve el mismo `OrderPublic` que el historial de cuenta.
- Si el invitado crea una cuenta con ese correo **y lo verifica**, el pedido se liga a la cuenta: aparece en `GET /v1/auth/me/orders` y la ruta de invitado empieza a dar `404`. Registrarse sin verificar no liga nada.

### Historial: `GET /v1/auth/me/orders` y `GET /v1/auth/me/orders/{orderUuid}` (`Authorization`)

Lista paginada (`limit`/`offset` en la query, más recientes primero): `{ total, docs: [OrderPublic] }`. El detalle da `404` si el pedido no es de la cuenta.

`OrderPublic`:

```json
{
  "uuid": "f1a2b3c4-...", "id": "d65b89dc-...", "sicarOrderId": "d65b89dc-...",
  "serieFolio": null,
  "status": "PAID",
  "dispatchStatus": "PENDING_ACCEPTANCE", "dispatchHistory": null,
  "total": 129.99, "subtotal": 142.99, "discountAmount": 13.0, "couponCode": "WELCOME10",
  "totalQuantity": 3,
  "deliveryInfo": { "contactInfo": { "name": "Juan Pérez", "phone": "3151234567", "email": null }, "deliveryType": "PICKUP" },
  "items": [ {
    "uuid": "3Cny...", "productUuid": "3Cny...", "sku": "PR2057", "description": "PORTAROLLO",
    "quantity": "1", "unit": "PZA", "imageUrl": "https://...",
    "unitPrice": 129.99, "lineTotal": 129.99
  } ],
  "createdAt": "2026-07-10T18:32:05Z",
  "cancellationReason": null,
  "shippingLabel": null
}
```

- `status` (pago): `TO_PAY` | `PAID` | `CANCELLED`. `dispatchStatus` (cumplimiento, lo controla el admin): `PENDING_ACCEPTANCE` → `PENDING` → `PREPARING` → `COMPLETE` → `DISPATCHED`. Son dos cosas distintas.
- `id` y `sicarOrderId` son el mismo valor; usa `id`.
- `couponCode`, `subtotal` y `discountAmount` son `null` si no hubo cupón. Si hubo, `total = subtotal - discountAmount + deliveryCost`.
- `deliveryCost`: envío cobrado (`null` si no se cobró). `shippingQuote` (solo `DELIVERYMAN`, pedidos nuevos): `{ status: "QUOTED" | "MANUAL", reason, carrier, serviceDescription, deliveryEstimate, price, currency }`; `MANUAL` = envío por confirmar con la tienda. También vienen en los webhooks de pedido.
- `hasBackorder`/`backorderLeadDays`: si alguna línea va sobre pedido y los días hábiles estimados (el mayor). Por línea: `fulfillment` (`STOCK` | `BACKORDER`; los pedidos viejos traen `STOCK`) y `backorderQuantity` (número, piezas sobre pedido de esa línea). Muéstralo en la confirmación y en el correo de `order-confirmed`, que también trae estos campos.
- `items[].quantity` es string. `unitPrice` y `lineTotal` llevan IVA y son **antes del cupón**; para GA4, el descuento va a nivel evento (`coupon`/`discount`). `imageUrl` puede faltar en pedidos viejos.
- `cancellationReason`: texto solo si canceló un admin.
- `shippingLabel` (solo `DELIVERYMAN` con guía real de envia.com): `{ carrier, service, shipmentId, serviceDescription, trackingNumber, trackUrl, labelUrl, totalPrice, currency, weight, length, width, height, generatedAt }`. Usa `trackUrl` como enlace de "sigue tu paquete". No hay rastreo en vivo.

### `POST /v1/orders/{order_id}/pay`

Cuenta: `X-Client-Token`. Invitado: el `order_id` (`id` u `orderUuid`) basta.

```json
{
  "token": "ff8080814c11e237014c1ff593b57b4d",
  "paymentMethodId": "visa",
  "paymentTypeId": "credit_card",
  "issuerId": "310",
  "installments": 1,
  "payer": { "email": "juan@example.com", "identification": { "type": "RFC", "number": "XAXX010101000" } }
}
```

- Manda el `formData` del `onSubmit` del Brick tal cual, más `paymentTypeId` = `selectedPaymentMethod` (`credit_card`, `debit_card`, `prepaid_card`, `ticket`, …). `token` no viene en OXXO. **Nunca mandes un monto.**
- Configura el Payment Brick solo con tarjeta de crédito, débito y OXXO (sin Wallet). La clave pública de Mercado Pago vive en el env del frontend.

Respuesta `200`:

```json
{ "orderUuid": "f1a2...", "status": "PAID", "mpPaymentId": "ORD01JS2V6CM8KJ0EC4H502TGK1WP", "mpStatus": "approved", "mpStatusDetail": "accredited", "ticketUrl": null }
```

- `status`:
  - `PAID`: aprobado.
  - `TO_PAY`: pendiente (tarjeta en revisión u OXXO por pagar). Muestra `ticketUrl` para OXXO; la confirmación llega después por webhook.
  - `CANCELLED`: rechazado; el stock ya se liberó.
- `mpPaymentId` es un id de order de Mercado Pago (`ORD…`). **No uses el Status Screen Brick** (solo acepta ids numéricos); muestra tu propio resultado.
- `mpStatus`: `approved` / `pending` / `rejected` / …. `mpStatusDetail` en rechazos: `rejected_by_issuer`, `bad_filled_card_data`, `insufficient_amount`, `card_insufficient_amount`, `invalid_installments`, `required_call_for_authorize`, `card_disabled`, `max_attempts_exceeded`, `high_risk`. Un OXXO pendiente llega como `waiting_payment`.
- Reintentar un pedido ya `PAID` devuelve el mismo `200`, sin cobrar dos veces.
- Errores: `401` (token inválido), `404` (no existe, no es tuyo o el invitado ya se ligó a una cuenta), `409` (pedido cancelado), `502` (Mercado Pago rechazó la solicitud; reintenta).

### `POST /v1/orders/{order_id}/cancel`

Misma identidad que `/pay`. El body es opcional (`products` y `cashRegisterUuid` se aceptan, pero se ignoran). Si el pago estaba aprobado lo reembolsa; si estaba pendiente lo cancela.

```json
{ "orderId": "d65b89dc-...", "documentUuid": "d65b89dc-...", "sicarTimestamp": 1783961225017.0, "message": "Pedido cancelado exitosamente.", "status": "CANCELLED" }
```

- La cancelación es definitiva en cuanto responde. `orderId` es igual a `documentUuid`. `sicarTimestamp` es la hora de la cancelación local, en ms.
- Si un admin ya había aceptado el pedido, el stock tarda un poco en volver al catálogo (normalmente menos de 1 min).
- `409` si ya estaba cancelado, o si `dispatchStatus` es `DISPATCHED` (un `COMPLETE` de `PICKUP` sí se puede cancelar). También `401` y `404`, igual que en `/pay`.

### `DELETE /v1/orders/{order_id}`

Solo para pedidos `TO_PAY` (`409` si no). Cancela cualquier pago pendiente, libera el stock y oculta el pedido del historial. `204`. Misma identidad que `/pay`.

---

## Reseñas

Una por cliente y producto (para cambiarla, edítala). No exige compra; `isVerifiedPurchase` es solo un badge.

- `GET /v1/products/{productUuid}/reviews?limit=&offset=&sortBy=newest|highest_rating|lowest_rating|most_helpful`: solo `x-api-key`.

```json
{
  "total": 2,
  "docs": [ { "uuid": "...", "productUuid": "...", "clientName": "Juan Pérez", "rating": 4, "comment": "...", "isVerifiedPurchase": true, "isHidden": false, "helpfulCount": 3, "adminReply": null, "adminReplyAt": null, "createdAt": "...", "updatedAt": null } ],
  "averageRating": 3.5, "reviewsCount": 2,
  "ratingBreakdown": { "1": 0, "2": 0, "3": 0, "4": 1, "5": 1 }
}
```

- `POST /v1/products/{productUuid}/reviews` (`Authorization`) `{ rating: 1-5, comment? }` → `201`. `404` si el producto no existe; `409` si ya lo reseñaste.
- `PATCH` / `DELETE /v1/reviews/{uuid}` (`Authorization`, solo el autor): `404` si no es tuya; `DELETE` responde `204`.
- `PUT` / `DELETE /v1/reviews/{uuid}/helpful` (`Authorization`): idempotentes. Responden `{ reviewUuid, helpfulCount, markedByMe }`; `404` si la reseña no existe o está oculta.
- `GET /v1/auth/me/reviews` (`Authorization`): historial propio, incluidas las ocultas por un admin.

## Wishlist `/v1/wishlist/*` (solo cuentas, `Authorization`)

La lista "Favoritos" (`isDefault: true`) se crea sola en el primer guardado y no se puede borrar. El mismo producto puede estar en varias listas; agregar y quitar son idempotentes.

| Ruta | Respuesta |
|---|---|
| `GET /favorites?limit=&offset=` | `{ total, docs: [{ productUuid, addedAt, available, product: ProductBasic \| null }] }`; vacío si la lista aún no existe |
| `PUT /favorites/{productUuid}` | `200` vacío; `404` si el producto no existe o no está disponible |
| `DELETE /favorites/{productUuid}` | `204` |
| `GET /collections` | `[{ uuid, name, isDefault, itemCount, createdAt }]`, sin paginar |
| `POST /collections` `{ name }` | `201` |
| `PATCH /collections/{uuid}` `{ name }` | `404` si no es tuya |
| `DELETE /collections/{uuid}` | `204`; `409` si es Favoritos |
| `GET /collections/{uuid}/items` | Igual que `GET /favorites` |
| `POST /collections/{uuid}/items` `{ productUuid }` | `201` |
| `DELETE /collections/{uuid}/items/{productUuid}` | `204` |

`available: false` significa que el producto se eliminó o desactivó (en ese caso `product` es `null`). Muéstralo como "ya no disponible".

## Avisos de reabastecimiento

- `POST /v1/products/{uuid}/stock-alerts` (`X-Client-Token` opcional) `{ email }`: con token se usa el correo de la cuenta; sin token, `email` es obligatorio (`400`).
  - `201` si es nueva, `200` si ya existía (misma suscripción).
  - `409` si el producto **sí** tiene existencia; `404`; `422` (correo inválido); `429` (10 por minuto por IP, 20 por día por correo).
  - Responde `{ uuid, productUuid, email, createdAt, expiresAt }`; caduca a los 90 días.
- `GET /v1/auth/me/stock-alerts` (`Authorization`): suscripciones activas, cada una con `product` (o `null`).
- `DELETE /v1/auth/me/stock-alerts/{productUuid}`: `204`, idempotente.
- La existencia se revisa cada 5 min y se avisa **una sola vez** con el webhook `product-back-in-stock`.

## Correos de marketing y consentimiento

- `marketingEmails` es `false` por defecto para todos. Se cambia en register, google (solo al crear la cuenta) o `PATCH /v1/auth/me`.
- Cada cambio queda en un historial que no se puede modificar. Fuentes por endpoint (cada uno acepta solo la suya; otro valor → `422`):
  - register: `register_checkbox`, se registra siempre;
  - google: `google_signup`, solo si crea la cuenta;
  - `PATCH /v1/auth/me`: `account_toggle`, solo si el valor cambia;
  - unsubscribe: `email_unsubscribe` | `one_click_unsubscribe`, solo con scope `marketing` y si estaba en `true`.
- `privacyNoticeVersion`: texto de hasta 64 caracteres. Manda tu constante.
- `client.marketingConsentAt` es la fecha del último cambio.

### `POST /v1/notifications/unsubscribe`

`{ "token": "<unsubscribeToken>", "source": "email_unsubscribe" }`, sin sesión. Responde
`{ "scope": "marketing" | "stock-alert", "email": "c***@example.com" }`. `marketing` apaga
`marketingEmails`; `stock-alert` cancela esa suscripción. Es idempotente; `400` si el token es
inválido. El token no caduca y no sirve como sesión.

---

## Webhooks salientes (hacia `{FRONTEND_BASE_URL}/api/webhooks/...`)

Los llama este backend (servidor a servidor), nunca el navegador. Cada uno dispara un correo
que envía el frontend con su propio template y su cuenta de Resend.

**Verificación (obligatoria en todos):**

```
X-Webhook-Timestamp: 1783961178
X-Webhook-Signature: <hex>
signature = hex(HMAC_SHA256(FRONTEND_WEBHOOK_SECRET, "{timestamp}." + rawBody))
```

Usa el **body crudo** (no re-serializado) y compara en tiempo constante (`crypto.timingSafeEqual`).
Rechaza timestamps de más de ~5 min. El secreto se entrega por fuera de este repo.

**Sin reintentos**: `order-confirmed`, `order-cancelled`, `order-status-changed`,
`verification-requested` y `password-reset-requested`. Responde `200` rápido.
**Con reintentos** (1 min, 10 min, 1 h): `product-back-in-stock`, `wishlist-price-drop` y
`cart-abandoned`. Traen además `X-Webhook-Id` (el mismo en cada reintento: úsalo para
deduplicar) y `X-Webhook-Event`. Responde `2xx` en cuanto valides la firma.

| Ruta | Cuándo | Body |
|---|---|---|
| `order-confirmed` | El pedido pasa a `PAID` (síncrono o por webhook de Mercado Pago) | `OrderPublic` + `clientEmail` + `clientName` |
| `order-cancelled` | Pasa a `CANCELLED` (cliente, `DELETE`, pago rechazado, admin) | Igual; `cancellationReason` trae texto solo si canceló un admin |
| `order-status-changed` | Admin: aceptado; `COMPLETE` de un `PICKUP`; `DISPATCHED` | Igual + `event` (`order-accepted` \| `order-ready-pickup` \| `order-dispatched`) + `statusMessage` (texto en español). En `order-dispatched`, `shippingLabel` viene si hay guía real |
| `verification-requested` | Registro local o resend | `{ clientUuid, clientName, clientEmail, token }`. Enlace → `POST /v1/auth/verify-email` (24 h) |
| `password-reset-requested` | forgot-password con cuenta existente | `{ clientUuid, clientName, clientEmail, hasPassword, resetToken, expiresInMinutes }`. Con `hasPassword: false` (cuenta Google) el token viene `null`: manda un correo que explique que la cuenta usa Google |
| `product-back-in-stock` | Vuelve la existencia | `{ email, clientName \| null, product: { sicarUuid, sku, name, imageUrl, price, stock }, unsubscribeToken }` (scope `stock-alert`) |
| `wishlist-price-drop` | Baja de 5 % o más en un favorito; revisión horaria, máx. 1 por cliente al día, solo con `marketingEmails` | `{ clientUuid, clientEmail, clientName, items: [{ product: { sicarUuid, sku, name, imageUrl }, oldPrice, newPrice }], unsubscribeToken }` |
| `cart-abandoned` | Carrito de cuenta con productos, 2 h sin tocar, sin pedido desde entonces, con `marketingEmails`; máx. 1 cada 7 días y solo si cambió | `{ clientUuid, clientEmail, clientName, items: [{ sicarUuid, name, imageUrl, price, quantity, available }], subtotal, unsubscribeToken }` |

En `order-*`, `clientEmail` viene de `deliveryInfo.contactInfo.email` o, si falta, del correo de
la cuenta; `clientName` usa el nombre del contacto en pedidos de invitado. No existe un webhook
de "pago pendiente" (la reserva sin pago dura 30 min).

---

## Ejemplo mínimo (fetch)

```ts
const API_URL = process.env.NEXT_PUBLIC_API_URL!;
const API_KEY = process.env.NEXT_PUBLIC_API_KEY!;

const json = { "x-api-key": API_KEY, "Content-Type": "application/json" };
const withClient = (t?: string) => (t ? { "X-Client-Token": t } : {});

export async function getCatalog(filters: Record<string, unknown>) {
  const res = await fetch(`${API_URL}/v1/products`, {
    method: "POST", headers: json, body: JSON.stringify({ limit: 60, offset: 0, ...filters }),
  });
  return res.json(); // { total, docs, priceRange }
}

// credentials: "include" es obligatorio en TODA llamada a /v1/cart*.
export async function adjustCartItem(productUuid: string, delta: number, clientToken?: string) {
  const res = await fetch(`${API_URL}/v1/cart/items`, {
    method: "PATCH", credentials: "include",
    headers: { ...json, ...withClient(clientToken) },
    body: JSON.stringify({ productUuid, delta }),
  });
  if (!res.ok) throw new Error((await res.json()).detail);
  return res.json();
}

export async function login(email: string, password: string, cartToken?: string) {
  const res = await fetch(`${API_URL}/v1/auth/login`, {
    method: "POST", headers: json,
    body: JSON.stringify({ email, password, ...(cartToken ? { cartToken } : {}) }),
  });
  if (!res.ok) throw new Error((await res.json()).detail);
  return res.json(); // { token, client, cart }
}

// clientToken undefined = invitado (email obligatorio, sin couponCode).
export async function createOrder(clientToken: string | undefined, body: unknown, idempotencyKey: string) {
  const res = await fetch(`${API_URL}/v1/orders`, {
    method: "POST",
    headers: { ...json, ...withClient(clientToken), "Idempotency-Key": idempotencyKey },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error((await res.json()).detail);
  return res.json(); // { id, orderUuid, status, total, discountAmount, ... }
}

// Desde el onSubmit del Payment Brick.
export async function payOrder(orderId: string, clientToken: string | undefined, formData: object, paymentTypeId: string) {
  const res = await fetch(`${API_URL}/v1/orders/${orderId}/pay`, {
    method: "POST", headers: { ...json, ...withClient(clientToken) },
    body: JSON.stringify({ ...formData, paymentTypeId }),
  });
  if (!res.ok) throw new Error((await res.json()).detail);
  return res.json(); // { orderUuid, status, mpPaymentId, mpStatus, mpStatusDetail, ticketUrl }
}
```
