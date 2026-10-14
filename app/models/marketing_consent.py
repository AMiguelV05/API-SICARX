from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import relationship
from app.core.database import Base
from app.models.client import ClientAccount


class MarketingConsentEvent(Base):
    """Historial del consentimiento de correos de marketing (ClientAccount.marketing_emails):
    una fila por cada cambio, para poder demostrar ante Profeco o la autoridad de datos
    personales cuando, como y con que version del aviso de privacidad se acepto o rechazo.

    Solo crece: un trigger de la migracion b6e2c9a4f1d7 rechaza UPDATE, DELETE y TRUNCATE.
    La unica forma de escribir aqui es marketing_consent_service.record_marketing_consent,
    que tambien actualiza ClientAccount.marketing_emails/marketing_emails_updated_at en la
    misma transaccion (ambos con el mismo instante), asi que el booleano de la cuenta siempre
    coincide con el ultimo evento."""
    __tablename__ = "marketing_consent_events"
    __table_args__ = (
        Index("ix_marketing_consent_events_client_changed", "client_account_id", "changed_at"),
    )

    id = Column(Integer, primary_key=True)
    # Sin ondelete: es evidencia legal, igual que Order (no existe borrado de cuentas hoy).
    client_account_id = Column(Integer, ForeignKey("client_accounts.id"), nullable=False)
    value = Column(Boolean, nullable=False)
    # register_checkbox | google_signup | account_toggle | email_unsubscribe |
    # one_click_unsubscribe | admin | migrated (estado previo a este historial, ver la
    # migracion). Vocabulario en la capa de aplicacion, igual que Order.status.
    source = Column(String, nullable=False)
    privacy_notice_version = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)
    changed_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    client_account = relationship(ClientAccount)
