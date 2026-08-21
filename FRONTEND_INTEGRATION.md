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

### 1. `x-api-key` — obligatorio en **todas** las rutas que llama el frontend

Header estático que autentica al frontend contra esta API (no contra Sicar X). Un solo valor,
provisto por el equipo backend, se manda igual en cada request:

```
x-api-key: <valor provisto por backend>
```

Sin este header, cualquier ruta responde `403`. La única excepción en todo el backend es
`POST /v1/payments/webhook` (lo llama Mercado Pago, no el frontend — no puede mandar este
header; se autentica distinto, ver esa sección más abajo) — no es una ruta que el frontend
necesite llamar nunca.

### 2. Token de cuenta de cliente (`X-Client-Token`) — opcional para `POST /v1/orders` (checkout de invitado, 2026-08-11)

**Actualización (2026-08-11): ya no es obligatorio tener cuenta para comprar** — existe
checkout de invitado, ver la sección "Checkout de invitado" más abajo antes de asumir que
falta este header siempre significa `401`. Cuando SÍ se manda, es un segundo JWT, distinto
del `x-api-key`, obtenido de `POST /v1/auth/register` o `POST /v1/auth/login` y reenviado en
una cabecera aparte, `X-Client-Token`. Identifica qué cuenta queda dueña del pedido, para que
después pueda verlo en `GET /v1/auth/me/orders`. En `POST /v1/orders`,
`POST /v1/orders/{order_id}/pay`, `POST /v1/orders/{order_id}/cancel` y
`DELETE /v1/orders/{order_id}`, omitir este header por completo ya no es un error — es la
señal de "este es un checkout de invitado". Si SÍ se manda pero es inválido/expiró, sigue
siendo `401` (a diferencia de omitirlo, que ahora es un camino soportado, no un fallback
silencioso).

(Antes existía un tercer token, el de sesión de Sicar X obtenido de `POST /v1/session/init` y
reenviado en `Authorization` — esa ruta y ese requisito fueron eliminados por completo: la
validación de carrito ahora es puramente local, sin ninguna llamada en vivo a Sicar X en el
checkout. Si tu build todavía llama a `/v1/session/init` o manda `Authorization` en `/v1/orders`,
quítalo — `Authorization` ya no tiene ningún uso en esa ruta.)

### 3. Cookie del carrito anónimo (`charly_cart_token`) — automática, sin gestionarla a mano

El carrito anónimo (sin login) ya **no** se identifica con un header manual — el backend emite una
cookie `httpOnly` (`charly_cart_token`, alcance `/v1/cart`) la primera vez que se escribe un
carrito sin sesión, y el navegador la reenvía solo en las siguientes llamadas a `/v1/cart*`. El
frontend **no puede ni necesita leer su valor** (es `httpOnly`, invisible a JavaScript) y ya no hay
que guardar nada en `localStorage` para esto.

Requisito indispensable: como el frontend (`ferreteriacharly.com`) y esta API
(`api-production-cf7a.up.railway.app`) están en dominios distintos, es una cookie *cross-site* —
toda llamada `fetch`/`axios` a `/v1/cart*` debe mandar `credentials: "include"` (u
`withCredentials: true` en axios) o el navegador nunca la envía ni la guarda, y cada llamada se ve
como un visitante anónimo nuevo. Ver la sección `/v1/cart` más abajo.

Para el carrito, el mismo token de cuenta del punto 2 sigue mandándose de forma distinta según la
ruta: `X-Client-Token` en `GET`/`PUT`/`DELETE`/`PATCH /v1/cart*`, pero `Authorization` en
`POST /v1/cart/merge` (igual que `/v1/auth/me/addresses` y `/v1/auth/me/orders`, que también usan
`Authorization`). No es un error tipográfico — revisa la cabecera exacta de cada ejemplo con
cuidado.

Nota aparte: `GET`/`PUT`/`PATCH /v1/cart*` siguen devolviendo `cartToken` en el cuerpo de la
respuesta cuando el carrito es anónimo (igual que antes) — pero ahora **solo sirve** para mandarlo
como `cartToken` en el body de `POST /v1/auth/register`/`POST /v1/auth/login` (ver más abajo) o de
`POST /v1/cart/merge`, ya que la cookie `httpOnly` tiene alcance `/v1/cart` y por diseño **no** se
envía a `/v1/auth/*`. Guárdalo en memoria (variable/estado, no hace falta `localStorage`) justo
después de armar el carrito sin sesión, por si el visitante inicia sesión o se registra después.

## Flujo típico de una compra

```
1. POST /v1/auth/register o /v1/auth/login          → obtener token de cuenta (X-Client-Token), una vez
   (opcional desde 2026-08-11 — se puede saltar este paso por completo, ver "Checkout de invitado")
2. POST /v1/products                                 → mostrar catálogo / resultados de filtro
3. GET  /v1/products/{uuid}                          → detalle al abrir una ficha de producto
4. POST /v1/orders                                   → reservar el pedido localmente (queda TO_PAY) + preparar el cobro
5. Renderizar el Payment Brick (Mercado Pago) con `amount`/`preferenceId` del paso 4
6. POST /v1/orders/{order_id}/pay                    → cobrar (tarjeta/OXXO — el Brick llama a esto en su onSubmit)
   (el método Wallet de Mercado Pago NO llama a este paso — redirige directo a Mercado Pago)
7. (si aplica) POST /v1/orders/{order_id}/cancel     → cancelar el pedido (usa el token del paso 1, o el `id`/`orderUuid` del paso 4 si fue invitado)
```

**Importante — esto es un cambio incompatible sobre el flujo anterior**: `POST /v1/orders`
ya **no** cobra ni deja el pedido pagado de inmediato — ahora solo lo reserva localmente
(`status: "TO_PAY"`) y prepara una preferencia de Mercado Pago. El pago real ocurre en el
paso 6 (`POST /v1/orders/{order_id}/pay`) o, si el comprador elige pagar con su cuenta de
Mercado Pago (Wallet), nunca pasa por este backend en absoluto — se confirma por webhook.

**Importante — segundo cambio incompatible (2026-07-31): Sicar X ya no se entera de un
pedido en absoluto hasta que un administrador lo acepta.** Antes, `POST /v1/orders` creaba
un documento real en Sicar X en el acto; ahora Sicar X pasó a ser solo el ERP de inventario
de la tienda — no sabe que este pedido existe hasta que se acepta del lado del dashboard
admin (ver `ADMIN_INTEGRATION.md`), momento en el que este backend le avisa el descuento de
inventario correspondiente de forma asíncrona. Esto no cambia ningún endpoint que ya uses
tal cual — el contrato de `POST /v1/orders`, `/pay` y `/cancel` es el mismo — pero sí cambia
el significado de algunos campos de la respuesta, ver la nota junto al ejemplo de respuesta
más abajo.
Ver "Pagos con Mercado Pago" más abajo.

Guarda el token de `/v1/auth/login`/`/v1/auth/register` para reenviarlo en `X-Client-Token` en
`/v1/orders` y `/v1/orders/{order_id}/cancel` — es el único token que necesita ese flujo. Si el
comprador no tiene cuenta, omite `X-Client-Token` por completo y sigue el flujo de invitado —
ver "Checkout de invitado" más abajo.

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

### `POST /v1/auth/register` / `POST /v1/auth/login` — cuentas de cliente (login propio, separado de Sicar X)

`/v1/auth/register` y `/v1/auth/login` son cuentas de cliente propias de esta API — para que un
usuario tenga un login persistente en el sitio (guardar direcciones, ver histórico, etc.). Ambos
requieren `x-api-key` igual que cualquier otra ruta.

```http
POST /v1/auth/register
x-api-key: <api-key>
Content-Type: application/json

{
  "name": "Juan Pérez",
  "email": "juan@example.com",
  "phone": "3151234567",
  "password": "unaContraseñaSegura",
  "cartToken": "5a5c479d-9aeb-49ce-bfcd-3ff285a64188"
}
```

`password` requiere mínimo 8 caracteres (`422` si es más corta). `cartToken` es **opcional** — si
el visitante ya tenía un carrito anónimo armado antes de registrarse (ver la cookie del carrito más
arriba), mándalo aquí y se fusiona a la cuenta nueva en la misma llamada, sin un segundo request a
`POST /v1/cart/merge`. Un `cartToken` ausente, vencido o que ya no corresponde a ningún carrito
**no** hace fallar el registro — simplemente se ignora. Responde `201` (creó una cuenta
nueva — antes de 2026-08-13 este endpoint respondía `200`; si tu cliente HTTP solo aceptaba
`200` como éxito, actualízalo para aceptar `2xx`) con el mismo shape que `/v1/auth/login`
(que sigue respondiendo `200`, no crea nada) — el registro inicia sesión automáticamente,
no hace falta llamar a `/v1/auth/login` después:

```json
{
  "token": "eyJhbGciOiJIUzI1NiIs...",
  "client": {
    "uuid": "f6bacfb9-cb38-4f96-adab-2593a14345bc",
    "name": "Juan Pérez",
    "email": "juan@example.com",
    "phone": "3151234567",
    "isVerified": false,
    "authProvider": "local"
  },
  "cart": {
    "items": [ { "productUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "sku": "PR2057", "name": "PORTAROLLO", "imageUrl": null, "price": 8.62, "stock": 2.0, "quantity": 2, "lineTotal": 17.24, "available": true } ],
    "subtotal": 17.24,
    "totalQuantity": 2,
    "cartToken": null,
    "updatedAt": "2026-07-18T14:36:17Z"
  }
}
```

`cart` viene **siempre**, se haya mandado `cartToken` o no — es el carrito real de la cuenta ya
fusionado (o vacío si la cuenta no tenía ninguno y no se mandó `cartToken`). Úsalo para hidratar el
estado del carrito en el frontend inmediatamente después de registrarse, sin un `GET /v1/cart`
aparte. `cartToken` dentro de `cart` siempre viene `null` aquí — ya es el carrito de la cuenta, no
uno anónimo. `409` si el correo ya está registrado (o, si ese correo ya está vinculado a Google,
`409` con un mensaje distinto pidiendo iniciar sesión con Google).

`isVerified` empieza en `false` para una cuenta nueva por registro local — este backend dispara un
correo de verificación automáticamente (ver el webhook `verification-requested` más abajo), pero
**no bloquea nada mientras tanto**: la cuenta puede iniciar sesión y comprar de inmediato. Es
responsabilidad del frontend decidir qué hacer con `isVerified: false` (p. ej. mostrar un banner
"verifica tu correo"), no una restricción de este backend. `authProvider` es `"local"` o
`"google"` — útil, por ejemplo, para ocultar la opción de "cambiar contraseña" en "Mi cuenta" si
la cuenta es `"google"` (no tiene contraseña local, ver `PATCH /v1/auth/me` más abajo).

```http
POST /v1/auth/login
x-api-key: <api-key>
Content-Type: application/json

{
  "email": "juan@example.com",
  "password": "unaContraseñaSegura",
  "cartToken": "5a5c479d-9aeb-49ce-bfcd-3ff285a64188"
}
```

Misma respuesta `200` que arriba (incluyendo `cart`), mismo comportamiento de `cartToken`
(opcional, tolerante a token ausente/inválido, fusiona en la misma llamada si es válido). `401` si
el correo o la contraseña son incorrectos. `403` si la cuenta existe y la contraseña es correcta
pero fue desactivada (`is_active: false` — no hay flujo de autoservicio para reactivarla hoy,
requiere intervención manual). El correo no distingue mayúsculas/minúsculas
(`Juan@x.com` y `juan@x.com` son la misma cuenta), así que no hace falta normalizar nada del lado
del frontend. `/v1/auth/login` está limitado a 5 intentos por minuto por IP — pasado ese límite
responde `429` con `{"error": "Rate limit exceeded: ..."}`.

Guarda el `token` de la respuesta — se reenvía en dos lugares distintos: como `Authorization` en
`GET`/`PATCH /v1/auth/me` (y las rutas de direcciones/historial de pedidos abajo), y como
`X-Client-Token` en `POST /v1/orders`/`POST /v1/orders/{order_id}/cancel` (ver "Dos capas de
autenticación" arriba — revisa la cabecera exacta de cada ejemplo con cuidado).

### `POST /v1/auth/google` — iniciar sesión o registrarse con Google

```http
POST /v1/auth/google
x-api-key: <api-key>
Content-Type: application/json

{
  "idToken": "eyJhbGciOiJSUzI1NiIs...",
  "cartToken": "5a5c479d-9aeb-49ce-bfcd-3ff285a64188"
}
```

`idToken` es el ID token que **tu frontend obtiene directamente de Google** (Google Identity
Services, flujo client-side — no un authorization code, no un redirect a este backend). Este
backend solo verifica la firma/`aud`/`iss` del token, nunca habla con Google directamente. Mismo
manejo de `cartToken` que `/v1/auth/register`/`/v1/auth/login` (opcional, tolerante). Responde
`200` con el mismo shape que login/registro (`token`/`client`/`cart`), `authProvider: "google"` y
`isVerified: true` (Google ya confirmó el correo, salvo un caso raro donde Google mismo reporte lo
contrario). Primera vez que ese usuario de Google entra → crea la cuenta. Ya existía (mismo Google
`sub`) → inicia sesión en la misma cuenta.

`409` si el correo de la cuenta de Google ya está registrado **localmente con contraseña** — este
backend deliberadamente no fusiona las cuentas automáticamente (evita que la contraseña de quien
haya registrado ese correo primero, sin probar que le pertenece, se quede vigente sobre lo que el
dueño real ahora cree que es una cuenta asegurada por Google). En ese caso, muéstrale al usuario
que inicie sesión con su contraseña — todavía no existe un flujo de "vincular Google a mi cuenta
existente". `403` si la cuenta de Google ya existente fue desactivada (mismo caso que en
`/v1/auth/login`). Limitado a 10 intentos por minuto por IP.

### `POST /v1/auth/verify-email` — confirmar verificación de correo

```http
POST /v1/auth/verify-email
x-api-key: <api-key>
Content-Type: application/json

{
  "token": "eyJhbGciOiJIUzI1NiIs..."
}
```

`token` es el que llega en el webhook `verification-requested` (ver más abajo) — tu backend arma
el link de verificación con ese valor (p. ej. `https://tudominio.com/verificar-correo?token=...`)
y esta ruta lo confirma cuando el usuario lo abre. **No requiere sesión activa** — el usuario puede
estar en un dispositivo distinto al que usó para registrarse, el token en sí prueba que puede leer
ese correo. Responde `200` con el mismo shape que `GET /v1/auth/me` (ya con `isVerified: true`).
Idempotente — confirmar un token ya usado no falla, solo no vuelve a hacer nada. `401` si el token
es inválido o venció (vigencia 24h desde que se generó) — en ese caso, ofrece reenviar desde
`/v1/auth/resend-verification`. Limitado a 10 por minuto por IP.

