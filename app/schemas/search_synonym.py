from datetime import datetime
from typing import List, Literal, Optional

from pydantic import Field, computed_field

from app.schemas.base import CamelModel

_ROOT_DESCRIPTION = (
    "Opcional. Con `root`, el sinonimo es de UNA direccion: buscar `root` tambien encuentra "
    "las palabras de `synonyms`, pero no al reves. Sin `root` (o `null`), es multi-direccional: "
    "cada palabra encuentra a las demas."
)
_SYNONYMS_DESCRIPTION = (
    "Palabras o frases (p. ej. `llave allen`). Se guardan normalizadas igual que las busquedas: "
    "minusculas, sin acentos, singular y unidades pegadas (`Baterías` -> `bateria`, `20 V` -> "
    "`20v`). La respuesta devuelve la forma guardada."
)


class SearchSynonymCreate(CamelModel):
    root: Optional[str] = Field(default=None, max_length=100, description=_ROOT_DESCRIPTION)
    synonyms: List[str] = Field(min_length=1, max_length=20, description=_SYNONYMS_DESCRIPTION)


class SearchSynonymUpdate(CamelModel):
    """Parcial (`exclude_unset`): mandar `root: null` explicito convierte una entrada de una
    direccion en multi-direccional."""
    root: Optional[str] = Field(default=None, max_length=100, description=_ROOT_DESCRIPTION)
    synonyms: Optional[List[str]] = Field(default=None, min_length=1, max_length=20, description=_SYNONYMS_DESCRIPTION)


class SearchSynonymPublic(CamelModel):
    uuid: str
    root: Optional[str]
    synonyms: List[str]
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def kind(self) -> Literal["MULTI_WAY", "ONE_WAY"]:
        return "ONE_WAY" if self.root else "MULTI_WAY"


class SearchSynonymWriteResponse(SearchSynonymPublic):
    synced_to_search: bool = Field(
        description="true si el cambio ya se aplico en el motor de busqueda. false si no se pudo "
                    "en este momento (o el motor no esta configurado): el cambio quedo guardado y "
                    "se aplica solo en unos minutos (reconciliacion del worker)."
    )


class SearchSynonymListResponse(CamelModel):
    total: int
    docs: List[SearchSynonymPublic]
