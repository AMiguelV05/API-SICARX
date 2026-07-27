import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.order import OrderIdempotencyKey

logger = logging.getLogger(__name__)

# Sin infraestructura de limpieza en segundo plano para reclamos abandonados (p. ej. el
# proceso murio entre el reclamo y la creacion real de la orden) - se resuelve al vuelo
# en cada intento comparando la antiguedad del reclamo contra este umbral.
ABANDONED_CLAIM_THRESHOLD = timedelta(minutes=2)


async def claim_idempotency_key(db: AsyncSession, client_account_id: int, idempotency_key: str) -> tuple[OrderIdempotencyKey, bool]:
    """Intenta reclamar `idempotency_key` para este cliente en su propia mini-transaccion
    (independiente del resto del flujo de creacion de la orden). Devuelve `(fila, is_new)`:
    `is_new=True` si la clave nunca se habia visto (reclamo exitoso, seguir con la
    creacion normal). `is_new=False` si ya existia - el llamador decide que hacer segun
    si `order_uuid` ya esta poblado (orden ya creada, reintento seguro) o sigue en NULL
    (en proceso o abandonada). La fila devuelta siempre tiene sus atributos cargados
    (nunca expirados), sea por `refresh` tras el commit o por la consulta directa."""
    claim = OrderIdempotencyKey(client_account_id=client_account_id, idempotency_key=idempotency_key)
    db.add(claim)
    try:
        await db.commit()
        await db.refresh(claim)
        return claim, True
    except IntegrityError:
        await db.rollback()
        result = await db.execute(
            select(OrderIdempotencyKey).where(
                OrderIdempotencyKey.client_account_id == client_account_id,
                OrderIdempotencyKey.idempotency_key == idempotency_key,
            )
        )
        return result.scalar_one(), False


def is_claim_abandoned(claim: OrderIdempotencyKey) -> bool:
    created_at = claim.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created_at > ABANDONED_CLAIM_THRESHOLD


async def discard_claim(db: AsyncSession, claim: OrderIdempotencyKey) -> None:
    """Borra un reclamo abandonado o fallido para que el cliente pueda reintentar de
    inmediato con la misma clave, en vez de esperar ABANDONED_CLAIM_THRESHOLD."""
    await db.delete(claim)
    await db.commit()
