from pydantic import BaseModel, EmailStr, Field
from typing import List, Optional

class ClientRegister(BaseModel):
    name: str = Field(..., min_length=1, description="Nombre completo del cliente")
    email: EmailStr
    phone: Optional[str] = None
    password: str = Field(..., min_length=8, description="Contraseña en texto plano, mínimo 8 caracteres")

class ClientLogin(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)

class ClientAddressBase(BaseModel):
    label: Optional[str] = None
    street: str = Field(..., min_length=1)
    ext_number: Optional[str] = None
    int_number: Optional[str] = None
    neighborhood: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    zip_code: Optional[str] = None
    references: Optional[str] = None
    is_default: bool = False

class ClientAddressCreate(ClientAddressBase):
    pass

class ClientAddressUpdate(BaseModel):
    label: Optional[str] = None
    street: Optional[str] = Field(default=None, min_length=1)
    ext_number: Optional[str] = None
    int_number: Optional[str] = None
    neighborhood: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    zip_code: Optional[str] = None
    references: Optional[str] = None
    is_default: Optional[bool] = None

class ClientAddressPublic(ClientAddressBase):
    uuid: str

    class Config:
        from_attributes = True

class ClientPublic(BaseModel):
    uuid: str
    name: str
    email: str
    phone: Optional[str]
    addresses: List[ClientAddressPublic] = []

    class Config:
        from_attributes = True

class ClientAuthResponse(BaseModel):
    token: str
    client: ClientPublic

class ClientUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, description="Nuevo nombre, si se quiere cambiar")
    phone: Optional[str] = Field(default=None, description="Nuevo teléfono, si se quiere cambiar")
    current_password: Optional[str] = Field(default=None, description="Contraseña actual, requerida solo si se envía new_password")
    new_password: Optional[str] = Field(default=None, min_length=8, description="Nueva contraseña, mínimo 8 caracteres")
