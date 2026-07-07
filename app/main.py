import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import products, orders, sessions

logging.basicConfig(
    filename="app.log",
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

app = FastAPI(
    title="API Integración Sicar X",
    description="Capa intermedia para el e-commerce",
    authors=["Ferretería Charly"],
    version="1.0.0"
)

origins = [
    "http://localhost",
    "http://localhost:8000",
    "http://localhost:8000/docs",
    "https://ferreteriacharly.com/"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Router para conseguir detalles de productos desde Sicar X y guardarlos en la base de datos local
app.include_router(products.router,
                   tags=["Products Catalog and Details"])

# Router para crear pedidos en Sicar X y descontar stock local
app.include_router(orders.router,
                   tags=["Orders Creation and Cancellation"])

# Router para inicializar o refrescar la sesión con Sicar X
app.include_router(sessions.router)

@app.get("/")
def read_root():
    return {"mensaje": "API intermedia funcionando correctamente"}