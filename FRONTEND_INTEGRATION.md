# Conectando el frontend a API SICARX

Guía práctica para el frontend (Next.js) de Ferretería Charly: cómo autenticarse, consultar el
catálogo, y crear/cancelar pedidos contra esta API.

## URLs base

| Entorno | URL |
|---|---|
| Producción (Railway) | `https://api-production-cf7a.up.railway.app` |
| Local (dev) | `http://127.0.0.1:8000` (o el puerto que uses con `uvicorn --reload`) |

## CORS

El origen del frontend debe estar en `origins` (`app/main.py`). Actualmente permitidos:
`http://localhost`, `http://localhost:8000`, `https://ferreteriacharly.com`,
`https://api-production-cf7a.up.railway.app`.

Si el frontend corre en otro origen (p. ej. `http://localhost:3000` en dev, o un dominio de
Vercel/preview), pide que se agregue a esa lista — si no, el navegador bloqueará las respuestas
con un error de CORS aunque la petición en sí llegue bien al backend.

## Dos capas de autenticación — no confundirlas

### 1. `x-api-key` — obligatorio en **todas** las rutas

Header estático que autentica al frontend contra esta API (no contra Sicar X). Un solo valor,
provisto por el equipo backend, se manda igual en cada request:

```
x-api-key: <valor provisto por backend>
```

Sin este header, cualquier ruta responde `403`.

### 2. Token de sesión de Sicar X — solo para `POST /v1/orders`

Un JWT por comprador, obtenido de `POST /v1/session/init` y reenviado como `Authorization` al
crear un pedido. **Nunca** se usa en ninguna otra ruta — `/v1/products`, `/v1/products/{uuid}`,
`/v1/taxonomy` no lo necesitan, solo el `x-api-key`.

### 3. Token de cuenta de cliente (`X-Client-Token`) — obligatorio para `POST /v1/orders` y `POST /v1/orders/{order_id}/cancel`

**Login ahora es obligatorio para comprar — ya no existe checkout anónimo.** Un tercer JWT, distinto
de los dos anteriores, obtenido de `POST /v1/auth/register` o `POST /v1/auth/login` y reenviado en
una cabecera aparte, `X-Client-Token` (NO en `Authorization`, que en estas dos rutas ya está ocupada
por el token de sesión de Sicar X del punto 2). Identifica qué cuenta queda dueña del pedido, para
que después pueda verlo en `GET /v1/auth/me/orders`. Sin este header, `POST /v1/orders` y
`POST /v1/orders/{order_id}/cancel` responden `401` antes de siquiera intentar hablar con Sicar X.

### 4. Token de carrito anónimo (`X-Cart-Token`) — solo para `/v1/cart` sin login

Un identificador opaco (no un JWT, no hay que decodificarlo) que este API genera y devuelve la
primera vez que se guarda un carrito sin que el visitante haya iniciado sesión. Ver la sección
`/v1/cart` más abajo — **importante:** para el carrito, el mismo token de cuenta de arriba (punto 3)
se manda de forma distinta según la ruta: `X-Client-Token` en `GET`/`PUT`/`DELETE /v1/cart`, pero
`Authorization` en `POST /v1/cart/merge` (igual que `/v1/auth/me/addresses` y
`/v1/auth/me/orders`, que también usan `Authorization`). No es un error tipográfico — revisa la
cabecera exacta de cada ejemplo con cuidado.

## Flujo típico de una compra

```
1. POST /v1/auth/register o /v1/auth/login       → obtener token de cuenta (X-Client-Token), una vez
2. POST /v1/session/init                          → obtener token de sesión de Sicar X (una vez)
3. POST /v1/products                              → mostrar catálogo / resultados de filtro
4. GET  /v1/products/{uuid}                       → detalle al abrir una ficha de producto
5. POST /v1/orders                                → crear el pedido (usa los tokens de los pasos 1 y 2)
6. (si aplica) POST /v1/orders/{order_id}/cancel  → cancelar el pedido creado en el paso 5 (usa el token del paso 1)
```

