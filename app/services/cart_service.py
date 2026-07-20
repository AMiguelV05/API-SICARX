import logging
from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.cart import Cart
from app.models.client import ClientAccount
from app.models.product import Product
from app.schemas.cart import CartItemPublic, CartResponse
from app.schemas.orders import ProductItem

logger = logging.getLogger(__name__)

async def _enrich(db: AsyncSession, raw_items: list) -> tuple[list[CartItemPublic], float, float]:
    """Enriquece las lineas guardadas (solo uuid+quantity) con datos en vivo de Product -
    precio/nombre/stock nunca se guardan en el carrito, siempre se leen frescos aqui."""
    uuids = [item.get("uuid") for item in raw_items if item.get("uuid")]
    products_by_uuid = {}
    if uuids:
        result = await db.execute(select(Product).where(Product.sicar_uuid.in_(uuids)))
        products_by_uuid = {p.sicar_uuid: p for p in result.scalars().all()}

    enriched = []
    subtotal = 0.0
    total_quantity = 0.0
    for raw in raw_items:
        product_uuid = raw.get("uuid")
        quantity = float(raw.get("quantity", 0))
        product = products_by_uuid.get(product_uuid)
        available = product is not None and product.is_active and not product.is_deleted

        total_quantity += quantity
        if available:
            line_total = float(product.price) * quantity
            subtotal += line_total
            enriched.append(CartItemPublic(
                productUuid=product_uuid,
                sku=product.sku,
                name=product.name,
                imageUrl=product.image_url,
                price=float(product.price),
                stock=product.stock,
                quantity=quantity,
                lineTotal=line_total,
                available=True,
            ))
        else:
            enriched.append(CartItemPublic(
                productUuid=product_uuid,
                quantity=quantity,
                available=False,
            ))

    return enriched, subtotal, total_quantity

async def get_cart_response(db: AsyncSession, cart: Optional[Cart]) -> CartResponse:
    """Ningun GET crea una fila - si no hay carrito, se responde un carrito vacio."""
    if cart is None:
        return CartResponse(items=[], subtotal=0.0, totalQuantity=0.0, cartToken=None, updatedAt=None)

    enriched, subtotal, total_quantity = await _enrich(db, cart.items or [])
    # cartToken siempre se deriva del uuid real del carrito resuelto, nunca se hace eco de lo
    # que mando el cliente - un X-Cart-Token obsoleto/no reconocido nunca se repite de vuelta.
    cart_token = cart.uuid if cart.client_account_id is None else None
    return CartResponse(
        items=enriched,
        subtotal=subtotal,
        totalQuantity=total_quantity,
        cartToken=cart_token,
        updatedAt=cart.updated_at or cart.created_at,
    )

async def replace_cart(
    db: AsyncSession,
    client: Optional[ClientAccount],
    existing_cart: Optional[Cart],
    items: list[ProductItem],
) -> CartResponse:
    raw_items = [{"uuid": item.uuid, "quantity": item.quantity} for item in items]

    if existing_cart is not None:
        existing_cart.items = raw_items
        cart = existing_cart
    else:
        cart = Cart(
            client_account_id=client.id if client else None,
            items=raw_items,
        )
        db.add(cart)

    await db.commit()
    await db.refresh(cart)

    logger.info(f"Carrito {cart.uuid} actualizado ({'cliente ' + str(cart.client_account_id) if cart.client_account_id else 'anonimo'}).")
    return await get_cart_response(db, cart)

async def clear_cart(db: AsyncSession, cart: Optional[Cart]) -> None:
    if cart is None:
        return
    await db.delete(cart)
    await db.commit()

async def _reparent_or_merge(db: AsyncSession, client: ClientAccount, anon_cart: Cart) -> Cart:
    """Nucleo compartido entre la fusion estricta (POST /cart/merge) y la tolerante (embebida
    en login/registro): dado un carrito anonimo ya resuelto, lo reasigna a la cuenta (si no
    tenia carrito propio) o suma cantidades linea por linea en el carrito existente,
    eliminando el anonimo ya consumido. No decide que hacer si el token no resuelve a nada -
    eso es responsabilidad de cada llamador."""
    client_cart = await db.scalar(select(Cart).where(Cart.client_account_id == client.id))

    if client_cart is None:
        anon_cart.client_account_id = client.id
        await db.commit()
        await db.refresh(anon_cart)
        return anon_cart

    quantities: dict[str, float] = {}
    for raw in (client_cart.items or []):
        quantities[raw["uuid"]] = quantities.get(raw["uuid"], 0) + float(raw["quantity"])
    for raw in (anon_cart.items or []):
        quantities[raw["uuid"]] = quantities.get(raw["uuid"], 0) + float(raw["quantity"])

    client_cart.items = [{"uuid": u, "quantity": q} for u, q in quantities.items()]
    await db.delete(anon_cart)
    await db.commit()
    await db.refresh(client_cart)
    return client_cart

