from typing import List, Optional
from app.schemas.base import CamelModel

class SepomexZipLookup(CamelModel):
    zip_code: str
    state: Optional[str] = None
    city: Optional[str] = None
    county: Optional[str] = None
    colonias: List[str] = []

class SepomexStatesResponse(CamelModel):
    states: List[str]

class SepomexCountiesResponse(CamelModel):
    state: str
    counties: List[str]