`/v1/session/init` normalmente se llama **una sola vez** al iniciar la sesión de compra (p. ej. al
cargar el carrito o al primer intento de checkout), no en cada request. Guarda el `token`
devuelto (en memoria, cookie, o storage del lado cliente) y reenvíalo tal cual en `Authorization`
al llamar a `/v1/orders`. Guarda también el token de `/v1/auth/login`/`/v1/auth/register` (distinto)
para reenviarlo en `X-Client-Token`.

`/v1/cart` es independiente de este flujo — es persistencia opcional del carrito (ver referencia
abajo), no un paso obligatorio antes de `/v1/orders`. `POST /v1/orders` sigue recibiendo el
carrito directo en el body, no lo lee de `/v1/cart`.

---

## Referencia de endpoints

> **Nota sobre nombres de campo:** las respuestas siempre usan camelCase (todos los ejemplos de
> abajo). Los *bodies* de request, por ahora, todavía aceptan también los nombres antiguos en
> snake_case (p. ej. mandar `department_uuid` en vez de `departmentUuid` sigue funcionando) —
> no es una migración forzada de entrada, solo de salida. No construyas código nuevo dependiendo
> de esto: no está garantizado que el soporte a snake_case se mantenga indefinidamente.

### `POST /v1/session/init` — iniciar o refrescar sesión

Sin `Authorization`: crea una sesión anónima nueva. Con `Authorization` (token previo): lo valida
y refresca si expiró.

```http
POST /v1/session/init
x-api-key: <api-key>
Authorization: <token-anterior>     # opcional
```

Respuesta `200`:
```json
{
  "token": "eyJhbGciOiJIUzI1NiIs...",
  "priceListUuid": "0b8b0848-3880-4085-b213-3b3d30c79429",
  "branchId": 151456,
  "deliveryCost": 50,
  "contentId": "145952"
}
```

Guarda `token` — es lo que se reenvía como `Authorization` en `/v1/orders`.

### `POST /v1/auth/register` / `POST /v1/auth/login` — cuentas de cliente (login propio, separado de Sicar X)

Esto es un tercer tipo de token, **distinto** del token de `/v1/session/init` de arriba.
`/v1/session/init` gestiona la sesión anónima de compra contra Sicar X (necesaria para
`/v1/orders`); `/v1/auth/register` y `/v1/auth/login` son cuentas de cliente propias de esta API —
para que un usuario tenga un login persistente en el sitio (guardar direcciones, ver histórico,
etc. — todavía no implementado, solo existe el login por ahora). Ambos requieren `x-api-key` igual
que cualquier otra ruta.

```http
POST /v1/auth/register
x-api-key: <api-key>
Content-Type: application/json

{
  "name": "Juan Pérez",
  "email": "juan@example.com",
  "phone": "3151234567",
  "password": "unaContraseñaSegura"
}
```

`password` requiere mínimo 8 caracteres (`422` si es más corta). Responde `200` con el mismo shape
que `/v1/auth/login` — el registro inicia sesión automáticamente, no hace falta llamar a
`/v1/auth/login` después:

```json
{
  "token": "eyJhbGciOiJIUzI1NiIs...",
  "client": {
    "uuid": "f6bacfb9-cb38-4f96-adab-2593a14345bc",
    "name": "Juan Pérez",
    "email": "juan@example.com",
    "phone": "3151234567"
  }
}
```

`409` si el correo ya está registrado.

```http
POST /v1/auth/login
x-api-key: <api-key>
Content-Type: application/json

{
  "email": "juan@example.com",
  "password": "unaContraseñaSegura"
}
```

Misma respuesta `200` que arriba. `401` si el correo o la contraseña son incorrectos. El correo
no distingue mayúsculas/minúsculas (`Juan@x.com` y `juan@x.com` son la misma cuenta), así que no
hace falta normalizar nada del lado del frontend. `/v1/auth/login` está limitado a 5 intentos por
minuto por IP — pasado ese límite responde `429` con `{"error": "Rate limit exceeded: ..."}`.

