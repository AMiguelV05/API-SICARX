import logging
from typing import Annotated, Optional
from fastapi import APIRouter, Depends, Body, Header, status
from sqlalchemy import select
from app.core.database import DbDep
from app.core.security import validate_api_key, OptionalClientHeaderDep, CurrentClientDep
from app.models.client import ClientAccount
from app.models.cart import Cart
from app.schemas.cart import CartReplace, CartMergeRequest, CartResponse
from app.services.cart_service import get_cart_response, replace_cart, clear_cart, merge_cart

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cart", tags=["Cart"], dependencies=[Depends(validate_api_key)])

CartContext = tuple[Optional[ClientAccount], Optional[Cart]]

async def get_cart_context(
    db: DbDep,
    client: OptionalClientHeaderDep,
    x_cart_token: str = Header(None, alias="X-Cart-Token", description="Token del carrito anonimo (opcional, ignorado si hay X-Client-Token valido)"),
) -> CartContext:
    """
    Resuelve la identidad del carrito: cuenta autenticada (X-Client-Token) tiene prioridad;
    si no hay cuenta, se busca el carrito anonimo por X-Cart-Token; si tampoco hay eso,
    no hay carrito resuelto (anonimo sin identidad todavia).
    """
    if client is not None:
        cart = await db.scalar(select(Cart).where(Cart.client_account_id == client.id))
        return client, cart
    if x_cart_token:
        cart = await db.scalar(select(Cart).where(Cart.uuid == x_cart_token, Cart.client_account_id.is_(None)))
        return None, cart
    return None, None

CartContextDep = Annotated[CartContext, Depends(get_cart_context)]

@router.get("", response_model=CartResponse, summary="Obtener el carrito actual")
async def get_cart(db: DbDep, ctx: CartContextDep):
    """
    Devuelve el carrito resuelto por identidad (cuenta o X-Cart-Token). No crea nada -
    si no hay carrito, responde uno vacio. Los precios/stock/nombre siempre se leen en vivo
    del catalogo local, nunca se guardan en el carrito.
    """
    _, cart = ctx
    return await get_cart_response(db, cart)

@router.put("", response_model=CartResponse, summary="Reemplazar el carrito completo")
async def put_cart(db: DbDep, ctx: CartContextDep, data: CartReplace = Body()):
    """
    Reemplaza por completo el contenido del carrito resuelto por identidad. Si es anonimo y
    no hay carrito resuelto (falta X-Cart-Token o no se reconoce), se crea uno nuevo en
    silencio y su uuid se devuelve como `cartToken` para que el frontend lo guarde.
    """
    client, cart = ctx
    return await replace_cart(db, client, cart, data.items)

@router.delete("", status_code=status.HTTP_204_NO_CONTENT, summary="Vaciar el carrito")
async def delete_cart(db: DbDep, ctx: CartContextDep):
    """Elimina el carrito resuelto por identidad. Si no hay carrito, no hace nada."""
    _, cart = ctx
    await clear_cart(db, cart)

@router.post("/merge", response_model=CartResponse, summary="Fusionar un carrito anonimo a la cuenta autenticada")
async def merge_cart_endpoint(client: CurrentClientDep, db: DbDep, data: CartMergeRequest = Body()):
    """
    Fusiona un carrito anonimo (identificado por `cartToken`, el `uuid` que devolvio un PUT
    anterior sin sesion) al carrito de la cuenta ya autenticada (`Authorization`, igual que
    `/v1/auth/me/addresses`). Si la cuenta no tenia carrito, simplemente reclama el anonimo;
    si ya tenia uno, las cantidades de productos en comun se suman. `404` si `cartToken` no
    corresponde a un carrito anonimo existente.
    """
    return await merge_cart(db, client, data.cartToken)
