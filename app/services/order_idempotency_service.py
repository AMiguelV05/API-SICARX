import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.order import OrderIdempotencyKey

logger = logging.getLogger(__name__)

# Reclamos abandonados no se limpian en segundo plano; se detectan al vuelo comparando antiguedad contra este umbral.
ABANDONED_CLAIM_THRESHOLD = timedelta(minutes=2)


async def claim_idempotency_key(
    db: AsyncSession, client_account_id: int | None, idempotency_key: str, guest_email: str | None = None,
) -> tuple[OrderIdempotencyKey, bool]:
    """Reclama `idempotency_key`; is_new=False si ya existia (el llamador decide segun si
    order_uuid ya esta poblado). Camino de invitado (`client_account_id=None`): la
    deduplicacion se basa en `(guest_email, idempotency_key)` via el indice unico parcial
    `ux_order_idempotency_guest_key` - la restriccion `(client_account_id, idempotency_key)`
    normal nunca detectaria una colision ahi, ya que Postgres trata NULL != NULL."""
    claim = OrderIdempotencyKey(client_account_id=client_account_id, idempotency_key=idempotency_key, guest_email=guest_email)
    db.add(claim)
    try:
        await db.commit()
        await db.refresh(claim)
        return claim, True
    except IntegrityError:
        await db.rollback()
        if client_account_id is not None:
            stmt = select(OrderIdempotencyKey).where(
                OrderIdempotencyKey.client_account_id == client_account_id,
                OrderIdempotencyKey.idempotency_key == idempotency_key,
            )
        else:
            stmt = select(OrderIdempotencyKey).where(
                OrderIdempotencyKey.client_account_id.is_(None),
                OrderIdempotencyKey.guest_email == guest_email,
                OrderIdempotencyKey.idempotency_key == idempotency_key,
            )
        result = await db.execute(stmt)
        return result.scalar_one(), False


def is_claim_abandoned(claim: OrderIdempotencyKey) -> bool:
    created_at = claim.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created_at > ABANDONED_CLAIM_THRESHOLD


async def discard_claim(db: AsyncSession, claim: OrderIdempotencyKey) -> None:
    """Borra un reclamo abandonado para permitir reintento inmediato con la misma clave."""
    await db.delete(claim)
    await db.commit()
