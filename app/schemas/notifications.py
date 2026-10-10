from datetime import datetime
from typing import List, Literal, Optional
from pydantic import EmailStr, Field, field_validator
from app.schemas.base import CamelModel
from app.schemas.products import ProductBasic


class StockAlertCreate(CamelModel):
    email: Optional[EmailStr] = Field(default=None, description="Requerido sin X-Client-Token. Con token se usa el correo de la cuenta y este se ignora.")

    @field_validator("email", mode="before")
    @classmethod
    def blank_to_none(cls, v):
        if isinstance(v, str) and v.strip() == "":
            return None
        return v


class StockAlertPublic(CamelModel):
    uuid: str
    product_uuid: str
    email: str
    created_at: datetime
    expires_at: datetime


class StockAlertWithProduct(StockAlertPublic):
    product: Optional[ProductBasic] = None


class StockAlertListResponse(CamelModel):
    docs: List[StockAlertWithProduct]


class UnsubscribeRequest(CamelModel):
    token: str = Field(min_length=1, description="unsubscribeToken del enlace del correo.")


class UnsubscribeResponse(CamelModel):
    scope: Literal["marketing", "stock-alert"]
    email: str = Field(description="Correo enmascarado (c***@example.com), para la confirmacion.")
