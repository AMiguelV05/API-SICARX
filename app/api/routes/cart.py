import logging
from typing import Annotated, Optional
from fastapi import APIRouter, Depends, Body, Cookie, Response, status
from sqlalchemy import select
from app.core.database import DbDep
from app.core.security import validate_api_key, OptionalClientHeaderDep, CurrentClientDep
from app.core.cookies import CART_COOKIE_NAME, set_cart_cookie, clear_cart_cookie
from app.models.client import ClientAccount
from app.models.cart import Cart
from app.schemas.cart import CartReplace, CartMergeRequest, CartItemDelta, CartResponse
from app.services.cart_service import get_cart_response, replace_cart, clear_cart, merge_cart, adjust_cart_item

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cart", tags=["Cart"], dependencies=[Depends(validate_api_key)])

CartContext = tuple[Optional[ClientAccount], Optional[Cart]]

async def get_cart_context(
    db: DbDep,
    client: OptionalClientHeaderDep,
    cart_token: Optional[str] = Cookie(default=None, alias=CART_COOKIE_NAME, description="Token del carrito anonimo (cookie httpOnly, opcional, ignorado si hay X-Client-Token valido)"),
) -> CartContext:
    """
    Resuelve la identidad del carrito: cuenta autenticada (X-Client-Token) tiene prioridad;
    si no hay cuenta, se busca el carrito anonimo por la cookie del carrito; si tampoco hay
    eso, no hay carrito resuelto (anonimo sin identidad todavia).
    """
    if client is not None:
        cart = await db.scalar(select(Cart).where(Cart.client_account_id == client.id))
        return client, cart
    if cart_token:
        cart = await db.scalar(select(Cart).where(Cart.uuid == cart_token, Cart.client_account_id.is_(None)))
        return None, cart
    return None, None

CartContextDep = Annotated[CartContext, Depends(get_cart_context)]

@router.get("", response_model=CartResponse, summary="Obtener el carrito actual")
async def get_cart(db: DbDep, ctx: CartContextDep):
    """
    Devuelve el carrito resuelto por identidad (cuenta o cookie del carrito). No crea nada -
    si no hay carrito, responde uno vacio. Los precios/stock/nombre siempre se leen en vivo
    del catalogo local, nunca se guardan en el carrito.
    """
    _, cart = ctx
    return await get_cart_response(db, cart)

@router.put("", response_model=CartResponse, summary="Reemplazar el carrito completo")
async def put_cart(db: DbDep, ctx: CartContextDep, response: Response, data: CartReplace = Body()):
    """
    Reemplaza por completo el contenido del carrito resuelto por identidad. Si es anonimo y
    no hay carrito resuelto (falta la cookie o no se reconoce), se crea uno nuevo en
    silencio y su uuid se devuelve/emite como cookie httpOnly para que el navegador la guarde.
    """
    client, cart = ctx
    result = await replace_cart(db, client, cart, data.items)
    if result.cartToken:
        set_cart_cookie(response, result.cartToken)
    return result

@router.patch("/items", response_model=CartResponse, summary="Incrementar o decrementar una linea del carrito")
async def patch_cart_item(db: DbDep, ctx: CartContextDep, response: Response, data: CartItemDelta = Body()):
    """
    Ajusta la cantidad de un solo producto sin necesitar el listado completo del carrito
    (a diferencia de PUT). `delta` positivo agrega/incrementa, negativo decrementa; si la
    cantidad resultante es <=0 la linea se elimina. Comparte identidad/mint-silencioso con PUT.
    """
    client, cart = ctx
    result = await adjust_cart_item(db, client, cart, data.productUuid, data.delta)
    if result.cartToken:
        set_cart_cookie(response, result.cartToken)
    return result

@router.delete("", status_code=status.HTTP_204_NO_CONTENT, summary="Vaciar el carrito")
async def delete_cart(db: DbDep, ctx: CartContextDep, response: Response):
    """Elimina el carrito resuelto por identidad. Si no hay carrito, no hace nada."""
    _, cart = ctx
    await clear_cart(db, cart)
    clear_cart_cookie(response)

@router.post("/merge", response_model=CartResponse, summary="Fusionar un carrito anonimo a la cuenta autenticada")
async def merge_cart_endpoint(client: CurrentClientDep, db: DbDep, response: Response, data: CartMergeRequest = Body()):
    """
    Fusiona un carrito anonimo (identificado por `cartToken`, el `uuid` que devolvio un PUT
    anterior sin sesion) al carrito de la cuenta ya autenticada (`Authorization`, igual que
    `/v1/auth/me/addresses`). Si la cuenta no tenia carrito, simplemente reclama el anonimo;
    si ya tenia uno, las cantidades de productos en comun se suman. `404` si `cartToken` no
    corresponde a un carrito anonimo existente.
    """
    result = await merge_cart(db, client, data.cartToken)
    clear_cart_cookie(response)
    return result
