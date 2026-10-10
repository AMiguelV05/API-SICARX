# Solicitudes al backend (API SICARX): funciones de prioridad media

Fecha: 2026-10-10 · Origen: revisión del e-commerce (frontend `ferreteriacharly.com`)

El frontend ya implementa todo lo que se puede hacer con el contrato actual:
- Vistos recientemente (solo en el navegador).
- Relacionados aproximados por categoría y marca.
- Sección "Ofertas" con `tag: "oferta"`.
- Medición GA4 con consentimiento.

Este documento lista lo que **falta del lado del backend** para completar esas funciones.

**Convenciones** (igual que el resto de `FRONTEND_INTEGRATION.md`):
- JSON en camelCase y errores `{"detail": "..."}` en español.
- Todo detrás de `x-api-key`.
- Los webhooks salientes nuevos usan el mismo esquema que `verification-requested` y `order-confirmed`: HMAC con `FRONTEND_WEBHOOK_SECRET` y encabezados `X-Webhook-Timestamp` y `X-Webhook-Signature`.
- El frontend responde `200` en cuanto valida la firma y manda el correo con Resend después.

Prioridad sugerida: **1 → 7 → 8 → 2 → 3 → 6 → 5 → 4**. Los puntos 7 y 8 son preguntas o ajustes pequeños.

---

## 1. Precio de oferta (precio tachado)

**Para qué:** mostrar "~~$150.00~~ $120.00 · −20 %" en tarjetas, ficha, carrito y en una sección de ofertas. Hoy solo existe la etiqueta `oferta` en `Product.tags`, que no dice cuál era el precio anterior.

**Pedido:**
- Agregar a `Product` dos campos:
  - `compareAtPrice: number | null`: precio anterior, **con IVA**, igual que `price`. Si no es `null`, debe ser mayor que `price`.
  - `saleEndsAt: string | null`: ISO-8601. Al pasar esa fecha, el backend devuelve `compareAtPrice: null` sin intervención manual.
- Incluir los dos campos en todas las respuestas que traen productos:
  - `POST /v1/products` (`docs[]`), `POST /v1/search`, `GET /v1/search/suggest`
  - `GET /v1/products/{uuid}`, `best-sellers` y `available-now`
  - los renglones de `GET /v1/cart` y los favoritos
- Nuevo filtro `onSale: boolean` en `POST /v1/products` y `POST /v1/search`, que regresa solo productos con `compareAtPrice` vigente.
- Admin:
  - `PUT /v1/admin/products/{uuid}/sale` con `{ compareAtPrice, saleEndsAt }`.
  - `DELETE` para quitar la oferta.
  - Opcional: una operación masiva por categoría o por lista de uuids.
- **El cobro no cambia:** el pedido sigue usando `price`. `compareAtPrice` es solo informativo.

**Pregunta:** ¿SICAR X ya maneja precios de lista y de oferta, o promociones, que se puedan sincronizar en vez de capturarlos a mano en el admin?

## 2. Productos relacionados

**Para qué:** sección "También te puede servir" en la ficha. Hoy el frontend arma una lista aproximada por categoría y marca, pero la mayoría de los productos no tienen categoría ni marca asignada, así que casi siempre sale vacía.

**Pedido:** `GET /v1/products/{uuid}/related?limit=8` (límite de 1 a 24, por defecto 8). Responde `{ docs: [...] }` con la **misma forma que los `docs` de `POST /v1/products`**.

Estrategia sugerida, en este orden y sin repetir productos:
1. **Comprados juntos:** productos que aparecen en los mismos pedidos pagados de los últimos 180 días, ordenados por frecuencia.
2. Mismo nodo de taxonomía, o el padre si el nodo tiene pocos.
3. Misma marca.
4. Compatibles con los mismos vehículos (`vehicleUuid`).

