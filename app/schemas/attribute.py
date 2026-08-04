from __future__ import annotations
from typing import List, Optional
from datetime import datetime
from pydantic import Field, model_validator
from app.schemas.base import CamelModel
from app.schemas.products import ProductBasic, DataType, AttributeValue, AttributeValuePublic

# Admin (/v1/admin/attributes/*, /v1/admin/attribute-presets/*, /v1/admin/variant-groups/*):
# catalogo de definiciones de atributos + bundles de conveniencia + agrupacion de variantes.
# Los VALORES reales de cada producto viven en Product.attributes (JSONB) - ver
# admin_products.py / attribute_service.py. DataType/AttributeValue/AttributeValuePublic/
# VariantSiblingPublic/VariantGroupDetail viven en schemas/products.py (no aqui) para evitar
# un import circular con ProductDetail - ver el comentario alli.


class AttributePublic(CamelModel):
    uuid: str
    name: str
    slug: str
    data_type: DataType
    allowed_values: Optional[List[str]] = None
    unit: Optional[str] = None
    updated_at: datetime


class AttributeCreateRequest(CamelModel):
    name: str = Field(min_length=1)
    data_type: DataType
    allowed_values: Optional[List[str]] = Field(default=None, description="Requerido (min. 2 valores) cuando dataType es ENUM; ignorado en cualquier otro caso.")
    unit: Optional[str] = Field(default=None, description="Unidad de display opcional, p. ej. \"mm\", \"V\", \"L\"")

    @model_validator(mode="after")
    def _validate_enum_values(self) -> "AttributeCreateRequest":
        if self.data_type == "ENUM" and (not self.allowed_values or len(self.allowed_values) < 2):
            raise ValueError("allowedValues es requerido (minimo 2 valores) cuando dataType es ENUM.")
        return self


class AttributeUpdateRequest(CamelModel):
    """Actualizacion parcial (exclude_unset=True). La validacion ENUM/allowedValues se hace
    en attribute_service contra el estado EFECTIVO (mezclando lo nuevo con lo existente),
    igual que vehicle_service valida yearStart/yearEnd efectivos en update_vehicle."""
    name: Optional[str] = Field(default=None, min_length=1)
    data_type: Optional[DataType] = None
    allowed_values: Optional[List[str]] = None
    unit: Optional[str] = None


class AttributeListResponse(CamelModel):
    total: int
    docs: List[AttributePublic]


# --- Attribute presets: bundles de conveniencia, nunca obligatorios ni validados contra un producto -----

class AttributePresetPublic(CamelModel):
    uuid: str
    name: str
    slug: str
    updated_at: datetime


class AttributePresetCreateRequest(CamelModel):
    name: str = Field(min_length=1)


class AttributePresetUpdateRequest(CamelModel):
    name: Optional[str] = Field(default=None, min_length=1)


class AttributePresetListResponse(CamelModel):
    total: int
    docs: List[AttributePresetPublic]


class AttributePresetItemInput(CamelModel):
    attribute_uuid: str
    is_required: bool = Field(default=False, description="Solo asesorio para la UI - nunca se valida contra los atributos guardados de un producto.")
    display_order: int = Field(default=0)


class AttributePresetItemPublic(AttributePresetItemInput):
    attribute: AttributePublic


class ReplaceAttributePresetItemsRequest(CamelModel):
    items: List[AttributePresetItemInput] = Field(default_factory=list, max_length=200, description="Reemplaza el conjunto COMPLETO de atributos del preset.")


class AttributePresetItemsResponse(CamelModel):
    preset_uuid: str
    docs: List[AttributePresetItemPublic]


class ApplyPresetRequest(CamelModel):
    product_uuids: List[str] = Field(min_length=1, max_length=5000, description="sicar_uuid de cada producto - se le agregan las claves del preset con valor null SOLO si no las tiene ya (nunca sobreescribe un valor existente).")


class ApplyPresetResponse(CamelModel):
    preset_uuid: str
    product_uuids: List[str]
    scaffolded_count: int = Field(description="Pares (producto, atributo) realmente agregados - claves que el producto ya tenia no se recuentan aqui.")


# --- Product attribute values: Product.attributes (JSONB), clave=Attribute.slug -----

class AttributeValueInput(CamelModel):
    attribute_uuid: str
    value: AttributeValue = Field(description="Un solo campo polimorfico en el wire - se valida y traduce server-side contra el dataType/allowedValues del atributo referenciado.")


class ReplaceProductAttributesRequest(CamelModel):
    values: List[AttributeValueInput] = Field(default_factory=list, max_length=200, description="Reemplaza el conjunto COMPLETO de atributos del producto.")


class ProductAttributesResponse(CamelModel):
    product_uuid: str
    docs: List[AttributeValuePublic]


# --- Variant groups: vinculo explicito entre SKUs de Sicar X que son la misma pieza en variantes -----

class VariantGroupPublic(CamelModel):
    uuid: str
    name: str
    variant_attribute_slug: Optional[str] = None
    updated_at: datetime


class VariantGroupCreateRequest(CamelModel):
    name: str = Field(min_length=1)
    variant_attribute_slug: Optional[str] = Field(default=None, description="Que atributo distingue a los miembros (p. ej. \"color\") - texto libre, solo para que el frontend sepa que selector renderizar.")


class VariantGroupUpdateRequest(CamelModel):
    name: Optional[str] = Field(default=None, min_length=1)
    variant_attribute_slug: Optional[str] = None


class VariantGroupListResponse(CamelModel):
    total: int
    docs: List[VariantGroupPublic]


class ReplaceVariantGroupProductsRequest(CamelModel):
    product_uuids: List[str] = Field(default_factory=list, max_length=5000, description="sicar_uuid de cada producto - reemplaza el conjunto completo de miembros del grupo.")


class ReplaceVariantGroupProductsResponse(CamelModel):
    variant_group_uuid: str
    product_uuids: List[str]


class VariantGroupProductsResponse(CamelModel):
    total: int
    docs: List[ProductBasic]


class SetProductVariantGroupRequest(CamelModel):
    variant_group_uuid: Optional[str] = Field(default=None, description="null quita al producto de cualquier grupo de variantes.")


class SetProductVariantGroupResponse(CamelModel):
    product_uuid: str
    variant_group_uuid: Optional[str] = None
