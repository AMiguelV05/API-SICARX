import logging

import sentry_sdk

from app.core.config import settings

logger = logging.getLogger("error_tracking")


def init_error_tracking(service_name: str) -> None:
    """Llamar una vez por proceso, en el arranque de cada uno (app/main.py para `api`,
    app/worker/sync_task.py para `worker` - son procesos separados, cada uno se inicializa
    por su cuenta). No-op si SENTRY_DSN no esta configurado - capture_exception/
    capture_message siguen comportandose exactamente igual que antes (solo loggean)."""
    if not settings.SENTRY_DSN:
        return
    sentry_sdk.init(dsn=settings.SENTRY_DSN, environment=settings.ENVIRONMENT)
    sentry_sdk.set_tag("service", service_name)


def capture_exception(exc: Exception, **context) -> None:
    """Punto unico para reportar una excepcion a un sistema externo (Sentry). Siempre loggea
    (comportamiento original, sin cambios); ademas reporta a Sentry si init_error_tracking()
    configuro un DSN - sentry_sdk.capture_exception es inerte/no-op si no se llamo init()."""
    logger.error(f"capture_exception: {type(exc).__name__}: {exc!r} | context={context}", exc_info=exc)
    sentry_sdk.set_context("extra", context)
    sentry_sdk.capture_exception(exc)


def capture_message(message: str, level: str = "error", **context) -> None:
    """Equivalente de capture_exception para un evento sin excepcion asociada (p. ej. un
    outbox row que agoto MAX_ATTEMPTS)."""
    log_fn = getattr(logger, level, logger.error)
    log_fn(f"capture_message: {message} | context={context}")
    sentry_sdk.set_context("extra", context)
    sentry_sdk.capture_message(message, level=level)
