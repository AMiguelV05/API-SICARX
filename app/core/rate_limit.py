import hmac

from fastapi import Request
from slowapi import Limiter
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app.core.config import settings

# Clave de las llamadas que el frontend hace desde su propio servidor sin un visitante detras
# (renders SSR/ISR, admin). Ver FrontendAwareSlowAPIMiddleware.
FRONTEND_SSR_KEY = "frontend-ssr"


def _is_trusted_frontend(request: Request) -> bool:
    secret = settings.FRONTEND_PROXY_SECRET
    if not secret:
        return False
    received = request.headers.get("X-Frontend-Auth")
    return bool(received) and hmac.compare_digest(received.encode(), secret.encode())


def get_client_ip(request: Request) -> str:
    """IP real del cliente para rate limiting por IP.

    Casi todo el trafico llega del servidor del frontend (la x-api-key nunca sale al navegador),
    asi que la IP que se conecta es la del frontend, no la del visitante: en un host con una sola
    IP de salida (Railway) todos los usuarios compartirian un mismo limite. Por eso, si la
    peticion trae X-Frontend-Auth con FRONTEND_PROXY_SECRET, se usa X-Client-IP (la IP del
    visitante que el frontend ya resolvio) - y si no trae X-Client-IP es un render del propio
    frontend sin visitante (FRONTEND_SSR_KEY). X-Client-IP sin el secreto se ignora: cualquiera
    puede mandar esa cabecera.

    Sin secreto valido: el ULTIMO valor de `X-Forwarded-For`, no el primero - el proxy de Railway
    agrega (append) la IP real del cliente al final de la cadena en vez de reemplazarla, asi que un
    cliente que intente falsificar la cabecera solo puede insertarse IPs falsas *antes* del valor
    que Railway mismo agrego. `request.client.host` seria la IP del proxy de Railway para *todas*
    las peticiones.
    """
    if _is_trusted_frontend(request):
        client_ip = (request.headers.get("X-Client-IP") or "").strip()
        return client_ip or FRONTEND_SSR_KEY

    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        ip = forwarded_for.split(",")[-1].strip()
        if ip:
            return ip
    return get_remote_address(request)


class FrontendAwareSlowAPIMiddleware(SlowAPIMiddleware):
    """SlowAPIMiddleware que no aplica los default_limits a los renders del propio frontend
    (FRONTEND_SSR_KEY): un solo bucket compartido por todos los renders SSR/ISR se agotaria con
    cualquier pico o crawler, y esas llamadas ya estan acotadas por el cache del frontend. slowapi
    0.1.x no tiene un `default_limits_exempt_when`, de ahi la subclase. Las rutas con su propio
    @limiter.limit(...) se validan en el decorador, no aqui, asi que siguen aplicando - el frontend
    les manda siempre X-Client-IP."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if get_client_ip(request) == FRONTEND_SSR_KEY:
            return await call_next(request)
        return await super().dispatch(request, call_next)


# En memoria por IP - valido solo mientras `api` corra como una sola instancia;
# necesitaria un backend compartido (p. ej. Redis) si eso cambia.
#
# default_limits: red de seguridad para CUALQUIER ruta sin su propio @limiter.limit(...) -
# antes solo orders/payments/auth/admin_auth tenian limite, dejando todo el catalogo/busqueda
# publico (incluido GET /products/{uuid}, que puede disparar una llamada GraphQL en vivo a
# Sicar X) y casi todo /v1/admin/* sin ningun limite, protegidos solo por la x-api-key
# estatica (que vive en el propio frontend, no es un secreto real) o el JWT de AdminUser.
# SlowAPIMiddleware aplica este default automaticamente a cualquier ruta SIN decorador propio
# (una ruta con @limiter.limit(...) queda exenta del default y solo obedece su propio limite,
# mas estricto - ver slowapi.middleware._should_exempt) asi que esto no cambia el
# comportamiento de las rutas que ya tenian su propio limite (10-60/min).
limiter = Limiter(key_func=get_client_ip, default_limits=["120/minute"])
