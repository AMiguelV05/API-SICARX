import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import products, orders, sessions, taxonomy, search, auth, addresses

logging.basicConfig(
    filename="app.log",
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

app = FastAPI(
    title="API Integración Sicar X",
    description="Capa intermedia para el e-commerce",
    contact={"name": "Ferretería Charly"},
    version="1.0.0"
)

origins = [
    "http://localhost",
    "http://localhost:8000",
    "https://ferreteriacharly.com",
    "https://api-production-cf7a.up.railway.app"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "x-api-key"],
)

# Router para conseguir detalles de productos desde Sicar X y guardarlos en la base de datos local
app.include_router(products.router,
                   tags=["Products Catalog and Details"])

# Router para crear pedidos en Sicar X y descontar stock local
app.include_router(orders.router,
                   tags=["Orders Creation and Cancellation"])

# Router para inicializar o refrescar la sesión con Sicar X
app.include_router(sessions.router)

# Router para departamentos y categorías (filtros del frontend)
app.include_router(taxonomy.router,
                   tags=["Taxonomy"])

# Router para busqueda de productos por sku o nombre
app.include_router(search.router,
                   tags=["Search"])

# Router para registro e inicio de sesión de cuentas de cliente
app.include_router(auth.router,
                   tags=["Client Auth"])

# Router para el libro de direcciones guardadas de cada cliente
app.include_router(addresses.router)

@app.get("/", summary="Health check", tags=["Health"])
def read_root():
    return {"mensaje": "API intermedia funcionando correctamente"}