async def merge_cart(db: AsyncSession, client: ClientAccount, cart_token: str) -> CartResponse:
    """Fusiona un carrito anonimo (identificado por su uuid/cartToken) en el carrito de la
    cuenta autenticada. No encontrarlo es un 404 (a diferencia del PUT anonimo, que crea uno
    nuevo en silencio) - aqui el llamador ya deberia tener un token real en la mano, asi que
    un exito silencioso enmascararia un bug real."""
    anon_cart = await db.scalar(
        select(Cart).where(Cart.uuid == cart_token, Cart.client_account_id.is_(None))
    )
    if not anon_cart:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Carrito no encontrado.")

    result_cart = await _reparent_or_merge(db, client, anon_cart)
    logger.info(f"Carrito anonimo {cart_token} fusionado a la cuenta {client.email}.")
    return await get_cart_response(db, result_cart)

async def try_merge_cart_token(db: AsyncSession, client: ClientAccount, cart_token: Optional[str]) -> Optional[Cart]:
    """Version tolerante para login/registro: un cart_token ausente o que no resuelve a un
    carrito anonimo real simplemente se ignora (se registra en el log) en vez de fallar el
    login/registro completo - el frontend no puede saber si un token guardado sigue siendo
    valido, y el login no debe volverse fragil por eso. Devuelve el Cart resultante si SI se
    fusiono algo, o None si no paso nada (para que el caller sepa si limpiar la cookie)."""
    if not cart_token:
        return None
    anon_cart = await db.scalar(
        select(Cart).where(Cart.uuid == cart_token, Cart.client_account_id.is_(None))
    )
    if not anon_cart:
        logger.info(f"cartToken '{cart_token}' en login/registro no resuelve a un carrito anonimo valido; se ignora.")
        return None
    result_cart = await _reparent_or_merge(db, client, anon_cart)
    logger.info(f"Carrito anonimo {cart_token} fusionado a la cuenta {client.email} durante login/registro.")
    return result_cart

async def adjust_cart_item(
    db: AsyncSession,
    client: Optional[ClientAccount],
    existing_cart: Optional[Cart],
    product_uuid: str,
    delta: float,
) -> CartResponse:
    """PATCH /cart/items: incrementa o decrementa una sola linea sin necesitar el listado
    completo del carrito. Cantidad resultante <=0 elimina la linea. Sin carrito + delta>0
    crea uno nuevo (mismo mint silencioso que PUT); sin carrito + delta<=0, o producto
    ausente + delta<=0, es un no-op (200 con el estado actual) - no es un error del llamador,
    es el mismo idioma que ya usan GET/DELETE de este recurso para la ausencia."""
    if existing_cart is None:
        if delta <= 0:
            return await get_cart_response(db, None)
        cart = Cart(
            client_account_id=client.id if client else None,
            items=[{"uuid": product_uuid, "quantity": delta}],
        )
        db.add(cart)
    else:
        raw_items = list(existing_cart.items or [])
        idx = next((i for i, it in enumerate(raw_items) if it.get("uuid") == product_uuid), None)
        if idx is None:
            if delta <= 0:
                return await get_cart_response(db, existing_cart)
            raw_items.append({"uuid": product_uuid, "quantity": delta})
        else:
            new_qty = float(raw_items[idx]["quantity"]) + delta
            if new_qty <= 0:
                raw_items.pop(idx)
            else:
                raw_items[idx] = {"uuid": product_uuid, "quantity": new_qty}
        # Reasignacion completa, no mutacion in-place: la columna JSON no esta envuelta en
        # MutableList, SQLAlchemy solo detecta el cambio si se reemplaza el atributo entero.
        existing_cart.items = raw_items
        cart = existing_cart

    await db.commit()
    await db.refresh(cart)
    return await get_cart_response(db, cart)