### `POST /v1/auth/resend-verification` — reenviar correo de verificación

```http
POST /v1/auth/resend-verification
x-api-key: <api-key>
Authorization: <token de /v1/auth/login o /v1/auth/register>
```

Requiere sesión activa a propósito (no acepta un correo suelto sin autenticar, para no habilitar
enumeración de cuentas registradas) — solo tiene sentido desde "Mi cuenta" o un banner post-login,
no desde una pantalla pública. `204` sin contenido si se reenvía. `400` si la cuenta ya está
verificada. Limitado a 5 por minuto por IP.

### `POST /v1/auth/forgot-password` — solicitar recuperación de contraseña

```http
POST /v1/auth/forgot-password
x-api-key: <api-key>
Content-Type: application/json

{
  "email": "juan@example.com"
}
```

Responde `200` **siempre con el mismo body**, exista o no una cuenta con ese correo — a
propósito, para no habilitar enumeración de cuentas registradas:

```json
{ "detail": "Si el correo existe, se enviará un enlace de recuperación." }
```

Si el correo corresponde a una cuenta local activa (con contraseña), dispara el webhook
`password-reset-requested` (ver más abajo) con un token de un solo uso. Si corresponde a
una cuenta que solo inicia sesión con Google (sin contraseña local), el webhook igual se
dispara, pero con `hasPassword: false` y sin token — úsalo para mostrarle a esa persona un
correo que le explique que su cuenta usa Google, en vez de silencio total. Pedir un nuevo
reset invalida cualquier token anterior sin usar de la misma cuenta — solo el enlace más
reciente funciona. Limitado a 5 por minuto por IP.

### `POST /v1/auth/reset-password` — confirmar recuperación de contraseña

```http
POST /v1/auth/reset-password
x-api-key: <api-key>
Content-Type: application/json

{
  "token": "AaBbCc...",
  "newPassword": "unaContraseñaNueva123"
}
```

`token` es el que llega en el webhook `password-reset-requested` — tu backend arma el link
(p. ej. `https://tudominio.com/restablecer-contraseña?token=...`) y esta ruta lo confirma
cuando el usuario lo abre y define su nueva contraseña. **No requiere sesión activa** — el
token en sí prueba propiedad del correo. Responde `200` con el mismo shape que
`/v1/auth/login` (`token`/`client`/`cart`) — el usuario queda logueado de inmediato, sin un
paso extra de login tras el reset. `400` si el token es inválido, ya fue usado, o venció
(vigencia 30 minutos desde que se generó) — en ese caso, hay que pedir uno nuevo desde
`/v1/auth/forgot-password`. Cualquier sesión (token JWT) previa de esa cuenta deja de
funcionar tras un reset exitoso. Limitado a 10 por minuto por IP.

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
  "isVerified": true,
  "authProvider": "local",
  "addresses": [
    {
      "uuid": "51cbf02f-cf83-470e-9313-c586d816c9c0",
      "label": "Casa",
      "street": "Av. Siempre Viva",
      "extNumber": "123",
      "intNumber": null,
      "neighborhood": null,
      "city": "Culiacán",
      "county": "Culiacán",
      "state": "Sinaloa",
      "country": "México",
      "zipCode": "80000",
      "references": null,
      "isDefault": true
    }
  ]
}
```

`county` (municipio) es un campo nuevo — distinto de `city`, opcional como el resto de campos
de dirección, pero **obligatorio si esa dirección se va a usar para un pedido con
`deliveryType: "DELIVERYMAN"`** (ver `POST /v1/orders` más abajo).

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
no coincide con la actual. `400` si la cuenta es `authProvider: "google"` (no tiene contraseña
local que cambiar) — usa `authProvider` de `GET /v1/auth/me` para ocultar esta opción de la UI en
ese caso, en vez de dejar que el usuario la intente y reciba el error. Responde `200` con el mismo
shape que `GET /v1/auth/me`, ya actualizado (este endpoint no toca `email` ni `addresses` — usa
las rutas de abajo para direcciones). Limitado a 10 llamadas por minuto por IP (`429` si se
excede).

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
  "neighborhood": "Centro",
  "city": "Culiacán",
  "county": "Culiacán",
  "state": "Sinaloa",
  "country": "México",
  "zipCode": "80000",
  "latitude": 24.809062,
  "longitude": -107.394012,
  "isDefault": true
}
```

Solo `street` es obligatorio (`422` si falta). `zipCode`, si se envía, debe tener exactamente 5
dígitos (`422` si no). Este backend **no** valida ni geocodifica códigos postales por su cuenta —
resuelve el resto del formulario (estado/ciudad/municipio/colonias y coordenadas) directo desde el
frontend contra la [Geocodes API de envia.com](https://docs.envia.com/docs/geocodes-api-overview)
(`GET https://geocodes.envia.com/zipcode/{country}/{zipcode}`, sin API key, CORS abierto — se puede
llamar directo desde el navegador) y luego manda los campos ya resueltos en este mismo body,
incluyendo `latitude`/`longitude` si los tiene. Si esta dirección se va a usar para entrega a
domicilio (`POST /v1/orders` con `deliveryType: "DELIVERYMAN"`), captura también
`city`/`county`/`state`/`zipCode`/`extNumber`/`neighborhood` — son opcionales aquí, pero el pedido
responde `400` si falta alguno al momento de usarla para entrega (`neighborhood` es obligatorio
porque Sicar X exige `district` no nulo en el pedido, y `district` se llena con este campo). `isDefault: true` desmarca automáticamente
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
      "sicarOrderId": "d65b89dc-9690-40b3-8dfb-aa2cdde18cc0",
      "id": "d65b89dc-9690-40b3-8dfb-aa2cdde18cc0",
      "serieFolio": null,
      "status": "PAID",
      "dispatchStatus": "PENDING_ACCEPTANCE",
      "dispatchHistory": null,
      "total": 129.99,
      "totalQuantity": 3,
      "deliveryInfo": { "contactInfo": { "name": "Juan Pérez", "phone": "3151234567", "email": null }, "deliveryType": "PICKUP" },
      "items": [ { "uuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "sku": "PR2057", "description": "PORTAROLLO", "quantity": "1", "unit": "PZA", "imageUrl": "https://.../portarollo.jpg" } ],
      "createdAt": "2026-07-10T18:32:05Z",
      "cancellationReason": null,
      "couponCode": "WELCOME10",
      "discountAmount": 13.00,
      "subtotal": 142.99,
      "shippingLabel": null
    }
  ]
}
```

**Nuevo (2026-08-13) — `id` es el nombre recomendado, `sicarOrderId` queda como alias
histórico**: mismo valor, mismo campo que `id` en la respuesta de `POST /v1/orders` — se
agregó aquí para que ambas respuestas usen el mismo nombre para el identificador de la
orden. `sicarOrderId` se sigue mandando y se seguirá mandando, no hay fecha de retiro
planeada; el nombre es histórico (de cuando este id sí venía de Sicar X, ya no es el caso).

Cada elemento de `items` lleva `imageUrl` (la `image_url` del producto en el catálogo local al
momento de crear la orden, `null` si el producto no la tenía) — pensado para que el frontend pueda
mostrar la imagen de cada producto en el historial de pedidos y en el correo de confirmación
(mismo campo también viaja en el webhook `order-confirmed`, ver más abajo). Órdenes creadas antes
de este cambio no tienen `imageUrl` en sus `items` — trátalo como opcional/posiblemente ausente,
no solo posiblemente `null`.

`couponCode`/`discountAmount`/`subtotal` son `null` si el pedido no usó un cupón (o si es anterior
a esta funcionalidad — trátalo igual que `null`). Cuando sí hay cupón, `total` ya viene con el
descuento aplicado (`total = subtotal - discountAmount`) — es el mismo `total` que se cobró vía
Mercado Pago, no hace falta restar nada del lado del frontend.

`shippingLabel` es `null` hasta que un admin genera una guía real con envia.com
(`POST /v1/admin/orders/{uuid}/shipping/generate`, ver `ADMIN_INTEGRATION.md`) — solo aplica a
pedidos `deliveryType: "DELIVERYMAN"`. Cuando existe, trae `carrier`/`service`/`trackingNumber`/
`labelUrl`/etc.; usa `shippingLabel.trackUrl` como la liga de "sigue tu paquete" — viene
directamente de envia.com, no hay que construirla.

### `GET /v1/auth/me/orders/{orderUuid}` — detalle de un pedido

```http
GET /v1/auth/me/orders/f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6
x-api-key: <api-key>
Authorization: <token de /v1/auth/login o /v1/auth/register>
```

Mismo shape que un elemento de `docs` arriba, y **solo Postgres local** — igual que la lista, sin
ninguna llamada en vivo a Sicar X. `404` si el pedido no existe o no pertenece a la cuenta
autenticada. `status` (`PAID`/`CANCELLED`) es el estado de pago/cancelación propio de esta API;
`dispatchStatus` (`PENDING_ACCEPTANCE`/`PENDING`/`PREPARING`/`COMPLETE`/`DISPATCHED`) es el estado
de cumplimiento/entrega, decidido enteramente por el dashboard admin (ver `ADMIN_INTEGRATION.md`)
— son dos cosas distintas, no las confundas al mostrar el seguimiento del pedido. Para enterarte de
un cambio de `dispatchStatus` en tiempo casi real (en vez de solo al volver a pedir este endpoint),
ver el webhook `order-status-changed` más abajo. `shippingLabel`/`shippingLabel.trackUrl` (ver nota
arriba) es la fuente para un link de "sigue tu paquete" una vez que `dispatchStatus` llega a
`DISPATCHED` vía una guía real.

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
  "taxonomyUuid": null,
  "vehicleUuid": null,
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
      "stock": 2.0,
      "salesCount": 0.0,
      "averageRating": null,
      "reviewsCount": 0
    }
  ]
}
```

Usa `departmentUuid`/`categoryUuid` (de `GET /v1/taxonomy`) para filtrar, y `tag` para ofertas u
otras etiquetas (coincidencia exacta contra los valores en `Product.tags`, p. ej. `"oferta"` o
`"pretul"` — no es substring). `inStock: true` restringe a productos con `stock > 0` (por
defecto `false`). Pagina con `limit`/`offset`.

`taxonomyUuid` filtra por un nodo del árbol propio de categorías (`GET /v1/taxonomy`, distinto de
`categoryUuid`) e incluye productos etiquetados en cualquier descendiente del nodo. `vehicleUuid`
filtra a productos compatibles con un vehículo específico — el `uuid` viene de resolver la
cascada de `GET /v1/vehicles*` (ver esa sección más abajo). Ambos son opcionales y combinables
entre sí y con el resto de los filtros.

`price` siempre viene con 2 decimales exactos (es un `Numeric` en la base de datos, no un
`float`) — no asumas más precisión que esa al mostrarlo o redondearlo del lado del frontend.

**`salesCount` (nuevo, campo aditivo)** — unidades vendidas en pedidos que llegaron a `PAID`
(ver `GET /v1/products/best-sellers` más abajo). Es un `Numeric`, no un entero — algunos
productos se venden por peso/medida, así que puede traer decimales (p. ej. `2.5`). Empieza en
`0.0` para todo el catálogo hasta que existan pedidos pagados reales; no lo trates como
disponible desde el día uno de este cambio.

**`averageRating`/`reviewsCount` (nuevo, campo aditivo)** — promedio y conteo de reseñas
visibles del producto (ver "Reseñas y calificaciones de productos" más abajo), cacheados y
listos para mostrar directo en una tarjeta de producto sin una llamada aparte por producto.
`averageRating` es `null` (no `0`) mientras el producto no tenga ninguna reseña — no lo
confundas con "calificación 0 estrellas" al renderizar.

