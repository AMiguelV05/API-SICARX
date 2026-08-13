import logging
import uuid
from contextvars import ContextVar
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi import _rate_limit_exceeded_handler
from app.core.config import settings
from app.core.rate_limit import limiter
from app.api.v1_router import v1_router

# Contextvar + logging.Filter para correlacionar todas las lineas de log de una misma
# peticion en app.log (el worker tiene su propio sync.log aparte, no cubierto por esto -
# ver CLAUDE.md sobre los dos procesos con logging independiente).
request_id_ctx_var: ContextVar[str] = ContextVar("request_id", default="-")

class _RequestIdLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx_var.get()
        return True

_log_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - [%(request_id)s] - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
_file_handler = logging.FileHandler("app.log")
_file_handler.setFormatter(_log_formatter)
_file_handler.addFilter(_RequestIdLogFilter())
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_log_formatter)
_stream_handler.addFilter(_RequestIdLogFilter())

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])

logger = logging.getLogger(__name__)

app = FastAPI(
    title="API Integración Sicar X",
    description="Capa intermedia para el e-commerce",
    contact={"name": "Ferretería Charly"},
    version="1.0.0"
)

if not settings.ADMIN_API_KEY:
    logger.warning(
        "ADMIN_API_KEY no esta configurada: todas las rutas /v1/admin/* responderan 401 "
        "hasta que se configure (ver CLAUDE.md, seccion Admin API)."
    )

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI/Pydantic devuelven por defecto `detail=[{loc,msg,type}, ...]` en un 422 -
    distinto a `detail=<string>`, que es lo que usa cada `HTTPException` explicito en este
    codebase (confirmado por grep: ningun `HTTPException` manda un `detail` dict/lista).
    Normaliza a la misma forma para que el frontend pueda tratar `error.detail` como texto
    de forma uniforme, sin un caso especial solo para 422."""
    messages = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"] if p != "body")
        msg = err.get("msg", "Entrada inválida")
        messages.append(f"{loc}: {msg}" if loc else msg)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "; ".join(messages) or "Entrada inválida."},
    )

@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Asigna un X-Request-ID (del cliente o generado aqui) a cada peticion, lo expone en
    la respuesta e inyecta cada linea de log via `request_id_ctx_var` - para grepear
    app.log por una sola peticion en vez de correlacionar por timestamp."""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    token = request_id_ctx_var.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_ctx_var.reset(token)
    response.headers["X-Request-ID"] = request_id
    return response

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
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "x-api-key", "X-Client-Token", "Idempotency-Key", "X-Admin-Key"],
)

# Toda la API vive bajo /v1 (ver app/api/v1_router.py) -- cada sub-router ya trae su
# propio tags=/dependencies=, asi que aqui solo se incluye el wrapper.
app.include_router(v1_router)

@app.get("/", summary="Health check", tags=["Health"])
def read_root():
    return {"mensaje": "API intermedia funcionando correctamente"}