Guarda este `token` — se reenvía en dos lugares distintos: como `Authorization` en
`GET`/`PATCH /v1/auth/me` (y las rutas de direcciones/historial de pedidos abajo), y como
`X-Client-Token` en `POST /v1/orders`/`POST /v1/orders/{order_id}/cancel` (ver "Dos capas de
autenticación" arriba — ahí sí importa la cabecera exacta, `Authorization` está ocupada por el
token de sesión de Sicar X en esas dos rutas). Login es obligatorio para comprar — ya no existe
checkout anónimo.

### `GET /v1/auth/me` — datos de la cuenta (para "Mi cuenta")

```http
GET /v1/auth/me
x-api-key: <api-key>
Authorization: <token de /v1/auth/register o /v1/auth/login>
```

Respuesta `200`:
```json
{
  "uuid": "f6bacfb9-cb38-4f96-adab-2593a14345bc",
  "name": "Juan Pérez",
  "email": "juan@example.com",
  "phone": "3151234567",
  "addresses": [
    {
      "uuid": "51cbf02f-cf83-470e-9313-c586d816c9c0",
      "label": "Casa",
      "street": "Av. Siempre Viva",
      "extNumber": "123",
      "intNumber": null,
      "neighborhood": null,
      "city": "Culiacán",
      "state": "Sinaloa",
      "country": "México",
      "zipCode": "80000",
      "references": null,
      "isDefault": true
    }
  ]
}
```

`addresses` viene incluido de una vez (no hace falta llamar a `GET /v1/auth/me/addresses` aparte
solo para pintar "Mi cuenta"), pero para agregar/editar/eliminar una dirección sí se usan las
rutas de abajo. `401` si falta el `Authorization`, el token es inválido/expiró, o la cuenta ya no
existe/está desactivada — en cualquiera de esos casos, manda al usuario de vuelta a login.

### `PATCH /v1/auth/me` — editar nombre, teléfono o contraseña

Todos los campos son opcionales — solo se cambia lo que se envíe.

```http
PATCH /v1/auth/me
x-api-key: <api-key>
Authorization: <token de /v1/auth/register o /v1/auth/login>
Content-Type: application/json

{
  "name": "Juan Pérez García",
  "phone": "3159999999"
}
```

Para cambiar la contraseña, hay que enviar **ambas**: la actual y la nueva, en la misma llamada
(no se puede cambiar la contraseña solo con el token — protege contra un token robado/viejo):

```json
{
  "currentPassword": "unaContraseñaSegura",
  "newPassword": "unaContraseñaNuevaSegura"
}
```

`newPassword` requiere mínimo 8 caracteres (`422` si es más corta). `401` si `currentPassword`
no coincide con la actual. Responde `200` con el mismo shape que `GET /v1/auth/me`, ya actualizado
(este endpoint no toca `email` ni `addresses` — usa las rutas de abajo para direcciones). Limitado
a 10 llamadas por minuto por IP (`429` si se excede).

### `GET/POST/PATCH/DELETE /v1/auth/me/addresses` — libro de direcciones

Direcciones guardadas de la cuenta, como recurso aparte (no se editan desde `PATCH /v1/auth/me`).
Todas requieren `x-api-key` + el mismo `Authorization` que `/v1/auth/me`.

```http
GET /v1/auth/me/addresses
x-api-key: <api-key>
Authorization: <token>
```

Responde `200` con un arreglo (mismo shape que `addresses` dentro de `GET /v1/auth/me`).

```http
POST /v1/auth/me/addresses
x-api-key: <api-key>
Authorization: <token>
Content-Type: application/json

{
  "label": "Casa",
  "street": "Av. Siempre Viva",
  "extNumber": "123",
  "city": "Culiacán",
  "state": "Sinaloa",
  "country": "México",
  "zipCode": "80000",
  "isDefault": true
}
```

Solo `street` es obligatorio (`422` si falta). `isDefault: true` desmarca automáticamente
cualquier otra dirección default que el cliente ya tuviera — solo puede haber una a la vez.
Responde `201` con la dirección creada (incluye su `uuid`, que es lo que identifica la dirección
en `PATCH`/`DELETE` de abajo — nunca un índice de arreglo).

```http
PATCH /v1/auth/me/addresses/{uuid}
x-api-key: <api-key>
Authorization: <token>
Content-Type: application/json

{
  "label": "Casa (nueva referencia)",
  "isDefault": true
}
```

