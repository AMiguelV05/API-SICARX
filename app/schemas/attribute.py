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


class AttributeProductPublic(ProductBasic):
    """ProductBasic + el valor guardado de ESTE atributo - a diferencia de ProductBasic
    puro (usado por categorias/vehiculos, sin valor por definicion), aqui hay un valor
    real que mostrar junto con cada producto en la UI de edicion."""
    value: AttributeValue = None


class AttributeProductsResponse(CamelModel):
    total: int
    docs: List[AttributeProductPublic]


class AttributeProductValueInput(CamelModel):
    product_uuid: str
    value: AttributeValue = Field(description="Se valida server-side contra el dataType/allowedValues de este atributo, igual que en ReplaceProductAttributesRequest.")


class ReplaceAttributeProductsRequest(CamelModel):
    """Direccion attribute-primero: reemplaza el conjunto COMPLETO de productos que tienen
    ESTE atributo asignado. Complementa ReplaceProductAttributesRequest (product-primero,
    reemplaza TODOS los atributos de un producto) - aqui solo se toca la clave de este
    atributo en cada producto, el resto de lo que cada producto ya tuviera guardado no se
    toca."""
    values: List[AttributeProductValueInput] = Field(default_factory=list, max_length=5000, description="Un producto que ya tenia este atributo y no aparece aqui pierde la clave (sin tocar sus demas atributos guardados).")


class ReplaceAttributeProductsResponse(CamelModel):
    attribute_uuid: str
    docs: List[AttributeProductValueInput]


class PatchAttributeProductsRequest(CamelModel):
    """Incremental (a diferencia de ReplaceAttributeProductsRequest): agrega/actualiza
    valores o quita la clave de este atributo sin necesitar conocer el conjunto completo -
    pensado para cuando el atributo tiene mas productos asignados que el limite de
    GET .../products (200)."""
    upsert: List[AttributeProductValueInput] = Field(default_factory=list, max_length=5000, description="Productos a los que se les asigna/actualiza el valor de este atributo.")
    remove: List[str] = Field(default_factory=list, max_length=5000, description="sicar_uuid de productos a los que se les quita la clave de este atributo; quien no la tenga o no exista se ignora (no-op tolerante).")

    @model_validator(mode="after")
    def _no_overlap(self) -> "PatchAttributeProductsRequest":
        overlap = {v.product_uuid for v in self.upsert} & set(self.remove)
        if overlap:
            raise ValueError(f"Un producto no puede estar en 'upsert' y 'remove' a la vez: {', '.join(sorted(overlap))}")
        return self


class PatchAttributeProductsResponse(CamelModel):
    attribute_uuid: str
    upserted: List[AttributeProductValueInput]
    removed: List[str]
    upserted_count: int
    removed_count: int


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


class PatchVariantGroupProductsRequest(CamelModel):
    """Incremental (a diferencia de ReplaceVariantGroupProductsRequest): agrega/quita sin
    necesitar conocer el conjunto completo - pensado para cuando el grupo tiene mas
    productos asignados que el limite de GET .../products (200). `remove` solo quita la
    pertenencia si el producto sigue en ESTE grupo - no le toca variantGroupUuid si
    mientras tanto ya fue movido a otro grupo."""
    add: List[str] = Field(default_factory=list, max_length=5000, description="sicar_uuid de productos a asignar a este grupo; reasigna incondicionalmente aunque ya pertenecieran a otro grupo.")
    remove: List[str] = Field(default_factory=list, max_length=5000, description="sicar_uuid de productos a quitar de este grupo; quien no pertenezca a este grupo (o no exista) se ignora (no-op tolerante).")

    @model_validator(mode="after")
    def _no_overlap(self) -> "PatchVariantGroupProductsRequest":
        overlap = set(self.add) & set(self.remove)
        if overlap:
            raise ValueError(f"Un producto no puede estar en 'add' y 'remove' a la vez: {', '.join(sorted(overlap))}")
        return self


class PatchVariantGroupProductsResponse(CamelModel):
    variant_group_uuid: str
    added: List[str]
    removed: List[str]
    added_count: int
    removed_count: int


class SetProductVariantGroupRequest(CamelModel):
    variant_group_uuid: Optional[str] = Field(default=None, description="null quita al producto de cualquier grupo de variantes.")


class SetProductVariantGroupResponse(CamelModel):
    product_uuid: str
    variant_group_uuid: Optional[str] = None
