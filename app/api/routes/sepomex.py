import logging
import re
from fastapi import APIRouter, Depends, HTTPException, Path, status
from app.core.security import validate_api_key
from app.services import sepomex_service
from app.schemas.sepomex import SepomexZipLookup, SepomexStatesResponse, SepomexCountiesResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sepomex", tags=["Sepomex"], dependencies=[Depends(validate_api_key)])

_ZIP_CODE_RE = re.compile(r"^\d{5}$")

@router.get("/zip/{zip_code}", response_model=SepomexZipLookup, summary="Autocompletar dirección por código postal")
async def lookup_zip_code(zip_code: str = Path()):
    """
    Consulta un código postal mexicano (datos oficiales de Sepomex vía
    correosmexico.com.mx) y devuelve estado/ciudad/municipio junto con la lista de
    colonias disponibles para ese CP, para autocompletar el formulario de dirección.
    """
    if not _ZIP_CODE_RE.match(zip_code):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="El código postal debe tener 5 dígitos.")

    info = await sepomex_service.get_zip_info(zip_code)
    if not info:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Código postal no encontrado.")
    return info

@router.get("/states", response_model=SepomexStatesResponse, summary="Listar estados de México")
async def list_states():
    """Devuelve el catálogo estático de los 32 estados, para el selector manual de estado."""
    return SepomexStatesResponse(states=sepomex_service.get_states())

@router.get("/states/{state}/counties", response_model=SepomexCountiesResponse, summary="Listar municipios de un estado")
async def list_counties(state: str = Path()):
    """
    Devuelve los municipios del estado indicado, para mostrar el dropdown de municipio
    cuando el usuario selecciona un estado manualmente (sin pasar por el código postal).
    `state` debe coincidir exactamente con uno de los valores de `GET /sepomex/states`.
    """
    counties = sepomex_service.get_counties(state)
    if counties is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Estado no reconocido.")
    return SepomexCountiesResponse(state=state, counties=counties)