Reglas:
- Excluir el propio producto y los de su mismo `variantGroup`.
- Solo productos activos, con existencia primero.
- Cachear del lado del servidor, porque cambia poco.

`404` si el uuid no existe (mismo criterio que `GET /v1/products/{uuid}`).

## 3. Avisos de reabastecimiento ("Avísame cuando llegue")

**Para qué:** en un producto agotado, que la persona deje su correo y reciba un aviso cuando vuelva a haber existencia.

**Pedido:**
- `POST /v1/products/{uuid}/stock-alerts` con body `{ "email": "..." }`:
  - `X-Client-Token` es opcional. Si viene, se usa el correo de la cuenta y se ignora el del body.
  - Es idempotente: suscribirse dos veces responde `200`, no duplica.
  - Respuestas: `201` creada; `409` si el producto **sí** tiene existencia; `422` si el correo no es válido.
  - Límite de frecuencia por IP y por correo. El frontend ya reenvía la IP del cliente, igual que en las otras rutas públicas.
- `GET /v1/auth/me/stock-alerts`: las suscripciones de la cuenta.
- `DELETE /v1/auth/me/stock-alerts/{productUuid}`: cancelar una suscripción de la cuenta.
- Cuando el sync detecte que la existencia pasa **de 0 a más de 0**, mandar un webhook a `POST {frontend}/api/webhooks/product-back-in-stock`:
  ```json
  {
    "email": "cliente@example.com",
    "clientName": "Juan",
    "product": { "sicarUuid": "...", "sku": "...", "name": "...", "imageUrl": "...", "price": 130.0, "stock": 4 },
    "unsubscribeToken": "..."
  }
  ```
  - `clientName` es `null` para invitados.
  - Se dispara **una sola vez** por suscripción y después la suscripción se marca como usada.
  - Las suscripciones caducan a los 90 días.
  - Si la existencia vuelve a 0 antes de que se envíe, no se manda nada.

## 4. Aviso de baja de precio en favoritos

**Para qué:** avisar por correo cuando baja el precio de un producto que la persona tiene en favoritos.

**Pedido:**
- Al sincronizar precios, si `price` baja **5 % o más** en un producto que está en favoritos o en una lista con nombre de alguna cuenta, acumular el cambio.
- Mandar **un webhook por cliente al día como máximo**, agrupado, a `POST {frontend}/api/webhooks/wishlist-price-drop`:
  ```json
  {
    "clientUuid": "...",
    "clientEmail": "...",
    "clientName": "...",
    "items": [
      { "product": { "sicarUuid": "...", "sku": "...", "name": "...", "imageUrl": "..." }, "oldPrice": 150.0, "newPrice": 120.0 }
    ],
    "unsubscribeToken": "..."
  }
  ```
- Respeta la preferencia `marketingEmails` (punto 6).
- No avisar más de una vez por el mismo producto y el mismo precio.

## 5. Carritos abandonados

**Para qué:** recordar por correo a quien dejó productos en el carrito.

**Pedido:**
- Job periódico, por ejemplo cada 30 minutos. Un carrito califica si:
  - pertenece a una **cuenta** (los anónimos e invitados no tienen correo);
  - tiene productos;
  - su `updatedAt` es de hace más de 2 horas;
  - no se creó ningún pedido de esa cuenta después de `updatedAt`.
- Webhook a `POST {frontend}/api/webhooks/cart-abandoned`:
  ```json
  {
    "clientUuid": "...",
    "clientEmail": "...",
    "clientName": "...",
    "items": [ { "sicarUuid": "...", "name": "...", "imageUrl": "...", "price": 130.0, "quantity": 1, "available": true } ],
    "subtotal": 130.0,
    "unsubscribeToken": "..."
  }
  ```
