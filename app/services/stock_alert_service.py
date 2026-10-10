"""Avisos de reabastecimiento ("Avisame cuando llegue") y bajas de correos. Ver
app/models/stock_alert.py y CLAUDE.md, "Avisos de reabastecimiento"."""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_unsubscribe_token
from app.models.client import ClientAccount
from app.models.product import Product
from app.models.stock_alert import StockAlert

logger = logging.getLogger(__name__)

ALERT_TTL = timedelta(days=90)
# Tope anti-abuso por correo (ademas del limite por IP de la ruta): suscripciones creadas
# en las ultimas 24 h.
MAX_ALERTS_PER_EMAIL_PER_DAY = 20


def _active():
    return (StockAlert.notified_at.is_(None), StockAlert.unsubscribed_at.is_(None))


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


async def subscribe(db: AsyncSession, product_uuid: str, email: Optional[str], client: Optional[ClientAccount]) -> tuple[StockAlert, bool]:
    """(suscripcion, creada). Idempotente: si ya hay una activa para este producto+correo,
    la devuelve con creada=False. 404 si el producto no existe/no esta activo, 409 si SI
    tiene existencia, 400 sin correo, 429 si el correo excede el tope diario."""
    product = await db.scalar(select(Product).where(
        Product.sicar_uuid == product_uuid, Product.is_deleted == False, Product.is_active == True,  # noqa: E712
    ))
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado")
    if product.available_stock > 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Este producto sí tiene existencia; ya puedes comprarlo.")

    if client is not None:
        email = client.email
    if not email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Indica un correo para avisarte.")
    email = email.lower()

    existing = await db.scalar(select(StockAlert).where(StockAlert.product_id == product.id, StockAlert.email == email, *_active()))
    if existing is not None:
        if existing.expires_at > datetime.now(timezone.utc):
            return existing, False
        # Vencida pero todavia "activa" en el indice unico: se reemplaza por una nueva.
        existing.unsubscribed_at = datetime.now(timezone.utc)
        await db.flush()

    recent = await db.scalar(select(func.count()).select_from(StockAlert).where(
        StockAlert.email == email, StockAlert.created_at > datetime.now(timezone.utc) - timedelta(days=1),
    ))
    if recent >= MAX_ALERTS_PER_EMAIL_PER_DAY:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Demasiadas suscripciones con este correo. Intenta mañana.")

    now = datetime.now(timezone.utc)
    stmt = insert(StockAlert).values(
        product_id=product.id, email=email, client_account_id=client.id if client else None,
        expires_at=now + ALERT_TTL,
        uuid=str(uuid.uuid4()),
    ).on_conflict_do_nothing(
        index_elements=["product_id", "email"],
        index_where=StockAlert.notified_at.is_(None) & StockAlert.unsubscribed_at.is_(None),
    ).returning(StockAlert.id)
    new_id = await db.scalar(stmt)
    created = new_id is not None
    alert = await db.scalar(select(StockAlert).where(StockAlert.product_id == product.id, StockAlert.email == email, *_active()))
    await db.commit()
    if created:
        logger.info(f"Aviso de reabastecimiento creado: producto {product_uuid}, {mask_email(email)}.")
    return alert, created


async def list_client_alerts(db: AsyncSession, client: ClientAccount) -> list[tuple[StockAlert, Product]]:
    """Activas y vigentes de la cuenta: las ligadas a ella o con su correo (p. ej. creadas
    como invitado antes de iniciar sesion)."""
    rows = await db.execute(
        select(StockAlert, Product).join(Product, Product.id == StockAlert.product_id)
        .where(
            (StockAlert.client_account_id == client.id) | (StockAlert.email == client.email.lower()),
            *_active(), StockAlert.expires_at > func.now(),
        )
        .order_by(StockAlert.created_at.desc())
    )
    return list(rows.tuples().all())


async def cancel_client_alert(db: AsyncSession, client: ClientAccount, product_uuid: str) -> None:
    """Idempotente: sin suscripcion activa para ese producto tambien termina sin error."""
    product_id = select(Product.id).where(Product.sicar_uuid == product_uuid).scalar_subquery()
    await db.execute(
        update(StockAlert)
        .where(
            StockAlert.product_id == product_id,
            (StockAlert.client_account_id == client.id) | (StockAlert.email == client.email.lower()),
            *_active(),
        )
        .values(unsubscribed_at=func.now())
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def unsubscribe(db: AsyncSession, token: str) -> tuple[str, str]:
    """POST /notifications/unsubscribe -> (scope, correo enmascarado). Idempotente."""
    scope, sub = decode_unsubscribe_token(token)
    if scope == "marketing":
        client = await db.scalar(select(ClientAccount).where(ClientAccount.uuid == sub))
        if client is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Enlace de baja inválido.")
        if client.marketing_emails:
            client.marketing_emails = False
            client.marketing_emails_updated_at = datetime.now(timezone.utc)
            await db.commit()
            logger.info(f"Baja de correos de marketing: {mask_email(client.email)}.")
        return scope, mask_email(client.email)

    alert = await db.scalar(select(StockAlert).where(StockAlert.uuid == sub))
    if alert is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Enlace de baja inválido.")
    if alert.unsubscribed_at is None:
        alert.unsubscribed_at = datetime.now(timezone.utc)
        await db.commit()
    return scope, mask_email(alert.email)
