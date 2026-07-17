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

Sin este header, cualquier ruta responde `401`.

### 2. Token de sesión del cliente — solo para `POST /orders`

Un JWT por comprador, obtenido de `POST /session/init` y reenviado como `Authorization` al
crear un pedido. **Nunca** se usa en ninguna otra ruta — `/catalog`, `/products/{uuid}`,
`/taxonomy` y `/cancel` no lo necesitan, solo el `x-api-key`.

## Flujo típico de una compra

```
1. POST /session/init            → obtener token de sesión (una vez, guardar en el cliente)
2. POST /catalog                 → mostrar catálogo / resultados de filtro
3. GET  /products/{uuid}         → detalle al abrir una ficha de producto
4. POST /orders                  → crear el pedido (usa el token del paso 1)
5. (si aplica) POST /cancel      → cancelar el pedido creado en el paso 4
```

`/session/init` normalmente se llama **una sola vez** al iniciar la sesión de compra (p. ej. al
cargar el carrito o al primer intento de checkout), no en cada request. Guarda el `token`
devuelto (en memoria, cookie, o storage del lado cliente) y reenvíalo tal cual en `Authorization`
al llamar a `/orders`.

---

## Referencia de endpoints

### `POST /session/init` — iniciar o refrescar sesión

Sin `Authorization`: crea una sesión anónima nueva. Con `Authorization` (token previo): lo valida
y refresca si expiró.

```http
POST /session/init
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

Guarda `token` — es lo que se reenvía como `Authorization` en `/orders`.

### `POST /auth/register` / `POST /auth/login` — cuentas de cliente (login propio, separado de Sicar X)

Esto es un tercer tipo de token, **distinto** del token de `/session/init` de arriba. `/session/init`
gestiona la sesión anónima de compra contra Sicar X (necesaria para `/orders`); `/auth/register` y
`/auth/login` son cuentas de cliente propias de esta API — para que un usuario tenga un login
persistente en el sitio (guardar direcciones, ver histórico, etc. — todavía no implementado, solo
existe el login por ahora). Ambos requieren `x-api-key` igual que cualquier otra ruta.

```http
POST /auth/register
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
que `/auth/login` — el registro inicia sesión automáticamente, no hace falta llamar a `/auth/login`
después:

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
POST /auth/login
x-api-key: <api-key>
Content-Type: application/json

{
  "email": "juan@example.com",
  "password": "unaContraseñaSegura"
}
```

Misma respuesta `200` que arriba. `401` si el correo o la contraseña son incorrectos.

Guarda este `token` — es el que se reenvía como `Authorization` en `GET`/`PATCH /auth/me` abajo.
**Nota:** todavía no está conectado a `/orders` — los pedidos se siguen creando de forma anónima
con el token de `/session/init`, sin importar si el cliente tiene cuenta o no.

### `GET /auth/me` — datos de la cuenta (para "Mi cuenta")

```http
GET /auth/me
x-api-key: <api-key>
Authorization: <token de /auth/register o /auth/login>
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
      "ext_number": "123",
      "int_number": null,
      "neighborhood": null,
      "city": "Culiacán",
      "state": "Sinaloa",
      "country": "México",
      "zip_code": "80000",
      "references": null,
      "is_default": true
    }
  ]
}
```

`addresses` viene incluido de una vez (no hace falta llamar a `GET /auth/me/addresses` aparte solo
para pintar "Mi cuenta"), pero para agregar/editar/eliminar una dirección sí se usan las rutas de
abajo. `401` si falta el `Authorization`, el token es inválido/expiró, o la cuenta ya no existe/está
desactivada — en cualquiera de esos casos, manda al usuario de vuelta a login.

### `PATCH /auth/me` — editar nombre, teléfono o contraseña

Todos los campos son opcionales — solo se cambia lo que se envíe.

```http
PATCH /auth/me
x-api-key: <api-key>
Authorization: <token de /auth/register o /auth/login>
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
  "current_password": "unaContraseñaSegura",
  "new_password": "unaContraseñaNuevaSegura"
}
```

`new_password` requiere mínimo 8 caracteres (`422` si es más corta). `401` si `current_password`
no coincide con la actual. Responde `200` con el mismo shape que `GET /auth/me`, ya actualizado
(este endpoint no toca `email` ni `addresses` — usa las rutas de abajo para direcciones).

### `GET/POST/PATCH/DELETE /auth/me/addresses` — libro de direcciones

Direcciones guardadas de la cuenta, como recurso aparte (no se editan desde `PATCH /auth/me`).
Todas requieren `x-api-key` + el mismo `Authorization` que `/auth/me`.

```http
GET /auth/me/addresses
x-api-key: <api-key>
Authorization: <token>
```

Responde `200` con un arreglo (mismo shape que `addresses` dentro de `GET /auth/me`).

```http
POST /auth/me/addresses
x-api-key: <api-key>
Authorization: <token>
Content-Type: application/json