`sortBy` ordena los resultados — valores válidos: `"price_asc"`, `"price_desc"`, `"name_asc"`,
`"relevance"` (nuevo). Cualquier otro valor responde `422`. Si se omite (`null`), no hay orden
garantizado entre llamadas — usa `sortBy` siempre que el orden le importe a la UI (p. ej. un
selector de "Ordenar por: Precio menor a mayor / mayor a menor / Nombre A-Z / Más vendidos").
`"relevance"` ordena por `salesCount` descendente (más vendidos primero, empate alfabético por
nombre) — como aquí no hay texto de búsqueda, es el proxy estándar de "orden por defecto" que
usan la mayoría de tiendas en línea; útil como el orden por defecto de una categoría o de una
vista "Destacados".

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
  "taxonomyUuid": null,
  "vehicleUuid": null,
  "inStock": false,
  "sortBy": "relevance"
}
```

Coincidencia por substring (contiene), sin distinguir mayúsculas/minúsculas, contra `sku` **o**
`name` en un solo campo de búsqueda. `departmentUuid`/`categoryUuid`/`taxonomyUuid`/`vehicleUuid`
son opcionales y funcionan igual que en `/v1/products` (ver esa sección para el detalle de cada
uno) — úsalos para combinar el cuadro de búsqueda con los filtros de departamento/categoría/
vehículo ya existentes. `inStock: true` restringe el resultado a productos con
`stock > 0` (por defecto `false`, no filtra por stock).

**`sortBy` (nuevo campo, opcional, default `"relevance"`)** — mismos cuatro valores que
`/v1/products` (`"relevance"`, `"price_asc"`, `"price_desc"`, `"name_asc"`), `422` si se manda
otro valor. Puedes omitirlo por completo y obtienes el comportamiento default de siempre. Con
`"relevance"` (el default): los resultados donde `sku` o `name` **empiezan con** el texto
buscado aparecen primero; dentro de ese mismo grupo (empieza-con vs. contiene-en-medio),
ordenan por `salesCount` descendente (más vendido primero) y por último por nombre — ya
paginado en ese orden, no es necesario ordenar nada del lado del frontend. Si tu UI ya tenía un
selector "Ordenar por" en resultados de búsqueda, ahora puedes mandar `price_asc`/`price_desc`/
`name_asc` igual que en `/v1/products` en vez de solo confiar en el orden default.

Respuesta `200` con la misma forma que `/v1/products`:

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
      "stock": 2.0,
      "salesCount": 0.0,
      "averageRating": null,
      "reviewsCount": 0
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

`404` si el `uuid` no existe, o si el producto fue descontinuado/ocultado del catálogo
(`isDeleted`/`isActive` — mismo filtro que `/v1/products`/`/v1/search`).

Respuesta `200` incluye todos los campos de `POST /v1/products` (`sicarUuid`, `sku`, `name`,
`descriptionDetails`, `imageUrl`, `price`, `stock`) más varios que solo trae el detalle:

```json
{
  "id": 40213,
  "sicarUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un",
  "sku": "PR2057",
  "additionalSkus": null,
  "name": "PORTAROLLO",
  "descriptionDetails": "Portarollo de acero inoxidable...",
  "imageUrl": null,
  "tags": ["oferta"],
  "additionalImages": null,
  "salesUnitUuid": "0b8b0848-3880-4085-b213-3b3d30c79429",
  "unitShortName": "PZA",
  "departmentUuid": "4aa3e82c-3ea2-4018-b8a7-12e727247cfa",
  "categoryUuid": "137bcaba-5aa2-4559-8545-2cab151d8369",
  "price": 8.62069,
  "stock": 2.0,
  "isBulk": false,
  "isActive": true,
  "isDeleted": false,
  "lastSyncId": "a1b2c3d4",
  "detailsUpdatedAt": "2026-07-27T10:15:00Z",
  "deletedAt": null,
  "attributes": [],
  "variantGroup": null
}
```

`tags`/`additionalImages`/`additionalSkus` pueden venir `null` en vez de un arreglo vacío si
Sicar X no tiene nada que reportar. `isActive`/`isDeleted` siempre vienen `true`/`false`
respectivamente en esta ruta (el `404` de arriba ya descarta cualquier otro caso) — no hace
falta revisarlos en el frontend, solo se incluyen porque son parte del modelo interno.
`id`/`lastSyncId` son identificadores internos de sincronización, no pensados para mostrarse
en la UI. Puede tardar un poco más la primera vez que se pide un producto (o si
`detailsUpdatedAt` tiene más de 24h) — internamente refresca `tags`/`additionalImages`/
`additionalSkus`/`descriptionDetails`/`salesUnitUuid`/`unitShortName` desde Sicar X antes de
responder. `unitShortName` (p. ej. `"PZA"`/`"MTR"`) es el nombre legible de la unidad de venta
resuelto a partir de `salesUnitUuid` — puede venir `null` si nunca se resolvió (fallback a
`"PZA"` en el checkout, ver `POST /v1/orders`); antes solo se resolvía efímeramente en cada
llamada a `/v1/orders`, ahora queda persistido aquí en el primer refresco de detalle.

**`attributes`/`variantGroup` (nuevo) — PIM propio, no viene de Sicar X.** Administrados desde
el panel admin (ver `ADMIN_INTEGRATION.md`, sección "Atributos de producto y grupos de
variantes") — esta es la única ruta del storefront que los expone; `POST /v1/products` y
`POST /v1/search` no los traen (payload de listado sin cambios, no hace falta actualizar esas
integraciones). Ambos empiezan vacíos/`null` para cualquier producto todavía sin clasificar, lo
cual es el caso de la inmensa mayoría del catálogo hoy — nunca un error.

`attributes` es un arreglo (`[]` si el producto no tiene ninguno guardado):

```json
"attributes": [
  { "attributeUuid": "8bdb99f9-9c96-4c37-a32c-1f000f38569b", "name": "Color", "slug": "color", "dataType": "ENUM", "unit": null, "value": "Rojo" },
  { "attributeUuid": "2380964d-7a04-4413-bee7-f5e9c56d7f56", "name": "Voltaje", "slug": "voltaje", "dataType": "NUMBER", "unit": "V", "value": 12.5 }
]
```

`dataType` (`"TEXT"`/`"NUMBER"`/`"BOOLEAN"`/`"ENUM"`) determina el tipo de `value` en JSON
(string/number/boolean respectivamente); `unit` es una unidad de display opcional (p. ej.
`"V"`, `"mm"`), `null` si no aplica.

`variantGroup` es `null` si el producto no pertenece a ningún grupo, o:

```json
"variantGroup": {
  "uuid": "b52bf1c5-873a-45f6-b341-930106d669ed",
  "name": "Portarollo acero inoxidable",
  "variantAttributeSlug": "color",
  "siblings": [
    { "uuid": "7Bqz2PPydY2HpTaM0sFuUANM8vo", "sku": "PR2058", "name": "PORTAROLLO ROJO", "imageUrl": null, "price": 8.62, "stock": 4.0, "value": "Azul" }
  ]
}
```

Un grupo de variantes vincula SKUs **distintos** en Sicar X (cada uno con su propio `sicarUuid`/
precio/stock) que son en realidad la misma pieza en presentaciones distintas (p. ej. color).
`siblings` son los demás productos del mismo grupo (nunca incluye al producto que estás
viendo) — cada uno con su propio `uuid`, usable directo en un segundo
`GET /v1/products/{uuid}` si el shopper cambia de variante, y `value` es el valor de
`variantAttributeSlug` para ESE sibling (p. ej. su color), pensado para pintar un selector de
variante (swatches, botones de talla) sin una llamada aparte por cada opción.
`variantAttributeSlug` puede venir `null` si el grupo no tiene un atributo distintivo
configurado — en ese caso no hay un valor estándar para etiquetar cada opción del selector,
usa `siblings[].name`/`sku` en su lugar.

### `GET /v1/products/best-sellers` — más vendidos (nuevo, para la página principal)

```http
GET /v1/products/best-sellers?limit=10
x-api-key: <api-key>
```

Pensado específicamente para una sección "Los más vendidos" en la página principal — a
diferencia de `POST /v1/products`/`POST /v1/search`, es `GET` con query params (no un body) y
**no pagina**: es un feed acotado de top-N, no un listado para hacer scroll infinito. Todos los
parámetros son opcionales:

| Query param | Tipo | Default | Descripción |
|---|---|---|---|
| `limit` | int (1-50) | `10` | Cuántos productos devolver |
| `departmentUuid` | string | — | Igual que en `/v1/products` |
| `categoryUuid` | string | — | Igual que en `/v1/products` |
| `taxonomyUuid` | string | — | Igual que en `/v1/products` (nodo del árbol de `GET /v1/taxonomy`) |
| `vehicleUuid` | string | — | Igual que en `/v1/products` (fitment resuelto de `GET /v1/vehicles`) |
| `inStock` | bool | `false` | Si `true`, solo productos con `stock > 0` |

Respuesta `200`:
```json
{
  "docs": [
    {
      "sicarUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un",
      "sku": "PR2057",
      "name": "PORTAROLLO",
      "descriptionDetails": null,
      "imageUrl": null,
      "price": 8.62069,
      "stock": 2.0,
      "salesCount": 14.0
    }
  ]
}
```

Sin `total` (a propósito — no es un listado paginado, así que un conteo total no aplica). Solo
incluye productos con `salesCount > 0`, ya ordenados de mayor a menor — **puede venir `docs: []`**
si todavía no hay ningún pedido pagado en el sistema (o ninguno que combine con los filtros
usados), no es un error; en ese caso, la UI debería simplemente ocultar la sección en vez de
mostrarla vacía. `salesCount` se cuenta desde pedidos que llegaron a `status: "PAID"` (ver
`POST /v1/orders` más abajo) y se descuenta de vuelta si ese mismo pedido se cancela después de
haber sido pagado — así que este feed puede cambiar de un día a otro, no lo caches de forma
agresiva.

### `GET /v1/taxonomy` — árbol de categorías (para filtros)

```http
GET /v1/taxonomy
x-api-key: <api-key>
```

Respuesta `200`:
```json
{
  "categories": [
    {
      "uuid": "4aa3e82c-3ea2-4018-b8a7-12e727247cfa",
      "name": "FERRETERÍA",
      "slug": "ferreteria",
      "children": [
        {
          "uuid": "137bcaba-5aa2-4559-8545-2cab151d8369",
          "name": "VIDAL",
          "slug": "vidal",
          "children": []
        }
      ]
    }
  ]
}
```

Árbol auto-referenciado de profundidad arbitraria (no un `departments`/`categories` de 2
niveles fijos) — cada nodo tiene una sola lista de `children`, que puede estar vacía o
anidarse tan profundo como se haya armado desde el admin. Los nodos raíz (sin padre) son los
elementos de primer nivel en `categories`; `slug` viene derivado del `name` y es único, útil
para URLs bonitas del lado del frontend si se necesitan. Usa el `uuid` de cualquier nodo
(raíz o anidado) como `taxonomyUuid` en `POST /v1/products`/`POST /v1/search` — filtrar por
un nodo padre también devuelve productos etiquetados solo en un descendiente.

### `GET /v1/vehicles*` — selector de vehículo ("¿qué le queda a mi carro?")

Cinco endpoints públicos para armar un selector en cascada (tipo → marca → año → modelo →
motor), como en los sitios de refacciones. El **tipo** (`"AUTOMOTIVE"`/`"MOTORCYCLE"`) no
necesita ninguna llamada — son solo 2 valores fijos que el frontend ya conoce; empieza la
cascada real en marcas. **Año va antes que modelo** (no es el orden más común en sitios de
refacciones, pero es el que se decidió aquí) — cada paso se filtra con lo ya elegido en los
pasos anteriores:

```http
GET /v1/vehicles/makes?vehicleType=AUTOMOTIVE
x-api-key: <api-key>
```
```json
{ "docs": ["Chevrolet", "Ford", "Honda", "..."] }
```

```http
GET /v1/vehicles/years?vehicleType=AUTOMOTIVE&make=Chevrolet
x-api-key: <api-key>
```
```json
{ "docs": [2016, 2015, 2014, 2013, 2012, 2011, 2010, 2009, 2008] }
```
Años en orden descendente (más reciente primero). Vienen de expandir el rango `yearStart`–
`yearEnd` de cada fitment de esa marca — un fitment sin `yearEnd` (todavía en producción)
cuenta como vigente hasta el año calendario actual.

```http
GET /v1/vehicles/models?vehicleType=AUTOMOTIVE&make=Chevrolet&year=2012
x-api-key: <api-key>
```
```json
{ "docs": ["Aveo", "Spark", "..."] }
```
Modelos de esa marca cuyo rango `yearStart`–`yearEnd` incluye el año elegido.

```http
GET /v1/vehicles/engines?vehicleType=AUTOMOTIVE&make=Chevrolet&model=Aveo&year=2012
x-api-key: <api-key>
```
```json
{ "docs": ["L4 1.6L"] }
```
Si `docs` viene vacío, no hay dato de motor para esa combinación — **salta este paso** y
sigue directo a la resolución final sin mandar `engine`.

```http
GET /v1/vehicles?vehicleType=AUTOMOTIVE&make=Chevrolet&model=Aveo&year=2012&engine=L4%201.6L
x-api-key: <api-key>
```
```json
{
  "total": 1,
  "docs": [
    {
      "uuid": "3f9c1b2a-....",
      "vehicleType": "AUTOMOTIVE",
      "make": "Chevrolet",
      "model": "Aveo",
      "yearStart": 2008,
      "yearEnd": 2016,
      "engine": "L4 1.6L",
      "updatedAt": "2026-08-01T10:00:00Z"
    }
  ]
}
```
Este último paso es el que resuelve la selección a un fitment real — usa su `uuid` como
`vehicleUuid` en `POST /v1/products`/`POST /v1/search` (ver esas secciones arriba) para
mostrar solo productos compatibles. Todos los parámetros de este endpoint son opcionales y
combinables (podés omitir `year`/`engine` si esos pasos no aplican); si guardás la
selección del shopper para reusarla después (p. ej. "mi garage"), guarda el objeto completo
que devuelve este endpoint, no solo el `uuid` — no existe un `GET /v1/vehicles/{uuid}` para
recuperarlo por separado.

### `GET`/`PUT`/`PATCH`/`DELETE /v1/cart*` — carrito persistente

Carrito guardado del lado del servidor (sobrevive a cerrar el navegador, o a cambiar de
dispositivo si hay login) — **no reemplaza** el carrito en memoria del frontend, es una capa
opcional de persistencia. Funciona tanto sin login (carrito anónimo) como con login (carrito de
cuenta). Solo guarda `{uuid, quantity}` por producto — precio, nombre, stock e imagen **siempre**
se leen en vivo del catálogo local al consultarlo, nunca se guardan como estaban al agregar el
producto.

**Sin login** — no mandes `X-Client-Token` ni `Authorization`, y **siempre** manda
`credentials: "include"` (o `withCredentials: true`) en el `fetch`/`axios` — es lo único que hace
falta para que la identidad anónima funcione, la cookie `charly_cart_token` la maneja el navegador
solo (ver el punto 3 de "Dos capas de autenticación" arriba). No hay ningún header que armar a mano
para esto:

```http
PUT /v1/cart
x-api-key: <api-key>
Content-Type: application/json

