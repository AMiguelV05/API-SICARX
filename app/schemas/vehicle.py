from __future__ import annotations
from typing import List, Literal, Optional
from datetime import datetime
from pydantic import Field
from app.schemas.base import CamelModel
from app.schemas.products import ProductBasic

# Admin (/v1/admin/vehicles/*, ver CLAUDE.md "Compatibilidad de vehiculos") - CRUD de
# fitments (make/model/year-range/engine) y asignacion de productos. El surface publico de
# solo lectura (facets en cascada + resolucion a uuid) vive en app/api/routes/vehicles.py -
# ver CLAUDE.md para el contrato completo.

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
    """Actualizacion parcial (`exclude_unset=True`, mismo patron que
    CategoryUpdateRequest/ClientAddressUpdate) - `yearEnd` mandado explicitamente en
    null mueve el fitment a "todavia vigente", ausente del body lo deja sin tocar."""
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

class AssignProductsToModelRequest(CamelModel):
    """Asignacion masiva ADITIVA de productos a un modelo a traves de varios anios (ver
    `vehicle_service.assign_products_to_model_years`) - distinto de
    `ReplaceVehicleProductsRequest`, que reemplaza el conjunto completo de UN solo
    vehiculo. `years` primero se resuelve contra `GET .../models-for-years` para saber que
    `model` existe en todos esos anios antes de mandar esta asignacion."""
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

# Publico (/v1/vehicles/*) - facets en cascada para un selector de vehiculo tipo
# auto-refacciones (tipo -> marca -> modelo -> anio -> motor). Sin total/paginacion: son
# listas de valores distintos, acotadas por naturaleza (un puñado de marcas/modelos/anios/
# motores por seleccion), no una coleccion paginada.

class VehicleMakesResponse(CamelModel):
    docs: List[str]

class VehicleModelsResponse(CamelModel):
    docs: List[str]

class VehicleYearsResponse(CamelModel):
    docs: List[int]

class VehicleEnginesResponse(CamelModel):
    docs: List[str]