{
  "label": "Casa",
  "street": "Av. Siempre Viva",
  "ext_number": "123",
  "city": "Culiacán",
  "state": "Sinaloa",
  "country": "México",
  "zip_code": "80000",
  "is_default": true
}
```

Solo `street` es obligatorio (`422` si falta). `is_default: true` desmarca automáticamente
cualquier otra dirección default que el cliente ya tuviera — solo puede haber una a la vez.
Responde `201` con la dirección creada (incluye su `uuid`, que es lo que identifica la dirección
en `PATCH`/`DELETE` de abajo — nunca un índice de arreglo).

```http
PATCH /auth/me/addresses/{uuid}
x-api-key: <api-key>
Authorization: <token>
Content-Type: application/json

{
  "label": "Casa (nueva referencia)",
  "is_default": true
}
```

Todos los campos son opcionales — solo se cambia lo que se envíe. Responde `200` con la dirección
actualizada. `404` si el `uuid` no existe o no pertenece al cliente autenticado.

```http
DELETE /auth/me/addresses/{uuid}
x-api-key: <api-key>
Authorization: <token>
```

`204` sin contenido si se elimina correctamente. `404` si el `uuid` no existe o no pertenece al
cliente autenticado — igual que `PATCH`, nunca revela si la dirección de otro cliente existe.

### `POST /catalog` — catálogo local (paginado, sin llamadas a Sicar X)

```http
POST /catalog
x-api-key: <api-key>
Content-Type: application/json

{
  "limit": 60,
  "offset": 0,
  "department_uuid": null,
  "category_uuid": null,
  "tag": null,
  "in_stock": false,
  "sort_by": null
}
```

Respuesta `200`:
```json
{
  "total": 124149,
  "docs": [
    {
      "sicar_uuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un",
      "sku": "PR2057",
      "name": "PORTAROLLO",
      "description_details": null,
      "image_url": null,
      "price": 8.62069,
      "stock": 2.0
    }
  ]
}
```

Usa `department_uuid`/`category_uuid` (de `GET /taxonomy`) para filtrar, y `tag` para ofertas u
otras etiquetas. `in_stock: true` restringe a productos con `stock > 0` (por defecto `false`).
Pagina con `limit`/`offset`.

`sort_by` ordena los resultados — valores válidos: `"price_asc"`, `"price_desc"`, `"name_asc"`.
Cualquier otro valor responde `422`. Si se omite (`null`), no hay orden garantizado entre
llamadas — usa `sort_by` siempre que el orden le importe a la UI (p. ej. un selector de
"Ordenar por: Precio menor a mayor / mayor a menor / Nombre A-Z").

### `POST /search` — buscar por sku o nombre

```http
POST /search
x-api-key: <api-key>
Content-Type: application/json

