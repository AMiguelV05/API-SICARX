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

    client_cart = await db.scalar(select(Cart).where(Cart.client_account_id == client.id))

    if client_cart is None:
        # Sin carrito propio todavia: simplemente se reasigna el carrito anonimo a la cuenta.
        anon_cart.client_account_id = client.id
        await db.commit()
        await db.refresh(anon_cart)
        result_cart = anon_cart
    else:
        quantities: dict[str, float] = {}
        for raw in (client_cart.items or []):
            quantities[raw["uuid"]] = quantities.get(raw["uuid"], 0) + float(raw["quantity"])
        for raw in (anon_cart.items or []):
            quantities[raw["uuid"]] = quantities.get(raw["uuid"], 0) + float(raw["quantity"])

        client_cart.items = [{"uuid": u, "quantity": q} for u, q in quantities.items()]
        await db.delete(anon_cart)
        await db.commit()
        await db.refresh(client_cart)
        result_cart = client_cart

    logger.info(f"Carrito anonimo {cart_token} fusionado a la cuenta {client.email}.")
    return await get_cart_response(db, result_cart)
