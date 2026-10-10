from datetime import datetime, timezone
from typing import List, Optional
from pydantic import Field, field_validator, model_validator
from app.schemas.base import CamelModel

# Precio de oferta (PIM propio) - ver Product.compare_at_price/sale_ends_at y "Precio de
# oferta" en CLAUDE.md. El precio con descuento es siempre Product.price (de Sicar X); aqui
# solo se captura el precio ANTERIOR que se muestra tachado.


def _future_or_none(v: Optional[datetime]) -> Optional[datetime]:
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    if v <= datetime.now(timezone.utc):
        raise ValueError("saleEndsAt debe ser una fecha futura.")
    return v


class ProductSaleSetRequest(CamelModel):
    compare_at_price: float = Field(gt=0, description="Precio anterior (con IVA). Debe ser mayor que el price actual del producto.")
    sale_ends_at: Optional[datetime] = Field(default=None, description="Fin de la oferta (ISO-8601). Null = sin fecha de fin. Sin zona horaria se interpreta como UTC.")

    @field_validator("sale_ends_at")
    @classmethod
    def _check_ends(cls, v: Optional[datetime]) -> Optional[datetime]:
        return _future_or_none(v)


class ProductSalePublic(CamelModel):
    product_uuid: str
    price: float
    compare_at_price: Optional[float] = None
    sale_ends_at: Optional[datetime] = None
    is_on_sale: bool


class _BulkTarget(CamelModel):
    product_uuids: Optional[List[str]] = Field(default=None, min_length=1, max_length=500, description="sicarUuid de los productos (1-500).")
    category_uuid: Optional[str] = Field(default=None, description="Nodo del arbol PIM (GET /taxonomy); incluye productos de sus descendientes.")

    @model_validator(mode="after")
    def _exactly_one_target(self):
        if (self.product_uuids is None) == (self.category_uuid is None):
            raise ValueError("Manda exactamente uno de productUuids o categoryUuid.")
        return self


class BulkSaleSetRequest(_BulkTarget):
    compare_at_price: Optional[float] = Field(default=None, gt=0, description="Mismo precio anterior para todos los productos.")
    compare_at_percent: Optional[float] = Field(default=None, gt=0, lt=100, description="Descuento en %: compareAtPrice = price / (1 - pct/100), por producto. Sirve para marcar \"-20 %\" despues de bajar el precio en Sicar X.")
    sale_ends_at: Optional[datetime] = None

    @field_validator("sale_ends_at")
    @classmethod
    def _check_ends(cls, v: Optional[datetime]) -> Optional[datetime]:
        return _future_or_none(v)

    @model_validator(mode="after")
    def _exactly_one_price(self):
        if (self.compare_at_price is None) == (self.compare_at_percent is None):
            raise ValueError("Manda exactamente uno de compareAtPrice o compareAtPercent.")
        return self


class BulkSaleClearRequest(_BulkTarget):
    pass


class BulkSaleResponse(CamelModel):
    updated: int
    not_found: List[str] = Field(default=[], description="uuids (o la categoria) que no resolvieron a productos no eliminados.")
    skipped: List[str] = Field(default=[], description="Productos omitidos porque el precio anterior no quedaba mayor que su price actual.")