{
  "items": [
    { "uuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "quantity": 2 }
  ]
}
```

`PUT` **reemplaza el carrito completo** — no es agregar/quitar un producto, es mandar el estado
completo deseado cada vez (el frontend ya arma esta lista en memoria de todas formas). Si no hay
carrito anónimo resuelto todavía (primera visita, o cookie no reconocida/vencida), se crea uno
nuevo silenciosamente (no es un error) y el navegador guarda la cookie nueva automáticamente a
partir de la respuesta — no hay nada que leer o guardar del lado del frontend para esto.

Respuesta `200` (misma forma para `GET`, `PUT`, `PATCH /v1/cart/items` y `POST /v1/cart/merge`):
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
  "updatedAt": "2026-07-18T14:36:17Z",
  "couponCode": null,
  "couponValid": false,
  "couponInvalidReason": null,
  "discountAmount": 0.0,
  "total": 17.24
}
```

`subtotal` (no confundir con `total`, ver más abajo — ese nombre significaba "cantidad de filas" en
`/v1/products`/`/v1/search` y en el historial de pedidos, pero aquí sí es un monto) es la suma de
`lineTotal` **solo de los productos `available: true`**. Un producto puede aparecer con
`available: false` (y sin `sku`/`name`/`price`/`stock`/`lineTotal`, todos `null`) si ya no existe
en el catálogo local o fue desactivado/eliminado — **no desaparece de `items`**, muéstralo igual
pero indica que ya no está disponible (p. ej. "Ya no disponible, quítalo del carrito"); no cuenta
en `subtotal` pero sí en `totalQuantity`. `cartToken` sigue viniendo en el body (no `null`) cuando
el carrito es anónimo — guárdalo solo en memoria, es lo que se manda como `cartToken` al
registrarse/iniciar sesión o a `POST /v1/cart/merge` (ver arriba y abajo); ya no hace falta
reenviarlo en ningún header.

`couponCode`/`couponValid`/`couponInvalidReason`/`discountAmount`/`total` son el preview en vivo
del cupón aplicado (ver `POST`/`DELETE /v1/cart/coupon` más abajo) — `total` es el monto real a
mostrar como "a pagar" (`subtotal - discountAmount`, nunca negativo; sin cupón aplicado,
`total == subtotal`). Si no hay `couponCode` aplicado, todos vienen en su valor por defecto
(`null`/`false`/`0.0`). Si `couponCode` está seteado pero el cupón ya no aplica (expiró, no
alcanza el mínimo de compra, etc.), `couponValid` es `false` y `couponInvalidReason` explica por
qué en texto legible — el `GET` **nunca falla** por esto, muestra el mensaje y ofrece quitarlo.
**Este preview no es autoritativo**: el descuento real se vuelve a calcular y bloquear en
`POST /v1/orders` (ver más abajo) — es posible, aunque raro, que un cupón válido en el carrito ya
no lo sea al momento de pagar (p. ej. alguien más agotó el cupo justo antes), en cuyo caso
`POST /v1/orders` responde `409`.

`GET /v1/cart` sin login y sin cookie reconocida responde un carrito vacío (`items: []`) sin crear
nada. `DELETE /v1/cart` vacía el carrito resuelto y limpia la cookie del carrito anónimo si
aplicaba, responde `204` siempre (incluso si no había nada que borrar).

**Con login** — igual, pero manda `X-Client-Token`; si está presente y es válido, siempre gana
sobre la cookie del carrito anónimo (si hubiera una) y el carrito es el de la cuenta, no el
anónimo. `cartToken` en la respuesta viene `null` en este caso (no hace falta, ya tienes
`X-Client-Token`).

Errores esperables:
- `401` — se mandó `X-Client-Token` pero es inválido/expiró (a diferencia de una cookie de
  carrito anónimo no reconocida, que nunca es error — ver arriba)
- `422` — algún `quantity`/`delta` tiene un formato inválido, o algún `uuid` de producto no es
  válido (en `PUT`, rechaza toda la petición, no solo esa línea)

### `PATCH /v1/cart/items` — incrementar o decrementar un solo producto

Pensado para un botón "agregar al carrito" o un stepper +/- sin tener que mandar el carrito
completo cada vez (a diferencia de `PUT`, que sí lo requiere). Misma identidad/cookie que
`GET`/`PUT`/`DELETE` de arriba.

```http
PATCH /v1/cart/items
x-api-key: <api-key>
Content-Type: application/json

{
  "productUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un",
  "delta": 1
}
```

`delta` es la cantidad a sumar (positivo, para agregar/incrementar) o restar (negativo, para
decrementar) — no la cantidad final. Si el producto no estaba en el carrito y `delta` es positivo,
se agrega como línea nueva con esa cantidad. Si la cantidad resultante queda en `0` o menos, la
línea se **elimina** del carrito (no se queda en `0`, desaparece). Si se manda un `delta` negativo
o cero para un producto que no está en el carrito, no pasa nada (`200` con el carrito sin cambios,
no es un error). Responde el mismo shape que `GET`/`PUT /v1/cart` arriba. Igual que `PUT`, si no
había carrito anónimo resuelto y `delta` es positivo, crea uno nuevo silenciosamente.

### `POST`/`DELETE /v1/cart/coupon` — aplicar o quitar un cupón del carrito

Misma identidad/cookie que el resto de `/v1/cart*` (sin login: cookie anónima + `credentials:
"include"`; con login: `X-Client-Token`).

```http
POST /v1/cart/coupon
x-api-key: <api-key>
Content-Type: application/json

{ "code": "WELCOME10" }
```

Guarda el código en el carrito (para que `GET /v1/cart` lo siga mostrando en visitas
posteriores) y responde el mismo shape de `CartResponse` de arriba, ya con el descuento
calculado. `400` si no hay carrito resuelto o está vacío (no tiene sentido aplicar un cupón a
nada). `404` si el código no existe o está inactivo (mismo mensaje genérico para ambos casos,
a propósito — no revela si un código desactivado existe). `409` si el código existe pero no
aplica ahora mismo (expirado, no alcanza el mínimo de compra, ya alcanzó su límite de usos,
etc.) — el detalle del error trae el motivo en texto legible.

```http
DELETE /v1/cart/coupon
x-api-key: <api-key>
```

Quita el cupón aplicado. `200` con el carrito sin descuento (no `204` — a diferencia de
`DELETE /v1/cart`, este endpoint sí devuelve el carrito actualizado). No-op si no había carrito
o no tenía cupón aplicado.

**Importante**: aplicar el cupón aquí es solo un preview — no reserva el uso ni garantiza que
siga disponible al pagar. El código real que cuenta se manda de nuevo en `couponCode` al llamar
`POST /v1/orders` (ver más abajo), que es donde se valida y bloquea de verdad.

### `POST /v1/cart/merge` — fusionar el carrito anónimo a la cuenta

**Ya casi nunca hace falta llamarlo aparte** — mandar `cartToken` directo en el body de
`POST /v1/auth/login`/`POST /v1/auth/register` (ver esas secciones arriba) hace la misma fusión en
la misma llamada. Este endpoint sigue existiendo para el caso en que el usuario **ya está
logueado** (en otra pestaña, u otro dispositivo) y arma un carrito anónimo nuevo — ahí sí hace
falta un request aparte para fusionarlo, ya que el login no vuelve a ocurrir. **Usa
`Authorization`** para el token de cuenta aquí, no `X-Client-Token` (ver la nota en "Dos capas de
autenticación" arriba) — igual que `/v1/auth/me/addresses`.

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
Responde `200` con el mismo shape de arriba (`cartToken: null`, ya es el carrito de la cuenta) y,
al fusionar con éxito, el backend limpia la cookie del carrito anónimo por su cuenta — no hay que
borrar nada del lado del frontend. `404` si `cartToken` no corresponde a un carrito anónimo
existente (ya fue fusionado antes, o nunca existió) — a diferencia de mandar `cartToken` en
`/v1/auth/login`/`/v1/auth/register` (que ignora un token inválido sin error), aquí sí es un
`404` explícito porque es una acción deliberada del usuario ya logueado, no un intento tolerante
como el de login.

### `POST /v1/orders` — reservar pedido (todavía no cobra)

Contrato mínimo: solo el carrito y los datos de entrega. **Todo lo demás (precios, sku, totales)
lo calcula el backend a partir del catálogo local — sin ninguna llamada en vivo a Sicar X.**
`X-Client-Token` es **opcional** (ver punto 2 de "Dos capas de autenticación" arriba) desde
2026-08-11 — mándalo para un pedido de cuenta, o omítelo por completo para checkout de
invitado (contrato distinto para `deliveryInfo`/`contactInfo.email`, ver "Checkout de
invitado" más abajo).

```http
POST /v1/orders
x-api-key: <api-key>
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

`deliveryType` acepta `"PICKUP"` (recoger en tienda) o `"DELIVERYMAN"` (entrega a domicilio) —
cualquier otro valor responde `422`. `contactInfo.email` es opcional **a nivel de schema**,
pero si se envía debe ser un correo válido (también `422` si no lo es) — **excepto** que ahora
un string vacío (`""`, lo típico de un campo opcional sin tocar en un formulario) se trata como
si no se hubiera mandado, en vez de rechazarse. **Para checkout de invitado (sin
`X-Client-Token`) es obligatorio en la práctica** — `400` si falta — ver "Checkout de
invitado" más abajo.

`contactInfo.phone` debe tener **exactamente 10 dígitos** tras normalizar — Sicar X no quiere
más, y desde **2026-08-13** tampoco menos. El backend limpia el valor antes de validar (quita
espacios, guiones, paréntesis, `+52`, etc., y si aun así sobran dígitos por un prefijo de país/
larga distancia se queda con los últimos 10), así que un input con máscara
(`"315-123-4567"`, `"+52 315 123 4567"`) sigue sin reventar con `422` — pero sigue siendo buena
idea mandar solo los 10 dígitos si el formulario ya los tiene separados. **Incidente (2026-07-30)**:
antes de ese cambio, cualquier teléfono con formato o un email vacío `""` producía un `422` genérico
en `POST /v1/orders` sin explicación visible del lado del frontend — si ves checkouts fallando así
con una versión más vieja de este backend, esta es la causa más probable.

**Nuevo (2026-08-13) — un teléfono vacío/incompleto ahora sí es `422`, antes pasaba en
silencio.** Antes de este cambio, un `phone` vacío (`""`) o con menos de 10 dígitos tras la
limpieza de arriba (p. ej. un número de 9 dígitos por un typo, o un campo que el formulario
mandó en blanco) pasaba la validación sin error — el problema solo aparecía después, si un
admin intentaba generar una guía de envío con envia.com y el teléfono llegaba vacío/inválido
a su API. Ahora `POST /v1/orders` responde `422` en el momento del checkout si `phone` no
tiene exactamente 10 dígitos tras normalizar — si tu formulario no valida ya un teléfono
completo antes de enviarlo, un usuario con un número incompleto ahora ve el error aquí, en
checkout, en vez de que el pedido se cree con un dato malo.

**`Idempotency-Key` (header, opcional, recomendado).** Un reintento de red o un doble-click en
"pagar" puede reenviar el mismo `POST /v1/orders` — sin este header, cada intento crea una orden
local nueva (y descuenta el catálogo local otra vez). Si se envía, genera un UUID una sola vez por click en el botón de checkout y
reenvía **exactamente el mismo valor** en cualquier reintento automático de esa misma solicitud
(no uno nuevo por reintento). Si la clave ya se usó antes para este cliente:
- Si la orden original ya terminó de crearse, se devuelve esa misma orden (mismo `200`, mismos
  datos) en vez de crear una segunda.
- Si la solicitud original sigue en proceso, responde `409` — reintenta en unos segundos con la
  misma clave.

Es opcional y retrocompatible: si no se envía, el comportamiento es igual que antes de este
header existir.

Cuatro campos opcionales más a nivel raíz — normalmente no hace falta mandarlos, cada uno tiene
su propio valor por defecto si se omiten (o se mandan como `null`):

- `contentId` — si se omite, genera uno nuevo (`uuid4`).
- `branchId` — si se omite, `151456`.
- `priceListUuid` — si se omite, el de configuración del servidor.
- `wholesalePrices` — `false` por defecto; se guarda como metadata del pedido, no cambia qué
  precio local se cobra.

En la práctica casi nunca hace falta enviarlos explícitamente.

**`couponCode` (opcional, solo pedidos de cuenta)** — no disponible para checkout de
invitado (`400` si se envía sin `X-Client-Token`, ver "Checkout de invitado" más abajo).
Reenvía aquí el mismo código que se aplicó en
`POST /v1/cart/coupon` (ver esa sección arriba) si el shopper tiene uno aplicado; **el
descuento del carrito es solo un preview, este campo es lo que realmente se valida y
bloquea**. `409` si el código ya no aplica al momento de pagar (expiró, alguien más agotó
el cupo, ya no alcanza el mínimo con el carrito final, etc.) o `404` si no existe/está
inactivo — en cualquiera de los dos casos, ningún pedido se crea; muestra el error y ofrece
quitar el cupón o reintentar. Omitir el campo (o mandar `null`) es exactamente igual que no
tener cupón.

Para entrega a domicilio **en un pedido de cuenta**, manda `addressUuid` (el `uuid` de una
dirección ya guardada — ver `POST /v1/auth/me/addresses` arriba) en vez de una dirección
escrita a mano en cada pedido. (Para invitado, `addressUuid` está prohibido — usa `address`
inline en su lugar, ver "Checkout de invitado" más abajo.)

```json
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
    "deliveryType": "DELIVERYMAN",
    "addressUuid": "51cbf02f-cf83-470e-9313-c586d816c9c0"
  }
}
```

`addressUuid` es **obligatorio** cuando `deliveryType` es `"DELIVERYMAN"` y **no debe enviarse**
cuando es `"PICKUP"` — `422` en cualquiera de los dos casos si no se cumple. El backend resuelve
la dirección del lado del servidor (no hace falta mandar calle/ciudad/etc. en el body del
pedido). `404` si `addressUuid` no existe o no pertenece a la cuenta autenticada. `400` si la
dirección existe pero le faltan campos necesarios para la entrega (`street`/`city`/`county`/
`state`/`zipCode`/`extNumber`/`neighborhood`) — revisa que la dirección guardada esté completa antes de
ofrecerla como opción de entrega. El monto a cobrar (`amount`, y lo que después se cobra en
`POST /v1/orders/{id}/pay`) **no incluye ningún costo de envío** todavía, para ningún tipo de
entrega — sigue siendo solo el total de productos.

Respuesta `201` (creación real; ver la nota de idempotencia más abajo para cuándo viene
`200` en vez de `201`):
```json
{
  "id": "d65b89dc-9690-40b3-8dfb-aa2cdde18cc0",
  "serieFolio": null,
  "date": null,
  "status": "TO_PAY",
  "orderUuid": "f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6",
  "preferenceId": "123456789-abcdef01-2345-6789-abcd-ef0123456789",
  "amount": 129.99,
  "total": 129.99,
  "discountAmount": 0.0
}
```

`discountAmount` es el monto descontado por el cupón aplicado (`0.0` si no se usó ninguno) —
`amount`/`total` **ya vienen con el descuento restado**, es el monto real a cobrar/mostrar en
el Payment Brick tal cual, no hace falta restar `discountAmount` del lado del frontend.

**Nuevo (2026-08-13) — `total` es el nombre recomendado, `amount` queda como alias
histórico**: ambos son exactamente el mismo valor (`total` simplemente se agregó al lado de
`amount`, nada se quitó). Es el mismo cambio que `GET /v1/auth/me/orders/{orderUuid}` ya
tiene abajo (`OrderPublic.total`) — usa `total` en integraciones nuevas para que el mismo
campo se llame igual en ambas respuestas; `amount` se sigue mandando y se seguirá mandando,
no hay fecha de retiro planeada.

**Importante (2026-07-31) — `id`/`serieFolio`/`date` cambiaron de significado**, aunque
siguen presentes con los mismos nombres:
- `id` ya **no** es un identificador emitido por Sicar X — se genera localmente en este
  backend. Sigue siendo un string único que debes guardar y usar tal cual como `{order_id}`
  en `POST /v1/orders/{order_id}/pay` y `/cancel`, exactamente igual que antes — pero si
  tenías alguna validación de formato (longitud, solo-hexadecimal, etc.) asumiendo el formato
  viejo de Sicar X, quítala: ahora es un UUID v4 estándar (con guiones).
- `serieFolio` y `date` ahora **siempre vienen `null`** — ya no existe ningún documento en
  Sicar X del que puedan salir. Si los mostrabas en algún lado (p. ej. un folio en la
  confirmación de pedido), esa UI necesita quitar o reemplazar esos campos.

**Esta llamada ya NO cobra ni deja el pedido pagado** — solo lo reserva localmente (`status`
viene `"TO_PAY"`) y prepara el cobro con Mercado Pago. **Guarda `id`** — se usa como
`{order_id}` tanto en `POST /v1/orders/{order_id}/pay` (siguiente paso) como en
`POST /v1/orders/{order_id}/cancel`. **Guarda `orderUuid`** — identificador local del pedido,
usado en `GET /v1/auth/me/orders/{orderUuid}`. `preferenceId` puede venir `null` si Mercado
Pago no respondió al crear la preferencia (no es fatal — el pedido igual se creó y sigue
soportando tarjeta/OXXO, solo no tendrá la opción de pagar con cuenta/Wallet de Mercado Pago).
`amount`/`total` es el total autoritativo calculado por el backend — úsalo en
`initialization.amount` del Payment Brick, no un total calculado en el frontend.

**Retry con `Idempotency-Key`**: si reenvías la misma request con el mismo header
`Idempotency-Key` (p. ej. un reintento de red del mismo submit), la respuesta trae el mismo
pedido ya creado la primera vez — pero con status `200`, no `201`, ya que no se creó nada
nuevo. No trates un `200` aquí como un error; el body tiene exactamente el mismo shape que
el `201` original.

Errores esperables:
- `401` — se mandó `X-Client-Token` pero es inválido/expiró (llama de nuevo a
  `/v1/auth/login`) — **omitirlo por completo ya no es un `401`**, ver "Checkout de
  invitado" abajo
- `400` — carrito vacío, datos de entrega inválidos, (para `DELIVERYMAN`) la dirección
  seleccionada existe pero le faltan campos necesarios para la entrega, o (checkout de
  invitado) falta `contactInfo.email`, se mandó `addressUuid`, o se mandó `couponCode`
- `404` — (para `DELIVERYMAN` de cuenta) `addressUuid` no existe o no pertenece a la cuenta
  autenticada, **o** `couponCode` no existe/está inactivo (mismo mensaje genérico para ambos
  casos)
- `409` — uno o más productos sin disponibilidad suficiente (sin stock, o precio local
  inconsistente — ver nota abajo), **o** el `couponCode` enviado ya no aplica (expiró, no
  alcanza el mínimo, alcanzó su límite de usos, etc.) — validación 100% local contra Postgres,
  sin ninguna llamada en vivo a Sicar X en este paso.

### Checkout de invitado (2026-08-11) — comprar sin cuenta

Omite `X-Client-Token` por completo en `POST /v1/orders` (y en `/pay`/`/cancel`/`DELETE`
después) para un pedido de invitado. Tres diferencias respecto al contrato de cuenta de
arriba:

1. **`contactInfo.email` es obligatorio** — es la única identidad del invitado; `400` si
   falta. Úsalo también para mostrarle al comprador un resumen/confirmación en pantalla, ya
   que no tiene "Mi cuenta" donde consultarlo después (salvo que luego se registre, ver más
   abajo).
2. **`couponCode` no se acepta** — `400` si se envía. Los cupones requieren cuenta.
3. **Para `DELIVERYMAN`, manda `address` en vez de `addressUuid`** — un objeto con la
   dirección completa escrita a mano (no hay address book sin cuenta):

```json
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
    "deliveryType": "DELIVERYMAN",
    "address": {
      "street": "Av Siempre Viva",
      "extNumber": "123",
      "intNumber": null,
      "neighborhood": "Centro",
      "city": "Guadalajara",
      "county": "Guadalajara",
      "state": "Jalisco",
      "zipCode": "44100",
      "references": null,
      "latitude": null,
      "longitude": null
    }
  }
}
```

`street`/`extNumber`/`neighborhood`/`city`/`county`/`state`/`zipCode` son obligatorios dentro
de `address` (`422` si falta alguno) — es el mismo conjunto de campos que ya se exige en
runtime para una dirección guardada de cuenta, solo que aquí se valida a nivel de schema.
`intNumber`/`references`/`latitude`/`longitude` son opcionales. Para `DELIVERYMAN`, manda
exactamente uno de `addressUuid`/`address` — ambos o ninguno responde `422`; el que no
corresponde a tu caso (`addressUuid` sin sesión, `address` con sesión) responde `400`.

La respuesta `200` es idéntica a la de un pedido de cuenta (mismo shape, ver arriba) — sigue
trayendo `id`/`orderUuid`. **Guárdalos**: como no hay `X-Client-Token`, cualquiera de los dos
(`id` o `orderUuid`, indistinto — usa el mismo campo que ya usas para pedidos de cuenta) sirve
como prueba de pertenencia para `POST /v1/orders/{order_id}/pay`, `/cancel`,
`DELETE /v1/orders/{order_id}` y `GET /v1/orders/guest/{order_id}` (ver esa sección más
abajo) — **sin ellos, el pedido es irrecuperable** (no hay login con el que reconstruir el
acceso), así que persístelos en el navegador (p. ej. `localStorage`, o en la URL de la página
de confirmación) antes de navegar fuera del flujo de checkout.

**Si el invitado se registra o inicia sesión después con el mismo correo usado en el
pedido, y verifica su correo** (clic en el enlace de verificación que llega por
`verification-requested`, o login con Google si Google ya reportó el correo verificado), el
pedido de invitado se vincula automáticamente a esa cuenta y aparece en
`GET /v1/auth/me/orders` — no hace falta ningún paso extra del lado del frontend para esto,
ocurre solo. **Ojo**: la vinculación pasa solo al verificarse, no al simplemente
registrarse/iniciar sesión — si el frontend muestra "revisa tu pedido en Mi cuenta" justo
después de un registro, aclara que el pedido de invitado aparecerá ahí una vez confirmado el
correo, no antes. Una vez vinculado, `GET /v1/orders/guest/{order_id}` deja de funcionar para
ese pedido (`404`) — a partir de ahí, usa `GET /v1/auth/me/orders/{orderUuid}` con sesión.

### `GET /v1/orders/guest/{order_id}` — consultar estado de un pedido de invitado

Sin `X-Client-Token` (no aplica — es la ruta de invitado). `{order_id}` es el mismo `id` u
`orderUuid` que devolvió `POST /v1/orders`. Responde el mismo shape que un item de
`GET /v1/auth/me/orders` (`OrderPublic`) — útil para una página de "sigue tu pedido" sin
necesidad de cuenta.

```http
GET /v1/orders/guest/d65b89dc-9690-40b3-8dfb-aa2cdde18cc0
x-api-key: <api-key>
```

`404` si el pedido no existe, o si ya fue vinculado a una cuenta (ver arriba) — en ese caso
no es un error real, solo indica que hay que usar la ruta autenticada en su lugar.

## Reseñas y calificaciones de productos

Cualquier cliente autenticado puede reseñar cualquier producto (no se exige haberlo
comprado) — `isVerifiedPurchase` es solo un badge informativo, calculado una sola vez al
crear la reseña a partir de las órdenes `PAID` del cliente. Una reseña por cliente por
producto: para volver a opinar, edita la existente (`PATCH`) en vez de crear otra (`409`
si lo intentas). Las respuestas siempre usan camelCase, igual que el resto de la API.

### `GET /v1/products/{productUuid}/reviews` — listar reseñas de un producto

No requiere sesión de cliente, solo `x-api-key`. Paginado (`limit`/`offset`, mismo
estilo que `/v1/products`), ordenable con `sortBy` (`newest` por defecto,
`highest_rating`, `lowest_rating` o `most_helpful`). Solo devuelve reseñas visibles
(no ocultas por un admin).

```http
GET /v1/products/3Cny4OOxdX1GoSzL9rEsTZNL7un/reviews?limit=20&offset=0&sortBy=newest
x-api-key: <api-key>
```

```json
{
  "total": 2,
  "docs": [
    {
      "uuid": "c2a8ff9a-63c4-43d2-9ce3-57aebc0e8be6",
      "productUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un",
      "clientName": "Juan Pérez",
      "rating": 4,
      "comment": "Buen producto, cumple lo esperado.",
      "isVerifiedPurchase": true,
      "isHidden": false,
      "helpfulCount": 3,
      "adminReply": null,
      "adminReplyAt": null,
      "createdAt": "2026-08-10T14:58:23.190116Z",
      "updatedAt": null
    }
  ],
  "averageRating": 3.5,
  "reviewsCount": 2,
  "ratingBreakdown": { "1": 0, "2": 0, "3": 0, "4": 1, "5": 1 }
}
```

`averageRating`/`reviewsCount` son el mismo par cacheado que ya viene en `stock`/
`salesCount` de `POST /v1/products` y `POST /v1/search` (ver más abajo) — no hace falta
volver a calcularlos en el frontend. `averageRating` es `null` si el producto todavía no
tiene ninguna reseña visible.

### `POST /v1/products/{productUuid}/reviews` — crear una reseña

Requiere `Authorization: <token>` (cuenta de cliente, igual que `/v1/auth/me`).

```http
POST /v1/products/3Cny4OOxdX1GoSzL9rEsTZNL7un/reviews
x-api-key: <api-key>
Authorization: <token>
Content-Type: application/json

