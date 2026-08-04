from slowapi import Limiter
from slowapi.util import get_remote_address

# En memoria por IP - valido solo mientras `api` corra como una sola instancia;
# necesitaria un backend compartido (p. ej. Redis) si eso cambia.
limiter = Limiter(key_func=get_remote_address)
