import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.cart import Cart
from app.models.client import ClientAccount
from app.models.product import Product
from app.schemas.cart import CartItemPublic, CartResponse
from app.schemas.orders import ProductItem
from app.services.order_service import _to_decimal

logger = logging.getLogger(__name__)

async def _enrich(db: AsyncSession, raw_items: list) -> tuple[list[CartItemPublic], float, float]:
    """Enriquece lineas guardadas (uuid+quantity) con datos frescos de Product - precio/nombre/stock nunca se guardan en el carrito."""
    uuids = [item.get("uuid") for item in raw_items if item.get("uuid")]
    products_by_uuid = {}
    if uuids:
        result = await db.execute(select(Product).where(Product.sicar_uuid.in_(uuids)))
        products_by_uuid = {p.sicar_uuid: p for p in result.scalars().all()}

    enriched = []
    subtotal_decimal = Decimal("0")
    total_quantity = 0.0
    for raw in raw_items:
        product_uuid = raw.get("uuid")
        quantity = float(raw.get("quantity", 0))
        product = products_by_uuid.get(product_uuid)
        available = product is not None and product.is_active and not product.is_deleted

        total_quantity += quantity
        if available:
            line_total_decimal = _to_decimal(product.price) * _to_decimal(quantity)
            subtotal_decimal += line_total_decimal
            enriched.append(CartItemPublic(
                productUuid=product_uuid,
                sku=product.sku,
                name=product.name,
                imageUrl=product.image_url,
                price=float(product.price),
                stock=product.available_stock,
                quantity=quantity,
                lineTotal=float(line_total_decimal),
                available=True,
            ))
        else:
            enriched.append(CartItemPublic(
                productUuid=product_uuid,
                quantity=quantity,
                available=False,
            ))

    subtotal = float(subtotal_decimal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return enriched, subtotal, total_quantity

async def get_cart_response(db: AsyncSession, cart: Optional[Cart]) -> CartResponse:
    """Ningun GET crea una fila - si no hay carrito, se responde un carrito vacio."""
    if cart is None:
        return CartResponse(items=[], subtotal=0.0, totalQuantity=0.0, cartToken=None, updatedAt=None)

    enriched, subtotal, total_quantity = await _enrich(db, cart.items or [])
    # cartToken siempre se deriva del carrito resuelto, nunca hace eco de lo que mando el cliente.
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
        existing_cart = await _lock_cart(db, existing_cart.id)
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

async def _lock_cart(db: AsyncSession, cart_id: int) -> Cart:
    """SELECT...FOR UPDATE antes de mutar `items` - sin esto, dos requests concurrentes sobre el mismo carrito producen un lost update."""
    result = await db.execute(
        select(Cart).where(Cart.id == cart_id).with_for_update().execution_options(populate_existing=True)
    )
    return result.scalar_one()

async def clear_cart(db: AsyncSession, cart: Optional[Cart]) -> None:
    if cart is None:
        return
    await db.delete(cart)
    await db.commit()

async def _reparent_or_merge(db: AsyncSession, client: ClientAccount, anon_cart: Cart) -> Cart:
    """Nucleo compartido entre /cart/merge y el merge tolerante de login/registro: reasigna o suma cantidades linea por linea. No resuelve el token - eso es responsabilidad del llamador."""
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
    """Fusiona un carrito anonimo en el de la cuenta autenticada. No encontrarlo es 404 (a diferencia del PUT anonimo, que crea uno en silencio) - aqui un exito silencioso enmascararia un bug real."""
    anon_cart = await db.scalar(
        select(Cart).where(Cart.uuid == cart_token, Cart.client_account_id.is_(None))
    )
    if not anon_cart:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Carrito no encontrado.")

    result_cart = await _reparent_or_merge(db, client, anon_cart)
    logger.info(f"Carrito anonimo {cart_token} fusionado a la cuenta {client.email}.")
    return await get_cart_response(db, result_cart)

async def try_merge_cart_token(db: AsyncSession, client: ClientAccount, cart_token: Optional[str]) -> Optional[Cart]:
    """Version tolerante para login/registro: un cart_token invalido/ausente se ignora (logueado) en vez de fallar el login. Devuelve None si no fusiono nada."""
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
    """Incrementa/decrementa una linea por delta; cantidad <=0 la elimina. Sin carrito+delta<=0, o producto ausente+delta<=0, es un no-op 200 (mismo idioma que GET/DELETE para la ausencia)."""
    if existing_cart is None:
        if delta <= 0:
            return await get_cart_response(db, None)
        cart = Cart(
            client_account_id=client.id if client else None,
            items=[{"uuid": product_uuid, "quantity": delta}],
        )
        db.add(cart)
    else:
        existing_cart = await _lock_cart(db, existing_cart.id)
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
        # Reasignacion completa (no in-place): la columna JSON no usa MutableList, SQLAlchemy solo detecta cambios de atributo completo.
        existing_cart.items = raw_items
        cart = existing_cart

    await db.commit()
    await db.refresh(cart)
    return await get_cart_response(db, cart)
