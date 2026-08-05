from __future__ import annotations
from typing import List, Literal, Optional
from datetime import datetime
from pydantic import Field, model_validator
from app.schemas.base import CamelModel
from app.schemas.products import ProductBasic

# Admin (/v1/admin/vehicles/*): CRUD de fitments y asignacion de productos; el surface publico de solo lectura vive en routes/vehicles.py.

VehicleType = Literal["AUTOMOTIVE", "MOTORCYCLE"]

class VehiclePublic(CamelModel):
    uuid: str
    vehicle_type: VehicleType
    make: str
    model: str
    year_start: int
    year_end: Optional[int] = None
    engine: Optional[str] = None
    updated_at: datetime

class VehicleCreateRequest(CamelModel):
    vehicle_type: VehicleType
    make: str = Field(min_length=1)
    model: str = Field(min_length=1)
    year_start: int = Field(ge=1900)
    year_end: Optional[int] = Field(default=None, ge=1900, description="Omitir/null = todavia en produccion")
    engine: Optional[str] = Field(default=None, description="Texto libre, p. ej. \"L4 1.6L\"")

class VehicleUpdateRequest(CamelModel):
    """Actualizacion parcial (exclude_unset=True): yearEnd explicito en null marca "todavia vigente"."""
    vehicle_type: Optional[VehicleType] = None
    make: Optional[str] = Field(default=None, min_length=1)
    model: Optional[str] = Field(default=None, min_length=1)
    year_start: Optional[int] = Field(default=None, ge=1900)
    year_end: Optional[int] = Field(default=None, ge=1900)
    engine: Optional[str] = None

class VehicleListResponse(CamelModel):
    total: int
    docs: List[VehiclePublic]

class ReplaceVehicleProductsRequest(CamelModel):
    product_uuids: List[str] = Field(default_factory=list, max_length=5000, description="sicar_uuid de cada producto - reemplaza el conjunto completo asignado al vehiculo")

class ReplaceVehicleProductsResponse(CamelModel):
    vehicle_uuid: str
    product_uuids: List[str]

class VehicleProductsResponse(CamelModel):
    total: int
    docs: List[ProductBasic]

class PatchVehicleProductsRequest(CamelModel):
    """Incremental (a diferencia de ReplaceVehicleProductsRequest): agrega/quita sin
    necesitar conocer el conjunto completo - pensado para cuando el vehiculo tiene mas
    productos asignados que el limite de GET .../products (200)."""
    add: List[str] = Field(default_factory=list, max_length=5000, description="sicar_uuid de productos a asignar; ya asignados se ignoran (idempotente).")
    remove: List[str] = Field(default_factory=list, max_length=5000, description="sicar_uuid de productos a quitar; no asignados o inexistentes se ignoran (no-op tolerante).")

    @model_validator(mode="after")
    def _no_overlap(self) -> "PatchVehicleProductsRequest":
        overlap = set(self.add) & set(self.remove)
        if overlap:
            raise ValueError(f"Un producto no puede estar en 'add' y 'remove' a la vez: {', '.join(sorted(overlap))}")
        return self

class PatchVehicleProductsResponse(CamelModel):
    vehicle_uuid: str
    added: List[str]
    removed: List[str]
    added_count: int
    removed_count: int

class AssignProductsToModelRequest(CamelModel):
    """Asignacion masiva ADITIVA a un modelo en varios anios - distinto de ReplaceVehicleProductsRequest (reemplaza un solo vehiculo)."""
    vehicle_type: Optional[VehicleType] = None
    make: str = Field(min_length=1)
    model: str = Field(min_length=1)
    years: List[int] = Field(min_length=1, description="El modelo debe existir en TODOS estos anios (interseccion, no union)")
    engine: Optional[str] = Field(default=None, description="Si se omite, aplica a todas las variantes de motor de ese make/model/anios")
    product_uuids: List[str] = Field(min_length=1, max_length=5000, description="sicar_uuid de cada producto a asignar")

class AssignProductsToModelResponse(CamelModel):
    make: str
    model: str
    years: List[int]
    engine: Optional[str] = None
    vehicle_uuids: List[str]
    product_uuids: List[str]
    assigned_count: int = Field(description="Vinculos (vehiculo, producto) nuevos realmente creados - pares ya existentes de una asignacion previa no se recuentan aqui")

# Publico (/v1/vehicles/*): facets en cascada tipo->marca->modelo->anio->motor. Sin paginacion: listas de valores acotadas por naturaleza.

class VehicleMakesResponse(CamelModel):
    docs: List[str]

class VehicleModelsResponse(CamelModel):
    docs: List[str]

class VehicleYearsResponse(CamelModel):
    docs: List[int]

class VehicleEnginesResponse(CamelModel):
    docs: List[str]