Todos los campos son opcionales — solo se cambia lo que se envíe. Responde `200` con la dirección
actualizada. `404` si el `uuid` no existe o no pertenece al cliente autenticado.

```http
DELETE /v1/auth/me/addresses/{uuid}
x-api-key: <api-key>
Authorization: <token>
```

`204` sin contenido si se elimina correctamente. `404` si el `uuid` no existe o no pertenece al
cliente autenticado — igual que `PATCH`, nunca revela si la dirección de otro cliente existe.

### `GET /v1/auth/me/orders` — historial de pedidos del cliente

```http
GET /v1/auth/me/orders?limit=20&offset=0
x-api-key: <api-key>
Authorization: <token de /v1/auth/login o /v1/auth/register>
```

Lista paginada (`limit`/`offset` como query params, no body — mismos límites que `/v1/products`:
`limit` 1-200, default 60; `offset` ≥ 0), más recientes primero. Solo Postgres local, sin llamadas
a Sicar X.

Respuesta `200`:
```json
{
  "total": 3,
  "docs": [
    {
      "uuid": "f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6",
      "sicarOrderId": "6a55165ada77fe7cd25d39e3",
      "serieFolio": "TL518",
      "status": "PAID",
      "dispatchStatus": "PENDING_ACCEPTANCE",
      "dispatchHistory": null,
      "total": 129.99,
      "totalQuantity": 3,
      "deliveryInfo": { "contactInfo": { "name": "Juan Pérez", "phone": "3151234567", "email": null }, "deliveryType": "PICKUP" },
      "items": [ { "uuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "sku": "PR2057", "description": "PORTAROLLO", "quantity": "1", "unit": "PZA" } ],
      "createdAt": "2026-07-10T18:32:05Z"
    }
  ]
}
```

### `GET /v1/auth/me/orders/{orderUuid}` — detalle de un pedido

```http
GET /v1/auth/me/orders/f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6
x-api-key: <api-key>
Authorization: <token de /v1/auth/login o /v1/auth/register>
```

Mismo shape que un elemento de `docs` arriba. `404` si el pedido no existe o no pertenece a la
cuenta autenticada. Si `dispatchStatus` no está en un estado definitivo (`COMPLETE`/`DISPATCHED`),
esta llamada intenta refrescarlo contra Sicar X antes de responder — puede tardar un poco más que
la lista. `status` (`PAID`/`CANCELLED`) es el estado de pago/cancelación propio de esta API;
`dispatchStatus` (`PENDING_ACCEPTANCE`/`PENDING`/`PREPARING`/`COMPLETE`/`DISPATCHED`) es el estado
de cumplimiento/entrega real de Sicar X — son dos cosas distintas, no las confundas al mostrar el
seguimiento del pedido.

### `POST /v1/products` — catálogo local (paginado, sin llamadas a Sicar X)

```http
POST /v1/products
x-api-key: <api-key>
Content-Type: application/json

{
  "limit": 60,
  "offset": 0,
  "departmentUuid": null,
  "categoryUuid": null,
  "tag": null,
  "inStock": false,
  "sortBy": null
}
```

Respuesta `200`:
```json
{
  "total": 124149,
  "docs": [
    {
      "sicarUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un",
      "sku": "PR2057",
      "name": "PORTAROLLO",
      "descriptionDetails": null,
      "imageUrl": null,
      "price": 8.62069,
      "stock": 2.0
    }
  ]
}
```

Usa `departmentUuid`/`categoryUuid` (de `GET /v1/taxonomy`) para filtrar, y `tag` para ofertas u
otras etiquetas (coincidencia exacta contra los valores en `Product.tags`, p. ej. `"oferta"` o
`"pretul"` — no es substring). `inStock: true` restringe a productos con `stock > 0` (por
defecto `false`). Pagina con `limit`/`offset`.

`price` siempre viene con 2 decimales exactos (es un `Numeric` en la base de datos, no un
`float`) — no asumas más precisión que esa al mostrarlo o redondearlo del lado del frontend.