- Como máximo un correo por carrito cada 7 días. Si el carrito cambia después del correo, no se vuelve a mandar hasta que pasen los 7 días.
- Respeta `marketingEmails` (punto 6).
- **Opcional, recomendado:** recordatorio de un **pedido apartado sin pagar**, para cuentas e invitados (el pedido sí tiene correo). Webhook `order-payment-pending` con `{ orderId, clientEmail, clientName, total, expiresAt }`, una vez, unos 30 minutos antes de que expire la reserva. Es transaccional, así que no depende de `marketingEmails`.

## 6. Preferencias de correo de marketing y bajas

**Para qué:** los puntos 4 y 5 son correos de marketing y necesitan una forma de darse de baja. El aviso de reabastecimiento (punto 3) lo pidió la persona, así que solo necesita su propia baja.

**Pedido:**
- `GET /v1/auth/me` y `PATCH /v1/auth/me`: nuevo campo `marketingEmails: boolean`. Valor por defecto a definir (sugerencia: `true` para cuentas nuevas, con aviso en el registro).
- `POST /v1/notifications/unsubscribe` con body `{ "token": "..." }`:
  - Sin sesión; el token viene en el enlace del correo.
  - Responde `{ "scope": "marketing" | "stock-alert", "email": "c***@example.com" }` para que el frontend muestre una confirmación.
  - Tokens firmados, de un solo propósito y sin caducidad corta.

## 7. Datos del pedido para el evento `purchase` de GA4

**Problema detectado:** `OrderItem` (en `GET /v1/auth/me/orders/{uuid}`, `GET /v1/orders/guest/{id}` y lo que consulta la página de éxito) solo trae `uuid`, `sku`, `description`, `quantity` (como string), `unit` e `imageUrl`. Le faltan el **precio unitario** y el **uuid del producto**, que GA4 necesita en `items[]` (`item_id`, `price`, `quantity`).

**Pedido:**
- Agregar a cada renglón de `items[]`:
  - `productUuid` (el `sicarUuid` del producto);
  - `unitPrice: number`, con IVA, lo que realmente se cobró;
  - `lineTotal: number`.
- Ya vienen `total`, `subtotal`, `discountAmount` y `couponCode`; confirmar que vienen igual en el endpoint de invitado.
- Opcional: `quantity` como número en una versión futura. El frontend tolera el string actual.

## 8. Confirmar si `price` incluye IVA

El frontend ahora muestra "Precios con IVA incluido". El dueño confirmó que los precios de SICAR ya traen IVA.

Pero el ejemplo de `FRONTEND_INTEGRATION.md` usa `"price": 8.62069`, que multiplicado por 1.16 da exactamente 10.00. Eso parece un precio **antes** de IVA.

**Pregunta:** ¿`price` (en catálogo, carrito y pedido) es el precio final al público con IVA, o el precio base? Si es base, hay que corregirlo antes de seguir con ofertas (punto 1).

## 9. Opcional: reintentos de webhooks

Los webhooks nuevos (3, 4 y 5) no son críticos para el cobro, pero si se pierde uno se pierde el correo. Sugerencia:
- 3 reintentos con backoff (1 min, 10 min, 1 h) cuando la respuesta no sea `2xx`.
- Encabezado `X-Webhook-Id` para que el frontend descarte duplicados.

---

### Lo que hará el frontend cuando esto exista
- **Punto 1:** precio tachado y porcentaje en tarjetas, ficha y carrito, y filtro "En oferta" en el catálogo.
- **Punto 2:** cambiar la fuente de "También te puede servir" al endpoint nuevo; solo cambia `lib/api.ts`.
- **Puntos 3 a 6:** rutas `app/api/webhooks/{product-back-in-stock,wishlist-price-drop,cart-abandoned,order-payment-pending}` y plantillas en `emails/`. También el botón "Avísame cuando llegue" en productos agotados, la página de baja `/notificaciones/baja` y el interruptor de correos de marketing en "Mi cuenta".
- **Punto 7:** `items[]` completos en el evento `purchase` de GA4. Mientras tanto se manda el total y el sku sin precio unitario.
