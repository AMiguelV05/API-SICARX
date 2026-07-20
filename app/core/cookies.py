from fastapi import Response
from app.core.config import settings

CART_COOKIE_NAME = "charly_cart_token"
CART_COOKIE_PATH = "/v1/cart"
CART_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # ~1 anio: Cart no tiene auto-expiracion por diseno,
                                            # una cookie de sesion contradiria eso silenciosamente.

def _is_prod() -> bool:
    return settings.ENVIRONMENT.lower() == "production"

def set_cart_cookie(response: Response, cart_uuid: str) -> None:
    """Emite/renueva la cookie httpOnly del carrito anonimo. SameSite=None+Secure en produccion
    (frontend y API estan en dominios distintos, es una cookie cross-site); en desarrollo
    SameSite=Lax+Secure=False para que siga siendo almacenable sobre http://localhost plano."""
    response.set_cookie(
        key=CART_COOKIE_NAME,
        value=cart_uuid,
        max_age=CART_COOKIE_MAX_AGE,
        httponly=True,
        secure=_is_prod(),
        samesite="none" if _is_prod() else "lax",
        path=CART_COOKIE_PATH,
    )

def clear_cart_cookie(response: Response) -> None:
    """path/secure/samesite deben coincidir exactamente con set_cart_cookie o el navegador
    no la reconoce como la misma cookie a borrar."""
    response.delete_cookie(
        key=CART_COOKIE_NAME,
        path=CART_COOKIE_PATH,
        secure=_is_prod(),
        samesite="none" if _is_prod() else "lax",
    )
