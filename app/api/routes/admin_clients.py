from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from app.core.database import DbDep
from app.core.security import get_current_admin
from app.models.client import ClientAccount
from app.schemas.marketing_consent import ClientMarketingConsentsResponse, MarketingConsentEventPublic
from app.services import marketing_consent_service

router = APIRouter(prefix="/admin/clients", tags=["Admin - Clients"], dependencies=[Depends(get_current_admin)])


@router.get("/{client_uuid}/consents", response_model=ClientMarketingConsentsResponse,
            summary="Historial de consentimiento de correos de marketing")
async def get_client_consents(db: DbDep, client_uuid: str = Path()):
    """Historial completo (solo crece) de `marketingEmails` de una cuenta, para atender una
    solicitud ARCO o una queja: cuando, como (`source`), con que version del aviso de
    privacidad, IP y user agent. Cualquier admin autenticado. `404` si la cuenta no existe."""
    client = await db.scalar(select(ClientAccount).where(ClientAccount.uuid == client_uuid))
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cliente no encontrado")
    events = await marketing_consent_service.list_consent_events(db, client)
    return ClientMarketingConsentsResponse(
        client_uuid=client.uuid,
        email=client.email,
        name=client.name,
        marketing_emails=client.marketing_emails,
        marketing_consent_at=client.marketing_emails_updated_at,
        events=[MarketingConsentEventPublic.model_validate(e) for e in events],
    )