`sortBy` ordena los resultados — valores válidos: `"price_asc"`, `"price_desc"`, `"name_asc"`.
Cualquier otro valor responde `422`. Si se omite (`null`), no hay orden garantizado entre
llamadas — usa `sortBy` siempre que el orden le importe a la UI (p. ej. un selector de
"Ordenar por: Precio menor a mayor / mayor a menor / Nombre A-Z").

`limit` debe estar entre 1 y 200 (por defecto 60 si se omite) y `offset` debe ser ≥ 0 — valores
fuera de esos rangos responden `422` en vez de aceptarse silenciosamente.

### `POST /v1/search` — buscar por sku o nombre

```http
POST /v1/search
x-api-key: <api-key>
Content-Type: application/json

{
  "q": "portarollo",
  "limit": 60,
  "offset": 0,
  "departmentUuid": null,
  "categoryUuid": null,
  "inStock": false
}
```

Coincidencia por substring (contiene), sin distinguir mayúsculas/minúsculas, contra `sku` **o**
`name` en un solo campo de búsqueda. `departmentUuid`/`categoryUuid` son opcionales y funcionan
igual que en `/v1/products` — úsalos para combinar el cuadro de búsqueda con los filtros de
departamento/categoría ya existentes. `inStock: true` restringe el resultado a productos con
`stock > 0` (por defecto `false`, no filtra por stock).

Los resultados donde `sku` o `name` **empiezan con** el texto buscado aparecen primero; el resto
(coincidencias en medio del texto) aparece después, ya paginado en ese orden — no es necesario
ordenar nada del lado del frontend. Respuesta `200` con la misma forma que `/v1/products`:

```json
{
  "total": 11,
  "docs": [
    {
      "sicarUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un",
      "sku": "PR2057",
      "name": "PORTAROLLO",
      "descriptionDetails": null,
      "imageUrl": null,
      "price": 8.62069,
      "stock": 2.0
    }
  ]
}
```

`q` no puede ir vacío (`422` si lo está o si falta). Mismos límites de paginación que
`/v1/products`: `limit` entre 1 y 200 (por defecto 60), `offset` ≥ 0 (`422` fuera de rango).

### `GET /v1/products/{uuid}` — detalle de producto

```http
GET /v1/products/3Cny4OOxdX1GoSzL9rEsTZNL7un
x-api-key: <api-key>
```

Respuesta `200` incluye todos los campos de `POST /v1/products` más `tags`, `additionalImages`,
`descriptionDetails` (puede tardar un poco más la primera vez si el detalle está desactualizado
— internamente refresca desde Sicar X antes de responder).

### `GET /v1/taxonomy` — departamentos y categorías (para filtros)

```http
GET /v1/taxonomy
x-api-key: <api-key>
```

Respuesta `200`:
```json
{
  "departments": [
    {
      "uuid": "4aa3e82c-3ea2-4018-b8a7-12e727247cfa",
      "name": "FERRETERÍA",
      "order": 70,
      "categories": [
        { "uuid": "137bcaba-5aa2-4559-8545-2cab151d8369", "name": "VIDAL" }
      ]
    }
  ]
}
```

Una categoría puede aparecer bajo varios departamentos (relación muchos-a-muchos), no es una
jerarquía estricta.

### `GET`/`PUT`/`DELETE /v1/cart` — carrito persistente

Carrito guardado del lado del servidor (sobrevive a que se borre el `localStorage`, o a cambiar
de dispositivo) — **no reemplaza** el carrito en memoria del frontend, es una capa opcional de
persistencia. Funciona tanto sin login (carrito anónimo) como con login (carrito de cuenta).
Solo guarda `{uuid, quantity}` por producto — precio, nombre, stock e imagen **siempre** se leen
en vivo del catálogo local al consultarlo, nunca se guardan como estaban al agregar el producto.

**Sin login** — no mandes `X-Client-Token` ni `Authorization`. La primera vez que hagas
`PUT /v1/cart`, la respuesta incluye un `cartToken`; guárdalo (localStorage) y reenvíalo en la
cabecera `X-Cart-Token` en las siguientes llamadas para seguir usando el mismo carrito:

```http
PUT /v1/cart
x-api-key: <api-key>
X-Cart-Token: <token guardado, omitir en la primera llamada>
Content-Type: application/json

{
  "items": [
    { "uuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "quantity": 2 }
  ]
}
```