{ "rating": 4, "comment": "Buen producto, cumple lo esperado." }
```

`rating` es obligatorio (entero 1-5, `422` fuera de rango). `comment` es opcional.
Responde `201` con el mismo shape que un item de `docs` arriba. Errores esperables:
- `404` — el producto no existe o no está disponible (eliminado/inactivo)
- `409` — ya reseñaste este producto (usa `PATCH` sobre la reseña existente)

### `PATCH /v1/reviews/{reviewUuid}` / `DELETE /v1/reviews/{reviewUuid}` — editar/eliminar la propia reseña

Requieren `Authorization`. Todos los campos de `PATCH` son opcionales (`rating`,
`comment`) — solo se cambia lo que se envíe. `404` si la reseña no existe o no
pertenece a la cuenta autenticada (no se distingue de "no existe", mismo patrón que el
resto de recursos propios de esta API). `DELETE` responde `204`.

### `PUT` / `DELETE /v1/reviews/{reviewUuid}/helpful` — marcar/quitar "útil"

Requieren `Authorization`. Ambos son idempotentes — marcar dos veces o quitar sin haber
marcado antes no cambian nada ni fallan. `404` si la reseña no existe o está oculta.

```json
{ "reviewUuid": "c2a8ff9a-63c4-43d2-9ce3-57aebc0e8be6", "helpfulCount": 4, "markedByMe": true }
```

### `GET /v1/auth/me/reviews` — historial de reseñas propias

Requiere `Authorization`. Paginado, mismo shape que `GET /v1/products/{uuid}/reviews`
pero sin `averageRating`/`ratingBreakdown` reales (siempre `null`/en cero — no aplican a
una lista mixta de productos). A diferencia de la vista pública, **sí incluye** las
reseñas que un admin haya ocultado (`isHidden: true`) — solo el propio autor las sigue
viendo aquí.

## Wishlist / lista de favoritos (nuevo, 2026-08-19)

Solo para cuentas de cliente — **no existe wishlist anónima/de invitado**, a diferencia del
carrito. Todas las rutas de esta sección van bajo `/v1/wishlist`, requieren `x-api-key` y
requieren `Authorization: <token>` (la misma cabecera de `/v1/auth/me` y de reseñas —
**no** `X-Client-Token`; esa cabecera es exclusiva de `/v1/orders`/`/v1/orders/*/cancel`,
ver "Dos capas de autenticación" arriba). Un `Authorization` ausente o inválido responde
`401` en todas ellas.

Cada cuenta tiene una lista fija llamada **"Favoritos"** (`isDefault: true`), pensada como
el destino del típico ícono de corazón en una tarjeta de producto, más cualquier cantidad
de listas adicionales con nombre propio que el cliente cree explícitamente (p. ej. para
armar una lista de regalo). El mismo producto puede estar guardado en varias listas a la
vez, pero no dos veces en la misma lista — guardarlo de nuevo no falla, simplemente no
hace nada (idempotente), igual que quitar un producto que ya no estaba.

La lista "Favoritos" **no existe hasta el primer guardado** — `GET /v1/wishlist/favorites`
antes de guardar algo responde una lista vacía (`{"total": 0, "docs": []}`), nunca `404`;
se crea sola en el primer `PUT /v1/wishlist/favorites/{productUuid}`. No hace falta
crearla manualmente ni comprobar que exista antes de usarla.

### Atajo de corazón — `GET`/`PUT`/`DELETE /v1/wishlist/favorites{/productUuid}`

Pensado para un ícono de corazón en una card de producto, sin que el frontend necesite
conocer el `uuid` de ninguna lista.

```http
PUT /v1/wishlist/favorites/3Cny4OOxdX1GoSzL9rEsTZNL7un
x-api-key: <api-key>
Authorization: <token>
```

Responde `200` vacío. `DELETE` sobre la misma ruta responde `204` vacío. Ambos son
idempotentes: marcar dos veces, o desmarcar algo que nunca se guardó, no son errores.
`404` si `productUuid` no corresponde a un producto existente/disponible (eliminado o
inactivo en el catálogo).

```http
GET /v1/wishlist/favorites?limit=60&offset=0
x-api-key: <api-key>
Authorization: <token>
```

```json
{
  "total": 2,
  "docs": [
    {
      "productUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un",
      "addedAt": "2026-08-19T10:12:04.501Z",
      "available": true,
      "product": { "uuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "sku": "...", "name": "...", "price": 149.0, "stock": 12, "...": "..." }
    },
    {
      "productUuid": "9Zab2LLpqM4RtVxK7cWnUABw88f",
      "addedAt": "2026-08-15T08:40:11.002Z",
      "available": false,
      "product": null
    }
  ]
}
```

`limit`/`offset` funcionan igual que en `POST /v1/products` (`limit` 1-200, default 60).
`product` trae el mismo shape que `ProductBasic` en `POST /v1/products`/`POST /v1/search`
(ver referencia arriba) — úsalo para pintar la card directamente sin una llamada aparte.
**`available: false` no significa que la fila se borró** — el producto fue desactivado o
eliminado del catálogo desde que se guardó, pero la wishlist lo conserva (fue una acción
explícita del cliente); en ese caso `product` viene `null` y conviene mostrar la línea
como "ya no disponible" en vez de ocultarla silenciosamente.

### Listas con nombre — `GET`/`POST /v1/wishlist/collections`

```http
GET /v1/wishlist/collections
x-api-key: <api-key>
Authorization: <token>
```

```json
[
  { "uuid": "a1b2...", "name": "Favoritos", "isDefault": true, "itemCount": 2, "createdAt": "2026-08-15T08:40:00.000Z" },
  { "uuid": "c3d4...", "name": "Cumpleaños de mi papá", "isDefault": false, "itemCount": 1, "createdAt": "2026-08-19T09:00:00.000Z" }
]
```

Sin paginar (un cliente no tiene cientos de listas) — si "Favoritos" todavía no existe
(nunca se guardó nada), simplemente no aparece en el arreglo. `POST` crea una lista nueva:

```http
POST /v1/wishlist/collections
x-api-key: <api-key>
Authorization: <token>
Content-Type: application/json

{ "name": "Cumpleaños de mi papá" }
```

Responde `201` con el mismo shape que un elemento del arreglo de arriba (`itemCount: 0`).

### `PATCH`/`DELETE /v1/wishlist/collections/{uuid}` — renombrar/eliminar una lista

`PATCH` acepta `{ "name": "..." }`. Ambas responden `404` si la lista no existe o no
pertenece a la cuenta autenticada (no se distingue de "no existe", mismo patrón que el
resto de recursos propios de esta API — direcciones, reseñas, etc.). `DELETE` responde
`204`, pero **`409` si intentas eliminar la lista "Favoritos"** (`isDefault: true`) — esa
lista no se puede borrar, solo vaciar quitando sus productos uno por uno.

### `GET`/`POST /v1/wishlist/collections/{uuid}/items` — productos de una lista con nombre

Mismo shape de request/response que el atajo de corazón (`GET /v1/wishlist/favorites` /
`PUT /v1/wishlist/favorites/{productUuid}`) — mismo `WishlistItemPublic` para cada item,
mismo `limit`/`offset` en el `GET`. La diferencia es que aquí se manda `productUuid` en
el body en vez de en la URL, y hay que conocer el `uuid` de la lista de antemano:

```http
POST /v1/wishlist/collections/c3d4.../items
x-api-key: <api-key>
Authorization: <token>
Content-Type: application/json

{ "productUuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un" }
```

Responde `201` vacío. `404` si la lista no pertenece al cliente, o si `productUuid` no
existe/no está disponible. Idempotente, igual que el atajo de corazón.

### `DELETE /v1/wishlist/collections/{uuid}/items/{productUuid}` — quitar un producto de una lista

`204` vacío. Idempotente (quitar algo que no estaba guardado no es error). `404` solo si
la lista no pertenece al cliente.

## Pagos con Mercado Pago (Checkout Bricks)

Después de `POST /v1/orders`, renderiza el **Payment Brick** de Mercado Pago
(`@mercadopago/sdk-react` o el script `sdk.mercadopago.com/js/v2`) con
`initialization.{amount, preferenceId}` de la respuesta anterior. La clave pública de
Mercado Pago (`NEXT_PUBLIC_MP_PUBLIC_KEY` o similar) vive en el **env del frontend** —
esta API nunca la expone ni la necesita, solo usa el access token privado internamente.

El Brick soporta tres caminos, y **solo dos de ellos llaman a esta API**:

- **Tarjeta u OXXO/ticket** — el `onSubmit` del Brick entrega un `formData`
  (`token` solo para tarjeta, `paymentMethodId`, `issuerId`, `installments`, `payer`).
  Reenvíalo tal cual a `POST /v1/orders/{order_id}/pay` (ver abajo).
- **Cuenta/Wallet de Mercado Pago** — el Brick redirige directo al sitio de Mercado Pago;
  **esto nunca llama a esta API**. El comprador vuelve a tu sitio via los `back_urls` que
  esta API configuró al crear la preferencia (`/checkout/success`, `/checkout/failure`,
  `/checkout/pending` sobre tu propio dominio — esas páginas las implementa el frontend).
  El pago se confirma por webhook del lado del backend; para saber si ya se aplicó, consulta
  `GET /v1/auth/me/orders/{orderUuid}` (el `status` pasa a `"PAID"` cuando el webhook lo
  confirma — puede tardar unos segundos tras el regreso a `/checkout/success`).

**El correo de confirmación de pedido lo envía el frontend, pero el disparo lo hace este
backend** — en el momento exacto en que un pedido pasa a `"PAID"`, este backend llama a un
webhook saliente en tu propio dominio (ver "Webhook saliente" más abajo). Cubre los tres
caminos de pago por igual (tarjeta/OXXO síncrono, y Wallet/OXXO tardío vía webhook),
incluido el caso Wallet donde nunca hay una respuesta síncrona ni garantía de que el
navegador del comprador siga abierto — por eso el disparo no puede vivir en el navegador
(ni por polling), tiene que ser esta llamada servidor-a-servidor.

### Webhook saliente: `POST {tu dominio}/api/webhooks/order-confirmed`

Este backend llama a esta ruta — **no la llama el navegador del comprador**, la llama
este servidor directamente en cuanto `finalize_order_payment` confirma el pago. Implementa
esta ruta en tu backend (p. ej. un API route de Next.js) para recibirla y disparar el
envío real del correo con tu propio template de `react-email` y tu propia cuenta de
Resend — este backend ya no llama a Resend.

**Verificación de firma** (obligatoria — no proceses el body sin verificar primero):

```http
POST /api/webhooks/order-confirmed
Content-Type: application/json
X-Webhook-Timestamp: 1783961178
X-Webhook-Signature: 3f2a9c...  (hex, HMAC-SHA256)
```

La firma se calcula así (mismo secreto compartido por el equipo de backend,
`FRONTEND_WEBHOOK_SECRET`, nunca expuesto en este documento):

```
manifest = "{X-Webhook-Timestamp}." + <raw request body, tal cual, sin re-serializar>
signature = hex(HMAC_SHA256(FRONTEND_WEBHOOK_SECRET, manifest))
```

Recalcula `signature` con el mismo secreto y compárala contra `X-Webhook-Signature` con
una comparación en tiempo constante (`crypto.timingSafeEqual` en Node, no `===`). Rechaza
también si `X-Webhook-Timestamp` tiene más de ~5 minutos de antigüedad (protección contra
replay). Importante: usa el **body crudo** para el HMAC, no un objeto ya parseado y
re-serializado — un JSON re-serializado puede no ser byte-a-byte idéntico al original y
la firma no va a coincidir.

**Body**: mismo shape que un elemento de `GET /v1/auth/me/orders/{orderUuid}` (ver
arriba), más dos campos que esa ruta no incluye porque ahí la identidad ya viene del
token de auth — aquí no hay token, así que van explícitos en el body:

```json
{
  "uuid": "f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6",
  "sicarOrderId": "d65b89dc-9690-40b3-8dfb-aa2cdde18cc0",
  "id": "d65b89dc-9690-40b3-8dfb-aa2cdde18cc0",
  "serieFolio": null,
  "status": "PAID",
  "dispatchStatus": "PENDING_ACCEPTANCE",
  "dispatchHistory": null,
  "total": 129.99,
  "totalQuantity": 3,
  "deliveryInfo": { "contactInfo": { "name": "Juan Pérez", "phone": "3151234567", "email": "juan@example.com" }, "deliveryType": "PICKUP" },
  "items": [ { "uuid": "3Cny4OOxdX1GoSzL9rEsTZNL7un", "sku": "PR2057", "description": "PORTAROLLO", "quantity": "1", "unit": "PZA", "imageUrl": "https://.../portarollo.jpg" } ],
  "createdAt": "2026-07-10T18:32:05Z",
  "cancellationReason": null,
  "couponCode": null,
  "discountAmount": null,
  "subtotal": null,
  "shippingLabel": null,
  "clientEmail": "juan@example.com",
  "clientName": "Juan Pérez"
}
```

`couponCode`/`discountAmount`/`subtotal` — mismo significado que en `GET /v1/auth/me/orders`
(ver arriba) — vienen aquí "gratis" porque este webhook reusa el mismo `OrderPublic`. Útil si
quieres mostrar "ahorraste $X" en el correo de confirmación.

`clientEmail` es el destinatario a usar — se toma de `deliveryInfo.contactInfo.email` cuando la
orden trae uno, y solo cae de vuelta al email de la cuenta (`ClientAccount.email`) si la orden se
creó sin ese campo (órdenes viejas, u otro caller de `POST /v1/orders` que lo omita). Esto
garantiza que `clientEmail` nunca llegue vacío. `clientName` viene de la cuenta cuando el pedido
es de cuenta — **(2026-08-11)** para un pedido de invitado (sin cuenta, ver "Checkout de
invitado" arriba) cae de vuelta a `deliveryInfo.contactInfo.name`, el mismo nombre capturado en
el checkout.

**Sin reintentos de este lado** — a diferencia del webhook de Mercado Pago hacia este
backend (que sí reintenta agresivamente), este backend **no** reintenta si tu endpoint
falla o tarda. Responde `200` rápido y trata cualquier falla de tu lado como definitiva
(no hay una segunda oportunidad automática todavía). **Nuevo (2026-08-13)**: este backend
ahora dispara la llamada HTTP hacia tu endpoint en segundo plano (después de ya haberle
respondido al comprador/a Mercado Pago) en vez de esperarla en línea — así que un receptor
lento de tu lado ya no le agrega latencia al checkout ni a la respuesta que este backend le
da a Mercado Pago. Esto no cambia el contrato para ti: sigue sin reintentos, sigue siendo
fire-and-forget desde nuestro lado, solo deja de ser una dependencia síncrona en la ruta
crítica. Si necesitas reintentos, impleméntalos tú mismo del lado del frontend antes de
disparar el correo, o avisa al equipo de backend
para evaluar una cola de reintentos ahí.

### Webhook saliente: `POST {tu dominio}/api/webhooks/order-cancelled`

Mismo mecanismo que `order-confirmed` de arriba — este backend llama a esta ruta
directamente (nunca el navegador) en el momento exacto en que un pedido pasa a
`"CANCELLED"`, ya sea porque el cliente lo canceló (`POST /v1/orders/{order_id}/cancel`),
lo eliminó (`DELETE /v1/orders/{order_id}`), un pago con Mercado Pago fue
rechazado/cancelado (webhook de Mercado Pago o el `POST /v1/orders/{order_id}/pay`
síncrono), o un administrador lo canceló desde el dashboard admin
(`POST /v1/admin/orders/{orderUuid}/cancel`, ver `ADMIN_INTEGRATION.md`). Implementa esta
ruta para disparar tu propio correo de cancelación, igual que con `order-confirmed`.

**Verificación de firma**: exactamente el mismo esquema que `order-confirmed` — mismos
headers, mismo secreto compartido (`FRONTEND_WEBHOOK_SECRET`), misma fórmula de manifest.
No la repetimos aquí — consulta la sección de `order-confirmed` arriba.

**Body**: mismo shape que `order-confirmed` (un elemento de `GET /v1/auth/me/orders/{orderUuid}`
más `clientEmail`/`clientName`), con `"status": "CANCELLED"`. `cancellationReason` es `null`
para las tres cancelaciones disparadas por el cliente arriba, y un texto libre cuando fue un
administrador quien canceló — útil si quieres mostrarle al cliente el motivo en el correo de
cancelación.

**Importante — si el pedido ya había sido aceptado por un administrador, avisarle a Sicar X
puede seguir en curso cuando llega este webhook** (y si nunca fue aceptado, no se le avisa
nada a Sicar X en absoluto). La cancelación local ya es un hecho consumado de cualquier forma
— ver la nota sobre `sicarTimestamp`/sincronización asíncrona en
`POST /v1/orders/{order_id}/cancel` más abajo, no la repetimos aquí. Para el correo al
cliente esto no importa: la cancelación ya es definitiva de su lado en cuanto reciben
esta notificación.

**Sin reintentos de este lado** — mismo comportamiento que `order-confirmed`: responde
`200` rápido, no hay reintento automático si tu endpoint falla o tarda. **(2026-08-13)**
también comparte el mismo cambio que `order-confirmed`: la llamada HTTP hacia tu endpoint
ahora se dispara en segundo plano, así que un receptor lento de tu lado ya no le agrega
latencia a `POST /v1/orders/{order_id}/cancel` ni a `DELETE /v1/orders/{order_id}`.

### Webhook saliente: `POST {tu dominio}/api/webhooks/order-status-changed`

Mismo mecanismo que `order-confirmed`/`order-cancelled` de arriba — este backend llama a esta ruta
directamente (nunca el navegador) cuando el **dashboard admin** (no Sicar X — ver
`ADMIN_INTEGRATION.md`) avanza el `dispatchStatus` de un pedido a alguno de estos 3 momentos
concretos:

- `event: "order-accepted"` — al aceptar el pedido (`PENDING_ACCEPTANCE` → `PENDING`).
- `event: "order-ready-pickup"` — al llegar a `COMPLETE`, **solo para pedidos `PICKUP`**.
- `event: "order-dispatched"` — al llegar a `DISPATCHED` (pedidos `DELIVERYMAN`), sin importar si
  fue vía una guía real de envia.com o un avance manual del dashboard.

**No hay evento al llegar a `PREPARING`**, y **no hay evento al llegar a `COMPLETE` para un pedido
`DELIVERYMAN`** (esa es una etapa interna de preparación — el cliente no tiene nada que hacer
todavía, solo se le avisa hasta que de verdad se envía). Tampoco se dispara nada al revertir un
paso (una corrección del dashboard, ver `ADMIN_INTEGRATION.md`).

**Verificación de firma**: exactamente el mismo esquema que `order-confirmed` — mismos headers,
mismo secreto compartido (`FRONTEND_WEBHOOK_SECRET`), misma fórmula de manifest. No la repetimos
aquí — consulta la sección de `order-confirmed` arriba.

**Body**: mismo shape que `order-confirmed` (un elemento de `GET /v1/auth/me/orders/{orderUuid}`
más `clientEmail`/`clientName`), más dos campos nuevos:

```json
{
  "...": "... (mismos campos que order-confirmed)",
  "clientEmail": "juan@example.com",
  "clientName": "Juan Pérez",
  "event": "order-ready-pickup",
  "statusMessage": "Tu pedido está listo para recoger"
}
```

`event` es el discriminador (`"order-accepted"` | `"order-ready-pickup"` | `"order-dispatched"`) por
si prefieres tu propio texto por idioma/canal; `statusMessage` es el texto en español ya armado,
listo para usar directamente si no necesitas personalizarlo.

**`event: "order-dispatched"` puede traer una guía real de envia.com** — mismo campo `shippingLabel`
documentado arriba en `GET /v1/auth/me/orders/{orderUuid}`, poblado cuando este evento se disparó
porque un admin generó una guía real (`POST /v1/admin/orders/{uuid}/shipping/generate`), y `null`
cuando fue un avance manual del dashboard sin guía física de por medio:

```json
{
  "...": "... (mismos campos que order-confirmed)",
  "shippingLabel": {
    "carrier": "fedex",
    "service": "ground",
    "shipmentId": 987654,
    "serviceDescription": "Fedex Ground",
    "trackingNumber": "794658125486",
    "trackUrl": "https://www.fedex.com/fedextrack/?trknbr=794658125486",
    "labelUrl": "https://envia.com/labels/abc123.pdf",
    "totalPrice": 145.00,
    "currency": "MXN",
    "weight": 1.5,
    "length": 20,
    "width": 15,
    "height": 10,
    "generatedAt": "2026-08-11T18:02:11Z"
  },
  "clientEmail": "juan@example.com",
  "clientName": "Juan Pérez",
  "event": "order-dispatched",
  "statusMessage": "Tu pedido está listo y ha sido enviado"
}
```

Usa `shippingLabel.trackUrl` como la liga de "sigue tu paquete" en el correo de envío — viene
directamente de la respuesta de envia.com, no hace falta construirla a partir del `carrier`/
`trackingNumber`.

**Sin reintentos de este lado** — mismo comportamiento que `order-confirmed`: responde `200`
rápido, no hay reintento automático si tu endpoint falla o tarda.

### Webhook saliente: `POST {tu dominio}/api/webhooks/verification-requested`

Mismo mecanismo que el webhook `order-confirmed` de arriba — este backend llama a esta
ruta directamente (nunca el navegador) cuando hay que enviar un correo de verificación:
justo después de `POST /v1/auth/register`, o cuando se llama a
`POST /v1/auth/resend-verification`. Implementa esta ruta para disparar el envío real con
tu propio template y tu propia cuenta de Resend, igual que con `order-confirmed`.

**Verificación de firma**: exactamente el mismo esquema que `order-confirmed` — mismos
headers (`X-Webhook-Timestamp`/`X-Webhook-Signature`), mismo secreto compartido
(`FRONTEND_WEBHOOK_SECRET`), misma fórmula de manifest, mismas recomendaciones (comparación
en tiempo constante, rechazar timestamps viejos, usar el body crudo). No la repetimos aquí
— consulta la sección de `order-confirmed` arriba, es el mismo código de verificación.

**Body**:

```json
{
  "clientUuid": "f6bacfb9-cb38-4f96-adab-2593a14345bc",
  "clientName": "Juan Pérez",
  "clientEmail": "juan@example.com",
  "token": "eyJhbGciOiJIUzI1NiIs..."
}
```

`token` es el que hay que meter en el link del correo (p. ej.
`https://tudominio.com/verificar-correo?token={token}`) — cuando el usuario lo abre, esa
página del frontend llama a `POST /v1/auth/verify-email` con ese mismo valor. Vigencia de
24h desde que se generó este webhook; pasado ese tiempo, `/v1/auth/verify-email` responde
`401` y el usuario necesita pedir uno nuevo desde `/v1/auth/resend-verification`.

**Sin reintentos de este lado** — mismo comportamiento que `order-confirmed`: responde
`200` rápido, no hay reintento automático si tu endpoint falla.

### Webhook saliente: `POST {tu dominio}/api/webhooks/password-reset-requested`

Mismo mecanismo que `verification-requested` arriba — este backend llama a esta ruta
directamente (nunca el navegador) cuando hay que enviar un correo de recuperación de
contraseña, justo después de `POST /v1/auth/forgot-password`. Implementa esta ruta para
disparar el envío real con tu propio template y tu propia cuenta de Resend.

**Verificación de firma**: exactamente el mismo esquema que `order-confirmed` — mismos
headers, mismo secreto compartido (`FRONTEND_WEBHOOK_SECRET`), misma fórmula de manifest.
No la repetimos aquí — consulta la sección de `order-confirmed` arriba.

**Body**:

```json
{
  "clientUuid": "f6bacfb9-cb38-4f96-adab-2593a14345bc",
  "clientName": "Juan Pérez",
  "clientEmail": "juan@example.com",
  "hasPassword": true,
  "resetToken": "AaBbCc...",
  "expiresInMinutes": 30
}
```

`hasPassword` distingue dos plantillas de correo distintas:

- `true` — `resetToken`/`expiresInMinutes` vienen poblados. Arma el link con `resetToken`
  (p. ej. `https://tudominio.com/restablecer-contraseña?token={resetToken}`) — cuando el
  usuario lo abre, esa página llama a `POST /v1/auth/reset-password` con ese mismo valor.
  Pasado el TTL, esa ruta responde `400` y el usuario necesita pedir uno nuevo desde
  `/v1/auth/forgot-password`.
- `false` — la cuenta es solo-Google (sin contraseña local); `resetToken`/
  `expiresInMinutes` vienen `null`. No hay nada que resetear — envía un correo distinto
  explicándole a esa persona que su cuenta inicia sesión con Google, en vez de un link de
  recuperación. Esta es la única señal de esta distinción: `POST /v1/auth/forgot-password`
  responde igual en ambos casos, para no filtrar el tipo de cuenta por HTTP.

**Sin reintentos de este lado** — mismo comportamiento que `order-confirmed`: responde
`200` rápido, no hay reintento automático si tu endpoint falla.

### `POST /v1/orders/{order_id}/pay` — cobrar pedido (tarjeta/OXXO)

Cuenta: requiere `X-Client-Token` — el pedido debe pertenecer a la cuenta autenticada (mismo
patrón de `404` que `/cancel`, no confirma si el pedido existe pero es de otra cuenta).
Invitado: omite `X-Client-Token` — `{order_id}` (el `id` o el `orderUuid` que devolvió
`POST /v1/orders`, cualquiera de los dos) es en sí mismo la prueba de pertenencia, `404` si
no existe/ya no es de invitado (ver "Checkout de invitado" arriba).

```http
POST /v1/orders/d65b89dc-9690-40b3-8dfb-aa2cdde18cc0/pay
x-api-key: <api-key>
X-Client-Token: <token de /v1/auth/login o /v1/auth/register>
Content-Type: application/json

{
  "token": "ff8080814c11e237014c1ff593b57b4d",
  "paymentMethodId": "visa",
  "issuerId": "310",
  "installments": 1,
  "payer": {
    "email": "juan@example.com",
    "identification": { "type": "RFC", "number": "XAXX010101000" }
  }
}
```

Manda exactamente el `formData` que entrega el `onSubmit` del Brick — `token` está ausente
para métodos sin tarjeta (p. ej. OXXO). **No mandes ningún monto** — el backend siempre cobra
el `amount` ya calculado en `POST /v1/orders`, nunca un valor que venga del frontend.

Respuesta `200`:
```json
{
  "orderUuid": "f1a2b3c4-d5e6-47f8-a9b0-c1d2e3f4a5b6",
  "status": "PAID",
  "mpPaymentId": "123456789",
  "mpStatus": "approved",
  "mpStatusDetail": "accredited",
  "ticketUrl": null
}
```

`status` es el estado local del pedido después del intento de cobro:
- `"PAID"` — aprobado. **(2026-07-31)** Esto ya no toca Sicar X en absoluto — el pago vive
  solo en Mercado Pago y en este backend; Sicar X se entera del pedido más adelante, solo si
  un administrador lo acepta (ver `ADMIN_INTEGRATION.md`), y solo se le avisa un descuento de
  inventario, nunca un pago.
- `"TO_PAY"` — pendiente (tarjeta en revisión, o pago OXXO esperando que el comprador pague en
  tienda). `ticketUrl` viene con la liga al comprobante/código de barras para métodos OXXO —
  muéstrala al comprador para que pueda completar el pago. El pedido se confirma después via
  webhook; consulta `GET /v1/auth/me/orders/{orderUuid}` más tarde para ver si ya pasó a `PAID`.
- `"CANCELLED"` — rechazado. El stock reservado ya se liberó, no hace falta llamar a
  `POST /v1/orders/{order_id}/cancel` aparte.

Errores esperables:
- `401` — se mandó `X-Client-Token` pero es inválido/expiró
- `404` — el pedido no existe, no pertenece a la cuenta autenticada, o (invitado) ya fue
  vinculado a una cuenta
- `409` — el pedido ya fue **cancelado** antes (no se puede cobrar un pedido cancelado)
- `502` — Mercado Pago rechazó la solicitud de cobro (reintenta más tarde)

**Nuevo (2026-08-13) — reintento seguro si el pedido ya quedó `PAID`**: si vuelves a llamar
a este endpoint para un pedido que ya está `PAID` (p. ej. la respuesta del primer submit se
perdió por un problema de red, pero el cobro sí se aplicó), la respuesta es exactamente el
mismo `200` de arriba con el resultado ya conocido — **ya no es `409`**. No hay riesgo de
doble cobro: Mercado Pago ya deduplica internamente por `orderUuid`. Solo un pedido ya
`CANCELLED` sigue devolviendo `409` (ese sí es un estado del que un reintento no debe
"recuperarse" silenciosamente).

### `POST /v1/orders/{order_id}/cancel` — cancelar pedido

Cuenta: requiere `X-Client-Token` — el pedido debe pertenecer a la cuenta autenticada, o
responde `404` (sin revelar si el pedido existe pero es de otra cuenta). Invitado: omite
`X-Client-Token` — `{order_id}` (`id` u `orderUuid`) prueba la pertenencia por sí mismo, ver
"Checkout de invitado" arriba. Si el pedido ya tenía un pago de Mercado Pago asociado, esta
llamada también lo reembolsa (si ya estaba aprobado) o lo cancela (si seguía pendiente)
automáticamente — no hace falta ningún paso aparte del lado del frontend para eso.

```http
POST /v1/orders/d65b89dc-9690-40b3-8dfb-aa2cdde18cc0/cancel
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
`Authorization`, no es un token de sesión). El body ya no lleva `uuid`: solo `products` y
`cashRegisterUuid` (opcional — tiene un valor por defecto del lado del servidor, solo hace falta
enviarlo si se necesita cancelar contra una caja distinta a la default).

**`products` ya no se usa para nada** — se sigue aceptando por compatibilidad (puedes seguir
enviándolo o dejar de hacerlo, cualquiera de las dos formas funciona) pero el stock se restaura
siempre a partir de lo que quedó guardado del pedido original en el servidor, nunca de este campo:

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
  "documentUuid": "d65b89dc-9690-40b3-8dfb-aa2cdde18cc0",
  "orderId": "d65b89dc-9690-40b3-8dfb-aa2cdde18cc0",
  "sicarTimestamp": 1783961225017.0,
  "message": "Pedido cancelado exitosamente.",
  "status": "CANCELLED"
}
```

**Nuevo (2026-08-13) — `orderId` es el nombre recomendado, `documentUuid` queda como alias
histórico**: mismo valor exacto (el `{order_id}` que se mandó en la URL) — el nombre
`documentUuid` viene de cuando esto sí era el UUID de un documento real de Sicar X, ya no es
el caso desde el rediseño "SICAR es solo ERP de inventario". `documentUuid` se sigue mandando
y se seguirá mandando, no hay fecha de retiro planeada.

**Importante — esta respuesta ya no espera a Sicar X.** El pedido queda `CANCELLED` y el
stock restaurado de inmediato en cuanto responde esta llamada (así un Sicar X caído nunca
bloquea que un cliente cancele). `sicarTimestamp` significa "cuándo se aceptó la cancelación
localmente", nunca una confirmación de Sicar X — para el frontend esto no cambia nada
práctico, la cancelación ya es definitiva desde el punto de vista del cliente en cuanto llega
esta respuesta `200`.

**Actualización (2026-07-31)**: si el pedido nunca llegó a ser aceptado por un administrador
(el caso normal para casi cualquier cancelación — el cliente suele cancelar antes de que
alguien lo revise), esta cancelación **no le avisa nada a Sicar X en absoluto**, porque Sicar X
nunca supo que este pedido existía. Solo si el pedido ya había sido aceptado (ver
`ADMIN_INTEGRATION.md`) se encola un aviso asíncrono a Sicar X para revertir el descuento de
inventario que se le había avisado al aceptarlo. Ninguno de los dos casos cambia la respuesta
que ves aquí ni requiere ningún manejo distinto del lado del frontend.

**Actualización (2026-08-07)**: matiza "el stock restaurado de inmediato" de arriba — sigue
siendo exactamente así en el caso normal (pedido nunca aceptado). Si el pedido **sí** había
sido aceptado, el stock disponible (`stock` en `/v1/products`/`/v1/search`, que ya representa
lo vendible neto de reservas) se restaura recién cuando el aviso asíncrono a Sicar X del
párrafo de arriba tiene éxito — normalmente bien por debajo de un minuto, hasta ~16 minutos en
el peor caso si Sicar X está teniendo problemas. Nada cambia en la respuesta de esta llamada ni
en que la cancelación ya es definitiva de inmediato para el cliente — el único efecto práctico
es que, solo en ese caso puntual, el producto puede tardar un poco en volver a mostrarse con
stock disponible en el catálogo.

Errores esperables:
- `401` — se mandó `X-Client-Token` pero es inválido/expiró
- `404` — el pedido no existe, no pertenece a la cuenta autenticada, o (invitado) ya fue
  vinculado a una cuenta
- `409` — el pedido ya fue cancelado antes
- `409` — el pedido ya fue **enviado** (`dispatchStatus: "DISPATCHED"`) — a partir de ese
  punto ya no se puede cancelar por este medio. `"COMPLETE"` no cuenta como enviado para
  este caso — también es el estado terminal de pedidos `PICKUP` listos para recoger en
  tienda, que nunca salieron a ningún lado, así que esos sí siguen siendo cancelables.

### `DELETE /v1/orders/{order_id}` — eliminar pedido reservado sin pagar

Distinto de `/cancel`: `/cancel` conserva el pedido en el historial con `status: "CANCELLED"`;
`DELETE` lo borra por completo del historial del cliente (`GET /v1/auth/me/orders` ya no lo
lista). Úsalo para "descartar" una reserva que el cliente nunca terminó de pagar (p. ej. un botón
de "eliminar" sobre un pedido en `TO_PAY`, en vez de "cancelar pedido"). El contrato de cara al
frontend no cambia con el cambio de `/cancel` de arriba (misma sincronización asíncrona con
Sicar X de fondo, ver esa sección) — esta ruta sigue devolviendo `204` de inmediato y el pedido
sigue desapareciendo de `GET /v1/auth/me/orders` en el acto.

Cuenta: requiere `X-Client-Token` — el pedido debe pertenecer a la cuenta autenticada, o
responde `404` (mismo criterio que `/cancel`). Invitado: omite `X-Client-Token` — `{order_id}`
(`id` u `orderUuid`) prueba la pertenencia por sí mismo, ver "Checkout de invitado" arriba.
Solo funciona sobre pedidos en `status: "TO_PAY"` — `409` si el
pedido ya está `PAID` o `CANCELLED` (esos no se pueden borrar). No lleva body: el stock a
restaurar ya se toma de lo guardado al crear el pedido, y si había un pago de Mercado Pago
pendiente (OXXO sin pagar, tarjeta en revisión) se cancela automáticamente, igual que en
`/cancel`. **(2026-07-31)**: un pedido en `TO_PAY` nunca pudo haber sido aceptado por un
administrador (eso requiere `status: "PAID"`), así que esta ruta nunca le avisa nada a Sicar
X — a diferencia de `/cancel`, aquí no hay ni siquiera el caso condicional.

```http
DELETE /v1/orders/d65b89dc-9690-40b3-8dfb-aa2cdde18cc0
x-api-key: <api-key>
X-Client-Token: <token de /v1/auth/login o /v1/auth/register>
```

Respuesta `204` (sin body) en éxito.

---

## Ejemplo mínimo (fetch, Next.js)

```ts
const API_URL = process.env.NEXT_PUBLIC_API_URL!;   // ej. https://api-production-cf7a.up.railway.app
const API_KEY = process.env.NEXT_PUBLIC_API_KEY!;    // provisto por backend

async function getCatalog(filters: { limit?: number; offset?: number; departmentUuid?: string }) {
  const res = await fetch(`${API_URL}/v1/products`, {
    method: "POST",
    headers: { "x-api-key": API_KEY, "Content-Type": "application/json" },
    body: JSON.stringify({ limit: 60, offset: 0, ...filters }),
  });
  return res.json(); // { total, docs }
}

// credentials: "include" es obligatorio en TODA llamada a /v1/cart* -- sin esto el navegador
// nunca manda ni guarda la cookie httpOnly del carrito anonimo (cross-site, ver la seccion
// "Dos capas de autenticacion", punto 3).
async function saveCart(items: { uuid: string; quantity: number }[], clientToken?: string) {
  const res = await fetch(`${API_URL}/v1/cart`, {
    method: "PUT",
    credentials: "include",
    headers: {
      "x-api-key": API_KEY,
      "Content-Type": "application/json",
      ...(clientToken ? { "X-Client-Token": clientToken } : {}),
    },
    body: JSON.stringify({ items }),
  });
  if (!res.ok) throw new Error("No se pudo guardar el carrito");
  return res.json(); // { items, subtotal, totalQuantity, cartToken, updatedAt }
}

// delta positivo agrega/incrementa, negativo decrementa (<=0 resultante elimina la linea).
async function adjustCartItem(productUuid: string, delta: number, clientToken?: string) {
  const res = await fetch(`${API_URL}/v1/cart/items`, {
    method: "PATCH",
    credentials: "include",
    headers: {
      "x-api-key": API_KEY,
      "Content-Type": "application/json",
      ...(clientToken ? { "X-Client-Token": clientToken } : {}),
    },
    body: JSON.stringify({ productUuid, delta }),
  });
  if (!res.ok) throw new Error("No se pudo actualizar el carrito");
  return res.json(); // { items, subtotal, totalQuantity, cartToken, updatedAt }
}

// cartToken (opcional) es el de un carrito anonimo armado antes de iniciar sesion -- guardado en
// memoria desde una respuesta previa de /v1/cart (ver el punto 3 de "Dos capas de autenticacion").
// La fusion ocurre en esta misma llamada; la respuesta ya trae `cart` listo para pintar la UI.
async function login(email: string, password: string, cartToken?: string) {
  const res = await fetch(`${API_URL}/v1/auth/login`, {
    method: "POST",
    headers: { "x-api-key": API_KEY, "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, ...(cartToken ? { cartToken } : {}) }),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail ?? "No se pudo iniciar sesión");
  }
  return res.json(); // { token, client, cart }
}

// Solo hace falta si el usuario YA esta logueado (otra pestana/dispositivo) y arma un carrito
// anonimo nuevo -- login/registro ya fusionan automaticamente, ver login() arriba.
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

// Ya NO cobra -- solo reserva el pedido localmente (TO_PAY) y prepara el cobro con
// Mercado Pago. Renderiza el Payment Brick con el `amount`/`preferenceId` de la respuesta.
// clientToken es opcional (2026-08-11) -- omitelo (undefined) para checkout de invitado;
// en ese caso contactInfo.email es obligatorio y no se puede mandar couponCode.
async function createOrder(
  clientToken: string | undefined,
  products: { uuid: string; quantity: number }[],
  contactInfo: { name: string; phone: string; email?: string },
) {
  const res = await fetch(`${API_URL}/v1/orders`, {
    method: "POST",
    headers: {
      "x-api-key": API_KEY,
      ...(clientToken ? { "X-Client-Token": clientToken } : {}),
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
  return res.json(); // { id, serieFolio, date, status, orderUuid, preferenceId, amount }
  // Guarda `id`/`orderUuid` -- si fue checkout de invitado (sin clientToken), son la unica
  // forma de reclamar el pedido despues en payOrder/cancel/DELETE/GET /orders/guest/{id}.
}

// Llamado desde el onSubmit del Payment Brick (tarjeta/OXXO) -- NO se llama para el
// metodo Wallet, que redirige directo a Mercado Pago (ver "Pagos con Mercado Pago").
// clientToken es opcional -- omitelo para un pedido de invitado (el propio orderId ya
// prueba pertenencia en ese caso).
async function payOrder(orderId: string, clientToken: string | undefined, formData: Record<string, unknown>) {
  const res = await fetch(`${API_URL}/v1/orders/${orderId}/pay`, {
    method: "POST",
    headers: {
      "x-api-key": API_KEY,
      ...(clientToken ? { "X-Client-Token": clientToken } : {}),
      "Content-Type": "application/json",
    },
    body: JSON.stringify(formData), // token/paymentMethodId/issuerId/installments/payer, tal cual del Brick
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail ?? "No se pudo procesar el pago");
  }
  return res.json(); // { orderUuid, status, mpPaymentId, mpStatus, mpStatusDetail, ticketUrl }
}
```

## Notas y advertencias

- **Nuevo (2026-08-19): wishlist / lista de favoritos** — `/v1/wishlist/*`, ver sección
  dedicada arriba. Solo para cuentas de cliente (sin equivalente de invitado/anónimo, a
  diferencia del carrito), autenticada con `Authorization` (no `X-Client-Token`). No
  requiere ningún cambio en rutas existentes.
- **Precios/stock pueden cambiar entre que se muestran y se compran** — `/v1/orders` valida
  disponibilidad contra el catálogo local (sincronizado desde Sicar X cada 5 minutos, sin
  ninguna llamada en vivo en este paso) antes de confirmar; un `409` en checkout es normal y
  esperado, no un bug.
- **`X-Client-Token` expira** — si `/v1/orders` responde `401` cuando sí se mandó el header,
  vuelve a llamar `/v1/auth/login`.
- **(2026-08-11) Login ya NO es obligatorio para comprar** — existe checkout de invitado:
  omitir `X-Client-Token` por completo en `/v1/orders`, `/v1/orders/{order_id}/pay`,
  `/v1/orders/{order_id}/cancel` y `DELETE /v1/orders/{order_id}` ya no es un `401`, es la
  señal de "este pedido es de invitado" — ver "Checkout de invitado" arriba para el contrato
  completo (email obligatorio, dirección inline en vez de `addressUuid`, sin cupones, y cómo
  se prueba pertenencia sin sesión).
- **`POST /v1/orders` ya no cobra ni confirma el pedido de inmediato** — solo lo reserva
  (`status: "TO_PAY"`). El cobro real ocurre en `POST /v1/orders/{order_id}/pay` (tarjeta/OXXO) o,
  para el método Wallet de Mercado Pago, nunca pasa por este backend — se confirma por webhook.
  No asumas que un pedido está pagado solo porque `POST /v1/orders` respondió `200`.
- **Seguimiento post-compra sí existe**: `GET /v1/auth/me/orders` (lista) y
  `GET /v1/auth/me/orders/{orderUuid}` (detalle, con `dispatchStatus`/`dispatchHistory`) — ver
  referencia arriba. `dispatchStatus` es controlado enteramente por el dashboard admin, ya no se
  consulta a Sicar X en vivo desde ninguna ruta de este backend — ver el webhook
  `order-status-changed` arriba para enterarte de cambios sin tener que hacer polling.
- **No hay seguimiento de paquete en tránsito todavía** (ubicación en vivo, ETA) — es una
  integración futura con el webhook de envia.com, no construida en este cambio. El webhook
  `order-status-changed` de arriba solo cubre aceptado/listo-para-recoger/enviado.
- **`/v1/cart*` no valida disponibilidad** — guardar o leer el carrito no revisa stock/precio en
  absoluto, ni siquiera contra el catálogo local (que sí usa `/v1/orders`, ver arriba). El `409`
  de "sin disponibilidad suficiente" solo puede pasar hasta el checkout real (`/v1/orders`), no
  al guardar el carrito.
- **La cabecera de la cuenta cambia según la ruta del carrito** — `X-Client-Token` en
  `GET`/`PUT`/`PATCH`/`DELETE /v1/cart*`, `Authorization` en `POST /v1/cart/merge`. Revisa la
  sección "Dos capas de autenticación" (punto 3) y los ejemplos de cada endpoint si algo da `401`
  inesperado.
- **`credentials: "include"` es obligatorio en toda llamada a `/v1/cart*`** — sin esto, el carrito
  anónimo (cookie `httpOnly` `charly_cart_token`, cross-site entre `ferreteriacharly.com` y
  `api-production-cf7a.up.railway.app`) nunca se guarda ni se reenvía, y cada visita se ve como un
  carrito nuevo vacío. No aplica a `/v1/auth/*` ni al resto de la API — es específico de `/v1/cart*`.
- **Nuevo: registro de "más vendidos" y orden por relevancia.** `GET /v1/products/best-sellers`
  es el nuevo endpoint para una sección "Los más vendidos" en la página principal (ver
  referencia arriba). `POST /v1/products` gana un cuarto valor de `sortBy`, `"relevance"`
  (ordena por popularidad). `POST /v1/search` gana `sortBy` como campo nuevo (antes no existía
  ahí), con default `"relevance"` que preserva el orden de siempre pero ahora desempata por
  popularidad en vez de solo por nombre — no requiere ningún cambio si tu frontend no manda
  `sortBy` hoy. Todo esto se apoya en un campo nuevo y aditivo, `salesCount`, en cada producto
  de `POST /v1/products`/`POST /v1/search`/`GET /v1/products/best-sellers` (no en
  `GET /v1/products/{uuid}`).
- **Ya no existe `X-Cart-Token` ni almacenamiento manual del carrito anónimo** — si el frontend
  todavía tiene un `lib/cartToken.ts` o similar guardando ese header en `localStorage`, puede
  eliminarse: la identidad anónima ahora es 100% automática vía cookie (ver el punto 3 de "Dos
  capas de autenticación"). Solo sigue haciendo falta guardar `cartToken` **en memoria** (no
  persistente) para pasarlo como `cartToken` en `/v1/auth/login`/`/v1/auth/register`.
- **Nuevo (2026-08-13) — nombres de campo consolidados, nada roto, nada retirado.** Varias
  respuestas tenían el mismo dato bajo dos nombres distintos según el endpoint; ahora ambos
  nombres viajan **siempre** en ambos lados, así que no hay nada que migrar con urgencia, solo
  una recomendación de a cuál conviene apuntar el código nuevo:
  - `POST /v1/orders` y el webhook `order-confirmed`: usa `id`, no `sicarOrderId`, en
    `GET /v1/auth/me/orders*` — ahora ambas respuestas traen `id`.
  - Usa `total`, no `amount`, en `POST /v1/orders` — ahora esa respuesta trae ambos
    (`OrderPublic.total` ya se llamaba así).
  - Usa `orderId`, no `documentUuid`, en `POST /v1/orders/{order_id}/cancel` — ahora esa
    respuesta trae ambos.
  - `POST /v1/orders` y `POST /v1/auth/register` ahora responden `201` (antes `200`) al crear
    de verdad — si tu cliente HTTP solo trataba `200` como éxito, acéptalo como `2xx`. La
    única excepción es un reintento con el mismo `Idempotency-Key` en `POST /v1/orders`, que
    sigue respondiendo `200` porque no crea nada nuevo (ver esa sección arriba).
  - `POST /v1/orders/{order_id}/pay` ya no responde `409` para un pedido que ya quedó `PAID`
    — un reintento ahora recibe el mismo `200` de la primera vez (ver esa sección arriba).
    Sigue siendo `409` para un pedido `CANCELLED`.
  - Las notificaciones salientes (`order-confirmed`/`order-cancelled`) ahora se disparan en
    segundo plano de este lado — no cambia nada del contrato que implementas, solo deja de
    ser una dependencia síncrona en la ruta crítica de checkout (ver la nota junto al webhook
    `order-confirmed` arriba).
- **Nuevo (2026-08-13) — dos cambios de comportamiento que sí conviene revisar del lado del
  frontend** (a diferencia de todo lo demás en esta fecha, que es aditivo/no-op):
  - **Todo `422` ahora trae `detail` como string, nunca como lista.** Antes, un error de
    validación de FastAPI (campo faltante, tipo inválido, etc. — en cualquier endpoint, no
    solo checkout) devolvía `detail` como `[{loc, msg, type}, ...]`, distinto al `detail`
    string que ya usaba cualquier otro error (`400`/`401`/`404`/`409`) de esta API. Si en
    algún lado tu código hace algo como `Array.isArray(error.detail)` o
    `error.detail[0].msg` para mostrar errores de validación campo por campo, eso ya no va a
    funcionar — ahora `error.detail` es siempre un string legible (p. ej.
    `"products: Field required; deliveryInfo: Field required"`), igual que cualquier otro
    error de esta API. Si tu manejo de errores ya trata `detail` genéricamente como texto,
    no hay nada que cambiar.
  - **`contactInfo.phone` vacío o incompleto ahora es `422` en checkout, antes pasaba
    silenciosamente** — ver el detalle completo en la sección de `POST /v1/orders` arriba.
    Si tu formulario de checkout no exige ya un teléfono completo (10 dígitos) antes de
    enviar, revisa que muestres bien este `422` nuevo en vez de dejar que se vea como un
    error genérico sin explicación.
