from slowapi import Limiter
from slowapi.util import get_remote_address

# Limiter en memoria por IP -- suficiente para un solo proceso `api` (no hay multiples
# instancias horizontales de este servicio en Railway hoy). Si eso cambia, este limiter
# necesitaria un backend compartido (p. ej. Redis) para que el conteo sea consistente
# entre instancias.
limiter = Limiter(key_func=get_remote_address)
