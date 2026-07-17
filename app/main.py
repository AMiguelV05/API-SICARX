import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi import _rate_limit_exceeded_handler
from app.core.rate_limit import limiter
from app.api.v1_router import v1_router

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

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

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
    allow_headers=["Content-Type", "Authorization", "x-api-key", "X-Client-Token"],
)

# Toda la API vive bajo /v1 (ver app/api/v1_router.py) -- cada sub-router ya trae su
# propio tags=/dependencies=, asi que aqui solo se incluye el wrapper.
app.include_router(v1_router)

@app.get("/", summary="Health check", tags=["Health"])
def read_root():
    return {"mensaje": "API intermedia funcionando correctamente"}