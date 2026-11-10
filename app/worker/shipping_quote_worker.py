"""Borra cotizaciones de envio del checkout con mas de 30 dias (shipping_quotes). Los pedidos
guardan su propia copia en Order.shipping_quote - ver shipping_quote_service.py."""
import logging

from app.core.database import AsyncSessionLocal
from app.core.error_tracking import capture_exception
from app.services.shipping_quote_service import prune_old_quotes

logger = logging.getLogger(__name__)


async def scheduled_shipping_quote_prune_job() -> None:
    try:
        async with AsyncSessionLocal() as session:
            deleted = await prune_old_quotes(session)
        if deleted:
            logger.info(f"Cotizaciones de envio viejas borradas: {deleted}.")
    except Exception as e:
        logger.error(f"Fallo en la tarea programada de limpieza de cotizaciones de envio: {e!r}")
        capture_exception(e, job="shipping_quote_prune")
