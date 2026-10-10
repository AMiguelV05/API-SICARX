"""Drena webhook_outbox: envia cada webhook pendiente al frontend y reintenta con backoff
(1 min, 10 min, 1 h) ante cualquier respuesta que no sea 2xx o error de red; tras el 4.o
intento fallido la fila queda FAILED. Ver app/models/webhook_outbox.py y CLAUDE.md,
"Notificaciones salientes con outbox"."""
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.error_tracking import capture_exception, capture_message
from app.core.webhook_client import WEBHOOK_TIMEOUT
from app.models.webhook_outbox import WebhookOutbox
from app.services.webhook_outbox_service import build_signed_request

logger = logging.getLogger(__name__)

# Espera antes del reintento n (1-indexado); agotada la lista, la fila queda FAILED.
RETRY_DELAYS = [timedelta(minutes=1), timedelta(minutes=10), timedelta(hours=1)]
MAX_ROWS_PER_TICK = 50
STALE_LEASE_MINUTES = 5


async def _claim_next_row(session: AsyncSession) -> WebhookOutbox | None:
    """Mismo claim atomico que sicar_sync_worker._claim_next_row: el lock nunca se sostiene
    durante el HTTP."""
    now = datetime.now(timezone.utc)
    claimable = (
        select(WebhookOutbox.id)
        .where(or_(
            and_(WebhookOutbox.status == "PENDING", WebhookOutbox.next_attempt_at <= now),
            and_(WebhookOutbox.status == "IN_PROGRESS", WebhookOutbox.updated_at < now - timedelta(minutes=STALE_LEASE_MINUTES)),
        ))
        .order_by(WebhookOutbox.next_attempt_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(
        update(WebhookOutbox)
        .where(WebhookOutbox.id == claimable.scalar_subquery())
        .values(status="IN_PROGRESS", updated_at=now)
        .returning(WebhookOutbox)
    )
    row = result.scalars().first()
    await session.commit()
    return row


async def _send(row: WebhookOutbox) -> str | None:
    """None si el frontend respondio 2xx; si no, la descripcion del error."""
    url, raw_body, headers = build_signed_request(row)
    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as client:
            response = await client.post(url, content=raw_body, headers=headers)
    except httpx.HTTPError as e:
        return f"Error de red: {type(e).__name__}: {e!r}"
    if 200 <= response.status_code < 300:
        return None
    return f"HTTP {response.status_code}: {response.text[:500]}"


async def _process_claimed_row(row_id: int) -> None:
    async with AsyncSessionLocal() as session:
        row = await session.get(WebhookOutbox, row_id)
        if row is None or row.status != "IN_PROGRESS":
            return
        error = await _send(row)
        if error is None:
            row.status = "SUCCEEDED"
            row.last_error = None
            logger.info(f"Webhook {row.event} {row.uuid} enviado (intento {row.attempts + 1}).")
        else:
            row.attempts += 1
            row.last_error = error[:2000]
            if row.attempts > len(RETRY_DELAYS):
                row.status = "FAILED"
                logger.error(f"Webhook {row.event} {row.uuid} agotado tras {row.attempts} intentos: {error}")
                capture_message(f"Webhook {row.event} agotado tras {row.attempts} intentos", webhook_id=row.uuid, last_error=row.last_error)
            else:
                row.status = "PENDING"
                row.next_attempt_at = datetime.now(timezone.utc) + RETRY_DELAYS[row.attempts - 1]
                logger.warning(f"Webhook {row.event} {row.uuid} fallo (intento {row.attempts}), se reintentara: {error}")
        await session.commit()


async def drain_webhook_outbox() -> int:
    """Hasta MAX_ROWS_PER_TICK filas, cada una en su propia transaccion corta."""
    sent = 0
    for _ in range(MAX_ROWS_PER_TICK):
        async with AsyncSessionLocal() as session:
            row = await _claim_next_row(session)
        if row is None:
            break
        await _process_claimed_row(row.id)
        sent += 1
    return sent


async def scheduled_webhook_outbox_job() -> None:
    try:
        await drain_webhook_outbox()
    except Exception as e:
        logger.error(f"Fallo en la tarea programada del outbox de webhooks: {e!r}")
        capture_exception(e, job="webhook_outbox")