`PUT` **reemplaza el carrito completo** — no es agregar/quitar un producto, es mandar el estado
completo deseado cada vez (el frontend ya arma esta lista en memoria de todas formas). Si se manda
un `X-Cart-Token` que no existe o se omite por completo, se crea un carrito nuevo silenciosamente
(no es un error) y su token viene en la respuesta.

Respuesta `200` (misma forma para `GET`, `PUT` y `POST /v1/cart/merge`):
```json
{
  "items": [
    {
      "productUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un",
      "sku": "PR2057",
      "name": "PORTAROLLO",
      "imageUrl": null,
      "price": 8.62,
      "stock": 2.0,
      "quantity": 2,
      "lineTotal": 17.24,
      "available": true
    }
  ],
  "subtotal": 17.24,
  "totalQuantity": 2,
  "cartToken": "5a5c479d-9aeb-49ce-bfcd-3ff285a64188",
  "updatedAt": "2026-07-18T14:36:17Z"
}
```

`subtotal` (no `total` — ese nombre ya significa "cantidad de filas" en `/v1/products`/`/v1/search`
y en el historial de pedidos) es la suma de `lineTotal` **solo de los productos `available: true`**.
Un producto puede aparecer con `available: false` (y sin `sku`/`name`/`price`/`stock`/`lineTotal`,
todos `null`) si ya no existe en el catálogo local o fue desactivado/eliminado — **no desaparece
de `items`**, muéstralo igual pero indica que ya no está disponible (p. ej. "Ya no disponible,
quítalo del carrito"); no cuenta en `subtotal` pero sí en `totalQuantity`.

`GET /v1/cart` sin ningún header de identidad (ni `X-Client-Token` ni `X-Cart-Token`) responde un
carrito vacío (`items: []`) sin crear nada. `DELETE /v1/cart` vacía el carrito resuelto por esos
mismos headers, responde `204` siempre (incluso si no había nada que borrar).

**Con login** — igual, pero manda `X-Client-Token` en vez de (o junto con) `X-Cart-Token`; si
`X-Client-Token` está presente y es válido, siempre gana sobre `X-Cart-Token` y el carrito es el
de la cuenta, no el anónimo. `cartToken` en la respuesta viene `null` en este caso (no hace falta,
ya tienes `X-Client-Token`).

Errores esperables:
- `401` — se mandó `X-Client-Token` pero es inválido/expiró (a diferencia de un `X-Cart-Token`
  desconocido, que nunca es error — ver arriba)
- `422` — algún `quantity` es `0` o negativo, o algún `uuid` de producto tiene un formato inválido
  (rechaza toda la petición, no solo esa línea)

### `POST /v1/cart/merge` — fusionar el carrito anónimo a la cuenta

Llama a este endpoint justo después de un login/registro exitoso **si** el frontend ya tenía un
`cartToken` guardado de antes de iniciar sesión (carrito armado sin cuenta). **Usa `Authorization`**
para el token de cuenta aquí, no `X-Client-Token` (ver la nota en "Dos capas de autenticación"
arriba) — igual que `/v1/auth/me/addresses`.

```http
POST /v1/cart/merge
x-api-key: <api-key>
Authorization: <token de /v1/auth/login o /v1/auth/register>
Content-Type: application/json

{
  "cartToken": "5a5c479d-9aeb-49ce-bfcd-3ff285a64188"
}
```

Si la cuenta no tenía carrito propio todavía, simplemente adopta el anónimo. Si ya tenía uno, las
cantidades se **suman** por producto en común (2 + 3 = 5, no se reemplaza) y el resto se agrega.
Responde `200` con el mismo shape de arriba (`cartToken: null`, ya es el carrito de la cuenta).
Después de fusionar, borra el `cartToken` guardado localmente — ya no sirve, el carrito anónimo
fue consumido. `404` si `cartToken` no corresponde a un carrito anónimo existente (ya fue
fusionado antes, o nunca existió) — en ese caso no hay nada que fusionar, solo continúa usando
`X-Client-Token` normalmente.

### `POST /v1/orders` — crear pedido

Contrato mínimo: solo el carrito y los datos de entrega. **Todo lo demás (precios, impuestos,
sku, totales) lo calcula el backend.** Requiere **login** — ver punto 3 de "Dos capas de
autenticación" arriba: `X-Client-Token` es obligatorio junto con `Authorization`, ya no existe
checkout anónimo.

```http
POST /v1/orders
x-api-key: <api-key>
Authorization: <token de /v1/session/init>
X-Client-Token: <token de /v1/auth/login o /v1/auth/register>
Content-Type: application/json

{
  "products": [
    { "uuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "quantity": 1 }
  ],
  "deliveryInfo": {
    "contactInfo": {
      "name": "Juan Pérez",
      "phone": "3151234567",
      "email": "juan@example.com"
    },
    "deliveryType": "PICKUP"
  }
}
```

`deliveryType` soportado hoy: `"PICKUP"` (recoger en tienda) — es el único valor aceptado
(`422` si se envía cualquier otro). `contactInfo.email` es opcional, pero si se envía debe ser
un correo válido (también `422` si no lo es).

Respuesta `200`:
```json
{
  "id": "6a55165ada77fe7cd25d39e3",
  "serieFolio": "TL518",
  "date": 1783961178060.0,
  "status": "ACTIVE",
  "orderUuid": "f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6"
}
```

**Guarda `id`** — es lo que se usa como `{order_id}` en la URL de `POST /v1/orders/{order_id}/cancel`
si el cliente necesita cancelar. **Guarda también `orderUuid`** — es el identificador local del
pedido, usado para verlo después en `GET /v1/auth/me/orders/{orderUuid}` (ver ese endpoint arriba).
El pedido ya queda pagado y confirmado en Sicar X al recibir esta respuesta (no hay un paso de pago
aparte del lado del frontend).

Errores esperables:
- `401` — falta o expiró el token de sesión, o falta/es inválido `X-Client-Token` (llama de nuevo a
  `/v1/session/init` o `/v1/auth/login` según cuál haya fallado)
- `400` — carrito vacío o datos de entrega inválidos
- `409` — uno o más productos sin disponibilidad suficiente
- `502` — Sicar X rechazó la orden o el pago (reintenta más tarde)

### `POST /v1/orders/{order_id}/cancel` — cancelar pedido

Requiere `X-Client-Token` — el pedido debe pertenecer a la cuenta autenticada, o responde `404`
(sin revelar si el pedido existe pero es de otra cuenta).

```http
POST /v1/orders/6a55165ada77fe7cd25d39e3/cancel
x-api-key: <api-key>
X-Client-Token: <token de /v1/auth/login o /v1/auth/register>
Content-Type: application/json

{
  "products": [
    { "uuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "quantity": 1 }
  ]
}
```

`{order_id}` en la URL es el `id` que devolvió `POST /v1/orders` (ya no va en el body — no requiere
`Authorization`, no es un token de sesión). El body ya no lleva `uuid`: solo `products`, que debe
repetir el mismo carrito del pedido original para que el stock local se restaure correctamente, y
`cashRegisterUuid` (opcional — tiene un valor por defecto del lado del servidor, solo hace falta
enviarlo si se necesita cancelar contra una caja distinta a la default):

```json
{
  "cashRegisterUuid": "8f3e6a2c-1d4b-4f0a-9c7e-2b5a6d1f0e33",
  "products": [
    { "uuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "quantity": 1 }
  ]
}
```

Respuesta `200`:
```json
{
  "documentUuid": "6a55165ada77fe7cd25d39e3",
  "sicarTimestamp": 1783961225017.0,
  "message": "Pedido cancelado exitosamente.",
  "status": "CANCELLED"
}
```

---

## Ejemplo mínimo (fetch, Next.js)

```ts
const API_URL = process.env.NEXT_PUBLIC_API_URL!;   // ej. https://api-production-cf7a.up.railway.app
const API_KEY = process.env.NEXT_PUBLIC_API_KEY!;    // provisto por backend

async function initSession(previousToken?: string) {
  const res = await fetch(`${API_URL}/v1/session/init`, {
    method: "POST",
    headers: {
      "x-api-key": API_KEY,
      ...(previousToken ? { Authorization: previousToken } : {}),
    },
  });
  if (!res.ok) throw new Error("No se pudo iniciar sesión");
  return res.json(); // { token, priceListUuid, branchId, deliveryCost, contentId }
}

async function getCatalog(filters: { limit?: number; offset?: number; departmentUuid?: string }) {
  const res = await fetch(`${API_URL}/v1/products`, {
    method: "POST",
    headers: { "x-api-key": API_KEY, "Content-Type": "application/json" },
    body: JSON.stringify({ limit: 60, offset: 0, ...filters }),
  });
  return res.json(); // { total, docs }
}

async function saveCart(items: { uuid: string; quantity: number }[], cartToken?: string, clientToken?: string) {
  const res = await fetch(`${API_URL}/v1/cart`, {
    method: "PUT",
    headers: {
      "x-api-key": API_KEY,
      "Content-Type": "application/json",
      ...(clientToken ? { "X-Client-Token": clientToken } : cartToken ? { "X-Cart-Token": cartToken } : {}),
    },
    body: JSON.stringify({ items }),
  });
  if (!res.ok) throw new Error("No se pudo guardar el carrito");
  return res.json(); // { items, subtotal, totalQuantity, cartToken, updatedAt }
}

async function mergeCartAfterLogin(clientToken: string, cartToken: string) {
  const res = await fetch(`${API_URL}/v1/cart/merge`, {
    method: "POST",
    headers: {
      "x-api-key": API_KEY,
      Authorization: clientToken, // OJO: Authorization aqui, no X-Client-Token
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ cartToken }),
  });
  if (res.status === 404) return null; // ya fusionado o nunca existio, nada que hacer
  if (!res.ok) throw new Error("No se pudo fusionar el carrito");
  return res.json();
}

async function createOrder(sessionToken: string, clientToken: string, products: { uuid: string; quantity: number }[], contactInfo: { name: string; phone: string; email?: string }) {
  const res = await fetch(`${API_URL}/v1/orders`, {
    method: "POST",
    headers: {
      "x-api-key": API_KEY,
      Authorization: sessionToken,
      "X-Client-Token": clientToken,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      products,
      deliveryInfo: { contactInfo, deliveryType: "PICKUP" },
    }),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail ?? "No se pudo crear el pedido");
  }
  return res.json(); // { id, serieFolio, date, status, orderUuid }
}
```

## Notas y advertencias

- **Precios/stock pueden cambiar entre que se muestran y se compran** — `/v1/orders` valida
  disponibilidad en tiempo real contra Sicar X antes de confirmar; un `409` en checkout es
  normal y esperado, no un bug.
- **El token de sesión expira** — si `/v1/orders` responde `401`, vuelve a llamar
  `/v1/session/init` pasando el token viejo en `Authorization` para refrescarlo, y reintenta. Si
  en cambio es `X-Client-Token` el que expiró/falta, vuelve a llamar `/v1/auth/login`.
- **Login es obligatorio para comprar** — no existe checkout anónimo; sin una cuenta autenticada
  (`X-Client-Token` válido) `/v1/orders` y `/v1/orders/{order_id}/cancel` responden `401`.
- **Seguimiento post-compra sí existe**: `GET /v1/auth/me/orders` (lista) y
  `GET /v1/auth/me/orders/{orderUuid}` (detalle, con `dispatchStatus`/`dispatchHistory`) — ver
  referencia arriba.
- **`/v1/cart` no valida disponibilidad en tiempo real** — a diferencia de `/v1/orders`, guardar
  o leer el carrito no consulta a Sicar X, solo el catálogo local (que se sincroniza cada 5 min).
  El `409` de "sin disponibilidad suficiente" solo puede pasar hasta el checkout real
  (`/v1/orders`), no al guardar el carrito.
- **La cabecera de la cuenta cambia según la ruta del carrito** — `X-Client-Token` en
  `GET`/`PUT`/`DELETE /v1/cart`, `Authorization` en `POST /v1/cart/merge`. Revisa la sección
  "Dos capas de autenticación" (punto 4) y los ejemplos de cada endpoint si algo da `401`
  inesperado.
