from fastapi import APIRouter
from app.api.routes import products, orders, taxonomy, search, vehicles, auth, addresses, client_orders, cart, payments, reviews, wishlist, admin, admin_categories, admin_vehicles, admin_bulk_import, admin_attributes, admin_attribute_presets, admin_variant_groups, admin_products, admin_dashboard, admin_reviews, admin_coupons, admin_auth, admin_admins, admin_audit

# Agrupa toda la API bajo /v1. Cada sub-router ya declara su propio tags=/dependencies=
# en su archivo -- no se repiten aqui para evitar que las listas de tags se dupliquen
# al anidar routers.
v1_router = APIRouter(prefix="/v1")

v1_router.include_router(products.router)
v1_router.include_router(orders.router)
v1_router.include_router(taxonomy.router)
v1_router.include_router(search.router)
v1_router.include_router(vehicles.router)
v1_router.include_router(auth.router)
v1_router.include_router(addresses.router)
v1_router.include_router(client_orders.router)
v1_router.include_router(cart.router)
v1_router.include_router(payments.router)
v1_router.include_router(reviews.router)
v1_router.include_router(wishlist.router)
# /admin/* - interno, no forma parte del contrato del frontend. Gateado por
# get_current_admin (JWT de AdminUser, Authorization: Bearer) via router-level
# dependencies=[Depends(get_current_admin)] en cada sub-router, no validate_api_key -
# ver app/core/security.py. admin_auth.router es la excepcion (login en si mismo no
# puede requerir el token que emite).
v1_router.include_router(admin_auth.router)
v1_router.include_router(admin_admins.router)
v1_router.include_router(admin_audit.router)
v1_router.include_router(admin.router)
v1_router.include_router(admin_categories.router)
v1_router.include_router(admin_coupons.router)
v1_router.include_router(admin_vehicles.router)
v1_router.include_router(admin_bulk_import.router)
v1_router.include_router(admin_attributes.router)
v1_router.include_router(admin_attribute_presets.router)
v1_router.include_router(admin_variant_groups.router)
v1_router.include_router(admin_products.router)
v1_router.include_router(admin_dashboard.router)
v1_router.include_router(admin_reviews.router)
