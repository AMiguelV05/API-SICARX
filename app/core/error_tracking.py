import logging

logger = logging.getLogger("error_tracking")


def capture_exception(exc: Exception, **context) -> None:
    """Punto unico para reportar una excepcion a un sistema externo (Sentry u otro).
    Implementacion por defecto: solo loggea (comportamiento actual, sin cambios) - un
    proveedor real se conecta cambiando solo este modulo, sin tocar ningun call site."""
    logger.error(f"capture_exception: {type(exc).__name__}: {exc!r} | context={context}", exc_info=exc)


def capture_message(message: str, level: str = "error", **context) -> None:
    """Equivalente de capture_exception para un evento sin excepcion asociada (p. ej. un
    outbox row que agoto MAX_ATTEMPTS)."""
    log_fn = getattr(logger, level, logger.error)
    log_fn(f"capture_message: {message} | context={context}")
