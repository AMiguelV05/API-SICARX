from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def get_client_ip(request: Request) -> str:
    """IP real del cliente para rate limiting por IP. Railway (unico deploy real de esta
    API - ver CLAUDE.md) siempre pone a esta app detras de su propio edge proxy, asi que
    `request.client.host` (usado antes via `get_remote_address` directo) es la IP del
    proxy de Railway para *todas* las peticiones, no la del cliente - el rate limit
    terminaba compartido entre todos los usuarios en vez de ser por-IP.

    Tomamos el ULTIMO valor de `X-Forwarded-For`, no el primero: el proxy de Railway
    agrega (append) la IP real del cliente al final de la cadena en vez de reemplazarla,
    asi que un cliente que intente falsificar la cabecera solo puede insertarse IPs falsas
    *antes* del valor que Railway mismo agrego - el ultimo valor es el unico que no puede
    ser controlado por quien hace la peticion.
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        ip = forwarded_for.split(",")[-1].strip()
        if ip:
            return ip
    return get_remote_address(request)


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
