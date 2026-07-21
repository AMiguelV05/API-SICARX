import re
from typing import List, Optional
from pydantic import EmailStr, Field, field_validator
from app.schemas.base import CamelModel
from app.schemas.cart import CartResponse

_ZIP_CODE_RE = re.compile(r"^\d{5}$")

def _validate_zip_code(v: Optional[str]) -> Optional[str]:
    if v is not None and not _ZIP_CODE_RE.match(v):
        raise ValueError("El código postal debe tener 5 dígitos.")
    return v

class ClientRegister(CamelModel):
    name: str = Field(min_length=1, description="Nombre completo del cliente")
    email: EmailStr
    phone: Optional[str] = None
    password: str = Field(min_length=8, description="Contraseña en texto plano, mínimo 8 caracteres")
    cart_token: Optional[str] = Field(default=None, description="cartToken de un carrito anonimo a fusionar, si existe")

class ClientLogin(CamelModel):
    email: EmailStr
    password: str = Field(min_length=1)
    cart_token: Optional[str] = Field(default=None, description="cartToken de un carrito anonimo a fusionar, si existe")

class ClientAddressBase(CamelModel):
    label: Optional[str] = None
    street: str = Field(min_length=1)
    ext_number: Optional[str] = None
    int_number: Optional[str] = None
    neighborhood: Optional[str] = None
    city: Optional[str] = None
    county: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    zip_code: Optional[str] = None
    references: Optional[str] = None
    # Pin del picker de Google Maps del frontend (su propia key de Maps/Places) - este
    # backend solo lo persiste, nunca llama a Google.
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    is_default: bool = False

    _validate_zip_code = field_validator("zip_code")(_validate_zip_code)

class ClientAddressCreate(ClientAddressBase):
    pass

class ClientAddressUpdate(CamelModel):
    label: Optional[str] = None
    street: Optional[str] = Field(default=None, min_length=1)
    ext_number: Optional[str] = None
    int_number: Optional[str] = None
    neighborhood: Optional[str] = None
    city: Optional[str] = None
    county: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    zip_code: Optional[str] = None
    references: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    is_default: Optional[bool] = None

    _validate_zip_code = field_validator("zip_code")(_validate_zip_code)

class ClientAddressPublic(ClientAddressBase):
    uuid: str

class ClientPublic(CamelModel):
    uuid: str
    name: str
    email: str
    phone: Optional[str]
    addresses: List[ClientAddressPublic] = []

class ClientAuthResponse(CamelModel):
    token: str
    client: ClientPublic
    cart: CartResponse

class ClientUpdate(CamelModel):
    name: Optional[str] = Field(default=None, min_length=1, description="Nuevo nombre, si se quiere cambiar")
    phone: Optional[str] = Field(default=None, description="Nuevo teléfono, si se quiere cambiar")
    current_password: Optional[str] = Field(default=None, description="Contraseña actual, requerida solo si se envía new_password")
    new_password: Optional[str] = Field(default=None, min_length=8, description="Nueva contraseña, mínimo 8 caracteres")
