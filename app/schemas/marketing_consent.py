from datetime import datetime
from typing import List, Optional
from pydantic import Field
from app.schemas.base import CamelModel


class MarketingConsentEventPublic(CamelModel):
    value: bool = Field(description="true = acepto los correos de marketing, false = los rechazo o se dio de baja.")
    source: str = Field(description="register_checkbox, google_signup, account_toggle, email_unsubscribe, one_click_unsubscribe, admin, o migrated (estado que ya tenia la cuenta antes de existir este historial; sin version, IP ni user agent).")
    privacy_notice_version: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    changed_at: datetime = Field(description="Hora del servidor (UTC).")


class ClientMarketingConsentsResponse(CamelModel):
    client_uuid: str
    email: str
    name: str
    marketing_emails: bool = Field(description="Estado actual; siempre igual al value del evento mas reciente.")
    marketing_consent_at: Optional[datetime] = None
    events: List[MarketingConsentEventPublic] = Field(description="Historial completo, el mas reciente primero.")
