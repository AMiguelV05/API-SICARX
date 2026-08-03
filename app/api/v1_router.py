from fastapi import APIRouter
from app.api.routes import products, orders, sessions, taxonomy, search, vehicles, auth, addresses, client_orders, cart, payments, admin, admin_categories, admin_vehicles, admin_bulk_import

# Agrupa toda la API bajo /v1. Cada sub-router ya declara su propio tags=/dependencies=
# en su archivo -- no se repiten aqui para evitar que las listas de tags se dupliquen
# al anidar routers.
v1_router = APIRouter(prefix="/v1")

v1_router.include_router(products.router)
v1_router.include_router(orders.router)
v1_router.include_router(sessions.router)
v1_router.include_router(taxonomy.router)
v1_router.include_router(search.router)
v1_router.include_router(vehicles.router)
v1_router.include_router(auth.router)
v1_router.include_router(addresses.router)
v1_router.include_router(client_orders.router)
v1_router.include_router(cart.router)
v1_router.include_router(payments.router)
# /admin/* - interno, no forma parte del contrato del frontend (ver FRONTEND_INTEGRATION.md,
# que deliberadamente no lo documenta). Gateado por validate_admin_key (X-Admin-Key), no
# validate_api_key - ver app/core/security.py y CLAUDE.md, seccion "Admin API".
v1_router.include_router(admin.router)
v1_router.include_router(admin_categories.router)
v1_router.include_router(admin_vehicles.router)
v1_router.include_router(admin_bulk_import.router)
