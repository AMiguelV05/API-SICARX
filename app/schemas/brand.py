from typing import List, Optional
from pydantic import Field, field_validator
from app.schemas.base import CamelModel


class AdminBrandPublic(CamelModel):
    name: str = Field(description="Grafia representativa del grupo (MIN(brand) entre las variantes).")
    product_count: int
    variants: List[str] = Field(description="Todas las grafias crudas agrupadas bajo `name` (case-insensitive). Mas de una = duplicado a fusionar via POST /admin/brands/rename.")


class AdminBrandListResponse(CamelModel):
    docs: List[AdminBrandPublic]
    unbranded_count: int = Field(description="Productos no eliminados sin marca (brand null).")


class BulkSetBrandRequest(CamelModel):
    product_uuids: List[str] = Field(min_length=1, max_length=500, description="sicarUuid de los productos a actualizar (1-500).")
    brand: Optional[str] = Field(description="Marca a asignar; se recorta, y null o \"\" la borra.")


class BulkSetBrandResponse(CamelModel):
    updated: int
    not_found: List[str]


class RenameBrandRequest(CamelModel):
    # `from` es palabra reservada en Python - alias explicito, gana sobre el alias_generator.
    from_: str = Field(alias="from", description="Marca actual (match case-insensitive).")
    to: str = Field(description="Marca nueva. Si ya existe, las dos se fusionan.")

    @field_validator("from_", "to")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("No puede estar vacio - para quitar una marca usa DELETE /admin/brands.")
        return value


class BrandUpdateCountResponse(CamelModel):
    updated: int
