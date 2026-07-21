from typing import List, Optional
from pydantic import EmailStr, Field
from app.schemas.base import CamelModel
from app.schemas.cart import CartResponse

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
    is_default: bool = False

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
    is_default: Optional[bool] = None

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
