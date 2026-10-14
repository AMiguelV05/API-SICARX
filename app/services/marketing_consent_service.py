"""Historial del consentimiento de correos de marketing (MarketingConsentEvent). Ver
CLAUDE.md, "Correos de marketing".

record_marketing_consent es el UNICO lugar que cambia ClientAccount.marketing_emails: escribe
el evento y el booleano en la misma transaccion (sin commit propio, lo hace quien llama), asi
que no puede quedar un cambio sin su registro."""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limit import FRONTEND_SSR_KEY, get_client_ip, is_trusted_frontend
from app.models.client import ClientAccount
from app.models.marketing_consent import MarketingConsentEvent

logger = logging.getLogger(__name__)

ConsentSource = Literal[
    "register_checkbox", "google_signup", "account_toggle",
    "email_unsubscribe", "one_click_unsubscribe", "admin",
]

_USER_AGENT_MAX = 512


@dataclass(frozen=True)
class ConsentContext:
    """IP y user agent del visitante, para el historial. Ambos opcionales."""
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None


def get_consent_context(request: Request) -> ConsentContext:
    """Desde el servidor del frontend (X-Frontend-Auth valido) se confia en X-Client-IP y
    X-Client-User-Agent, que el frontend reenvia del visitante; sin el secreto, la IP de
    get_client_ip y el User-Agent propio de la peticion. Misma regla que el rate limiting."""
    ip = get_client_ip(request)
    if ip == FRONTEND_SSR_KEY:
        ip = None
    header = "X-Client-User-Agent" if is_trusted_frontend(request) else "User-Agent"
    user_agent = (request.headers.get(header) or "").strip()[:_USER_AGENT_MAX] or None
    return ConsentContext(ip_address=ip, user_agent=user_agent)


ConsentContextDep = Annotated[ConsentContext, Depends(get_consent_context)]


def record_marketing_consent(
    db: AsyncSession,
    client: ClientAccount,
    value: bool,
    source: ConsentSource,
    *,
    privacy_notice_version: Optional[str] = None,
    context: Optional[ConsentContext] = None,
) -> MarketingConsentEvent:
    """Fija marketing_emails y agrega el evento. Sirve tambien para una cuenta nueva aun sin
    id (el evento va ligado por la relacion). No compara con el valor anterior: quien llama
    decide si hubo cambio (en el alta se registra siempre, aunque sea false)."""
    context = context or ConsentContext()
    now = datetime.now(timezone.utc)
    client.marketing_emails = value
    client.marketing_emails_updated_at = now
    event = MarketingConsentEvent(
        client_account=client,
        value=value,
        source=source,
        privacy_notice_version=privacy_notice_version,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
        changed_at=now,
    )
    db.add(event)
    return event


async def list_consent_events(db: AsyncSession, client: ClientAccount) -> list[MarketingConsentEvent]:
    """Historial completo de una cuenta, el mas reciente primero. Sin paginar: una persona
    cambia esto pocas veces."""
    rows = await db.scalars(
        select(MarketingConsentEvent)
        .where(MarketingConsentEvent.client_account_id == client.id)
        .order_by(MarketingConsentEvent.changed_at.desc(), MarketingConsentEvent.id.desc())
    )
    return list(rows.all())
