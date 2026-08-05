from typing import List, Literal, Optional, Union
from datetime import datetime
from pydantic import Field
from app.schemas.base import CamelModel

# DataType/AttributeValue/AttributeValuePublic/VariantSiblingPublic/VariantGroupDetail viven
# aqui (no en schemas/attribute.py) para que este modulo se quede como base/hoja - taxonomy.py
# y vehicle.py ya dependen de products.py (ProductBasic) y nunca al reves; attribute.py sigue
# el mismo patron e importa estos tipos de aqui en vez de definirlos el mismo, para evitar un
# import circular con ProductDetail (que a su vez necesita los tipos de attribute.py).

DataType = Literal["TEXT", "NUMBER", "BOOLEAN", "ENUM"]
AttributeValue = Union[str, float, bool, None]

class AttributeValuePublic(CamelModel):
    attribute_uuid: str
    name: str
    slug: str
    data_type: DataType
    unit: Optional[str] = None
    value: AttributeValue = None

class VariantSiblingPublic(CamelModel):
    uuid: str
    sku: Optional[str]
    name: str
    image_url: Optional[str]
    price: float
    stock: float
    value: AttributeValue = Field(default=None, description="Valor del atributo distintivo (variantAttributeSlug) para este sibling, si esta definido.")

class VariantGroupDetail(CamelModel):
    uuid: str
    name: str
    variant_attribute_slug: Optional[str] = None
    siblings: List[VariantSiblingPublic] = []

class LocalCatalogFilter(CamelModel):
    limit: int = Field(default=60, ge=1, le=200, description="Cantidad de productos por página (1-200)")
    offset: int = Field(default=0, ge=0, description="Paginación (inicio)")
    department_uuid: Optional[str] = None
    category_uuid: Optional[str] = None
    taxonomy_uuid: Optional[str] = Field(default=None, description="UUID de un nodo del arbol de categorias propio (PIM, GET /taxonomy) - distinto de category_uuid (clasificacion cruda de Sicar X). Incluye productos etiquetados en descendientes del nodo.")
    vehicle_uuid: Optional[str] = Field(default=None, description="UUID de un fitment de vehiculo (GET /v1/vehicles) - filtra a productos compatibles con ese vehiculo.")
    tag: Optional[str] = None
    in_stock: Optional[bool] = Field(default=False, description="Si es true, solo muestra productos con stock > 0")
    sort_by: Optional[Literal["price_asc", "price_desc", "name_asc", "relevance"]] = Field(
        default=None, description="Orden de los resultados: price_asc, price_desc, name_asc o relevance (mas vendidos primero)"
    )

class ProductBasic(CamelModel):
    sicar_uuid: str
    sku: str
    name: str
    description_details: Optional[str]
    image_url: Optional[str]
    price: float
    stock: float
    sales_count: float

class LocalCatalogResponse(CamelModel):
    total: int
    docs: List[ProductBasic]

class BestSellersResponse(CamelModel):
    docs: List[ProductBasic]

class ProductDetail(CamelModel):
    id: int
    sicar_uuid: str
    sku: Optional[str]
    additional_skus: Optional[list[str]]
    name: str
    description_details: Optional[str]
    image_url: Optional[str]
    tags: Optional[list[str]]
    additional_images: Optional[list[str]]
    sales_unit_uuid: Optional[str]
    unit_short_name: Optional[str]
    department_uuid: Optional[str]
    category_uuid: Optional[str]
    price: float
    stock: float
    is_bulk: bool
    is_active: bool
    is_deleted: bool
    last_sync_id: Optional[str]
    details_updated_at: Optional[datetime]
    deleted_at: Optional[datetime]
    # PIM propio (no sincronizado desde Sicar X) - aditivo, nunca en ProductBasic (solo detalle).
    attributes: List[AttributeValuePublic] = []
    variant_group: Optional[VariantGroupDetail] = None
