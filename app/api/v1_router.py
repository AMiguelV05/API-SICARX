from fastapi import APIRouter
from app.api.routes import products, orders, sessions, taxonomy, search, auth, addresses, client_orders, cart, payments, sepomex

# Agrupa toda la API bajo /v1. Cada sub-router ya declara su propio tags=/dependencies=
# en su archivo -- no se repiten aqui para evitar que las listas de tags se dupliquen
# al anidar routers.
v1_router = APIRouter(prefix="/v1")

v1_router.include_router(products.router)
v1_router.include_router(orders.router)
v1_router.include_router(sessions.router)
v1_router.include_router(taxonomy.router)
v1_router.include_router(search.router)
v1_router.include_router(auth.router)
v1_router.include_router(addresses.router)
v1_router.include_router(client_orders.router)
v1_router.include_router(cart.router)
v1_router.include_router(payments.router)
v1_router.include_router(sepomex.router)