{
  "q": "portarollo",
  "limit": 60,
  "offset": 0,
  "department_uuid": null,
  "category_uuid": null,
  "in_stock": false
}
```

Coincidencia por substring (contiene), sin distinguir mayúsculas/minúsculas, contra `sku` **o**
`name` en un solo campo de búsqueda. `department_uuid`/`category_uuid` son opcionales y funcionan
igual que en `/catalog` — úsalos para combinar el cuadro de búsqueda con los filtros de
departamento/categoría ya existentes. `in_stock: true` restringe el resultado a productos con
`stock > 0` (por defecto `false`, no filtra por stock).

Los resultados donde `sku` o `name` **empiezan con** el texto buscado aparecen primero; el resto
(coincidencias en medio del texto) aparece después, ya paginado en ese orden — no es necesario
ordenar nada del lado del frontend. Respuesta `200` con la misma forma que `/catalog`:

```json
{
  "total": 11,
  "docs": [
    {
      "sicar_uuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un",
      "sku": "PR2057",
      "name": "PORTAROLLO",
      "description_details": null,
      "image_url": null,
      "price": 8.62069,
      "stock": 2.0
    }
  ]
}
```

`q` no puede ir vacío (`422` si lo está o si falta).

### `GET /products/{uuid}` — detalle de producto

```http
GET /products/3Cny4OOxdX1GoSzL9rEsTZNL7un
x-api-key: <api-key>
```

Respuesta `200` incluye todos los campos de `/catalog` más `tags`, `additional_images`,
`description_details` (puede tardar un poco más la primera vez si el detalle está desactualizado
— internamente refresca desde Sicar X antes de responder).

### `GET /taxonomy` — departamentos y categorías (para filtros)

```http
GET /taxonomy
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

### `POST /orders` — crear pedido

Contrato mínimo: solo el carrito y los datos de entrega. **Todo lo demás (precios, impuestos,
sku, totales) lo calcula el backend.**

```http
POST /orders
x-api-key: <api-key>
Authorization: <token de /session/init>
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

`deliveryType` soportado hoy: `"PICKUP"` (recoger en tienda). `contactInfo.email` es opcional.

Respuesta `200`:
```json
{
  "id": "6a55165ada77fe7cd25d39e3",
  "serieFolio": "TL518",
  "date": 1783961178060.0,
  "status": "ACTIVE"
}
```

**Guarda `id`** — es lo que se manda a `/cancel` si el cliente necesita cancelar. El pedido ya
queda pagado y confirmado en Sicar X al recibir esta respuesta (no hay un paso de pago aparte del
lado del frontend).

Errores esperables:
- `401` — falta o expiró el token de sesión (llama de nuevo a `/session/init`)
- `400` — carrito vacío o datos de entrega inválidos
- `409` — uno o más productos sin disponibilidad suficiente
- `502` — Sicar X rechazó la orden o el pago (reintenta más tarde)

### `POST /cancel` — cancelar pedido

```http
POST /cancel
x-api-key: <api-key>
Content-Type: application/json

{
  "uuid": "6a55165ada77fe7cd25d39e3",
  "products": [
    { "uuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "quantity": 1 }
  ]
}
```

`uuid` es el `id` que devolvió `/orders` (no un token de sesión, no requiere `Authorization`).
`products` debe repetir el mismo carrito del pedido original, para que el stock local se
restaure correctamente.

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
  const res = await fetch(`${API_URL}/session/init`, {
    method: "POST",
    headers: {
      "x-api-key": API_KEY,
      ...(previousToken ? { Authorization: previousToken } : {}),
    },
  });
  if (!res.ok) throw new Error("No se pudo iniciar sesión");
  return res.json(); // { token, priceListUuid, branchId, deliveryCost, contentId }
}

async function getCatalog(filters: { limit?: number; offset?: number; department_uuid?: string }) {
  const res = await fetch(`${API_URL}/catalog`, {
    method: "POST",
    headers: { "x-api-key": API_KEY, "Content-Type": "application/json" },
    body: JSON.stringify({ limit: 60, offset: 0, ...filters }),
  });
  return res.json(); // { total, docs }
}

async function createOrder(sessionToken: string, products: { uuid: string; quantity: number }[], contactInfo: { name: string; phone: string; email?: string }) {
  const res = await fetch(`${API_URL}/orders`, {
    method: "POST",
    headers: {
      "x-api-key": API_KEY,
      Authorization: sessionToken,
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
  return res.json(); // { id, serieFolio, date, status }
}
```

## Notas y advertencias

- **Precios/stock pueden cambiar entre que se muestran y se compran** — `/orders` valida
  disponibilidad en tiempo real contra Sicar X antes de confirmar; un `409` en checkout es
  normal y esperado, no un bug.
- **El token de sesión expira** — si `/orders` responde `401`, vuelve a llamar `/session/init`
  pasando el token viejo en `Authorization` para refrescarlo, y reintenta.
- **No hay endpoint de "estado del pedido"** todavía — si el frontend necesita mostrar
  seguimiento post-compra, es una conversación aparte con backend (no existe hoy).
