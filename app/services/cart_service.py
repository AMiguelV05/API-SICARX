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
from app.services import coupon_service
from app.services.order_service import _to_decimal

logger = logging.getLogger(__name__)

async def _enrich(db: AsyncSession, raw_items: list) -> tuple[list[CartItemPublic], float, float, dict]:
    """Enriquece lineas guardadas (uuid+quantity) con datos frescos de Product - precio/nombre/stock nunca se guardan en el carrito. Tambien devuelve el mapa de Product resuelto (uuid->Product) para que get_cart_response lo reuse en el preview de cupon en vez de volver a consultar."""
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
    return enriched, subtotal, total_quantity, products_by_uuid

async def get_cart_response(db: AsyncSession, cart: Optional[Cart]) -> CartResponse:
    """Ningun GET crea una fila - si no hay carrito, se responde un carrito vacio. El
    descuento del cupon aplicado (si hay uno) se recalcula en vivo en cada lectura, mismo
    criterio que precio/stock - nunca se confia un monto guardado. Un cupon que ya no aplica
    (expirado, no alcanza el minimo, etc.) no falla la lectura: couponValid queda false y
    couponInvalidReason explica por que, mismo idioma que una linea con available=false."""
    if cart is None:
        return CartResponse(items=[], subtotal=0.0, totalQuantity=0.0, cartToken=None, updatedAt=None, total=0.0)

    enriched, subtotal, total_quantity, products_by_uuid = await _enrich(db, cart.items or [])

    coupon_valid = False
    discount_amount_decimal = Decimal("0")
    invalid_reason = None
    if cart.coupon_code:
        try:
            coupon = await coupon_service.get_coupon_by_code(db, cart.coupon_code)
            await coupon_service.validate_coupon_eligibility(db, coupon, cart.client_account_id, _to_decimal(subtotal))
            quantities = {item["uuid"]: item["quantity"] for item in (cart.items or []) if item.get("uuid")}
            scoped_subtotal = await coupon_service.compute_scoped_subtotal(db, coupon, products_by_uuid, quantities)
            discount_amount_decimal = coupon_service.compute_discount_amount(coupon, scoped_subtotal)
            coupon_valid = True
        except HTTPException as e:
            invalid_reason = e.detail

    # cartToken siempre se deriva del carrito resuelto, nunca hace eco de lo que mando el cliente.
    cart_token = cart.uuid if cart.client_account_id is None else None
    # Resta final en Decimal (no float) para no heredar error de representacion binaria en
    # un campo de dinero - mismo criterio que order_service._format_amount.
    total_decimal = max(_to_decimal(subtotal) - discount_amount_decimal, Decimal("0"))
    total_decimal = total_decimal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    discount_amount = float(discount_amount_decimal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return CartResponse(
        items=enriched,
        subtotal=subtotal,
        totalQuantity=total_quantity,
        cartToken=cart_token,
        updatedAt=cart.updated_at or cart.created_at,
        couponCode=cart.coupon_code,
        couponValid=coupon_valid,
        couponInvalidReason=invalid_reason,
        discountAmount=discount_amount,
        total=float(total_decimal),
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

async def apply_coupon_to_cart(db: AsyncSession, client: Optional[ClientAccount], cart: Optional[Cart], code: str) -> CartResponse:
    """Guarda el codigo en el carrito para que se prevea en cada GET - NO es la aplicacion
    autoritativa (esa solo ocurre en POST /orders, ver coupon_service.lock_and_validate_coupon).
    Se valida una vez aqui (sin lock de Coupon) solo para dar feedback inmediato (404/409) en
    vez de guardar en silencio un codigo que ya se sabe invalido."""
    if cart is None or not (cart.items or []):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El carrito está vacío.")

    cart = await _lock_cart(db, cart.id)
    _, subtotal, _, products_by_uuid = await _enrich(db, cart.items or [])
    coupon = await coupon_service.get_coupon_by_code(db, code)
    await coupon_service.validate_coupon_eligibility(db, coupon, client.id if client else None, _to_decimal(subtotal))

    cart.coupon_code = coupon.code
    await db.commit()
    await db.refresh(cart)
    logger.info(f"Cupón '{coupon.code}' aplicado al carrito {cart.uuid} ({'cliente ' + str(cart.client_account_id) if cart.client_account_id else 'anonimo'}).")
    return await get_cart_response(db, cart)

async def remove_coupon_from_cart(db: AsyncSession, cart: Optional[Cart]) -> CartResponse:
    """Tolerante: sin carrito o sin cupon aplicado es un no-op 200, mismo idioma que clear_cart con carrito ausente."""
    if cart is None or not cart.coupon_code:
        return await get_cart_response(db, cart)

    cart = await _lock_cart(db, cart.id)
    cart.coupon_code = None
    await db.commit()
    await db.refresh(cart)
    return await get_cart_response(db, cart)
