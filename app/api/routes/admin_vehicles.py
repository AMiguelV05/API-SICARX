import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, Body, Query, status
from app.core.database import DbDep
from app.core.security import validate_admin_key
from app.schemas.vehicle import (
    VehicleType,
    VehiclePublic,
    VehicleCreateRequest,
    VehicleUpdateRequest,
    VehicleListResponse,
    VehicleModelsResponse,
    ReplaceVehicleProductsRequest,
    ReplaceVehicleProductsResponse,
    VehicleProductsResponse,
    AssignProductsToModelRequest,
    AssignProductsToModelResponse,
)
from app.schemas.products import ProductBasic
from app.services import vehicle_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/vehicles", tags=["Admin - Vehicles"], dependencies=[Depends(validate_admin_key)])


@router.post("", response_model=VehiclePublic, status_code=status.HTTP_201_CREATED, summary="Crear un fitment de vehiculo (make/model/year-range/engine)")
async def admin_create_vehicle(db: DbDep, data: VehicleCreateRequest = Body()):
    """Crea un fitment (make/model/rango de anios/engine). `vehicles` es plana, no un
    arbol como `categories` - un fitment no es una jerarquia. `422` si `yearEnd < yearStart`."""
    vehicle = await vehicle_service.create_vehicle(db, data.vehicle_type, data.make, data.model, data.year_start, data.year_end, data.engine)
    return VehiclePublic.model_validate(vehicle)


@router.get("/by-product/{product_uuid}", response_model=List[VehiclePublic], summary="Listar los vehiculos asignados directamente a un producto")
async def admin_list_product_vehicles(product_uuid: str, db: DbDep):
    """Direccion inversa: dado un producto, los fitments a los que esta asignado
    directamente. Lista sin paginar, ordenada por make/model/yearStart; `[]` si el
    producto existe pero no tiene ninguno asignado."""
    vehicles = await vehicle_service.get_vehicles_for_product(db, product_uuid)
    return [VehiclePublic.model_validate(v) for v in vehicles]


@router.get("/models-for-years", response_model=VehicleModelsResponse, summary="Modelos de una marca disponibles en TODOS los anios dados (interseccion, para asignacion masiva)")
async def admin_list_models_for_years(
    db: DbDep,
    make: str = Query(...),
    years: List[int] = Query(..., description="Repetir el parametro por cada anio, p. ej. ?years=2012&years=2013"),
    vehicle_type: Optional[VehicleType] = Query(default=None, alias="vehicleType"),
):
    """Dado `make` y una lista de `years`, devuelve solo los modelos que existen en
    TODOS esos anios (interseccion, no union) - alimenta `POST /assign-by-model` para
    asignar un producto a un modelo a lo largo de varios anios sin elegir cada fitment
    uno por uno."""
    docs = await vehicle_service.get_distinct_models_for_years(db, make=make, years=years, vehicle_type=vehicle_type)
    return VehicleModelsResponse(docs=docs)


@router.post("/assign-by-model", response_model=AssignProductsToModelResponse, summary="Asignar productos a todos los fitments de un modelo que toquen alguno de los anios dados (aditivo, no reemplaza)")
async def admin_assign_products_by_model(db: DbDep, data: AssignProductsToModelRequest = Body()):
    """Asignacion masiva ADITIVA (a diferencia de `PUT .../{uuid}/products`, que
    reemplaza): agrega `productUuids` a todos los fitments de `make`/`model` cuyo rango
    de anios toque alguno de `years`. Idempotente (`ON CONFLICT DO NOTHING`) - pares ya
    existentes no cuentan como error. `404` si algun uuid no resuelve o ningun fitment
    coincide."""
    vehicle_uuids, product_uuids, assigned_count = await vehicle_service.assign_products_to_model_years(
        db,
        make=data.make,
        model=data.model,
        years=data.years,
        vehicle_type=data.vehicle_type,
        engine=data.engine,
        product_uuids=data.product_uuids,
    )
    return AssignProductsToModelResponse(
        make=data.make,
        model=data.model,
        years=sorted(set(data.years)),
        engine=data.engine,
        vehicle_uuids=vehicle_uuids,
        product_uuids=product_uuids,
        assigned_count=assigned_count,
    )


@router.get("", response_model=VehicleListResponse, summary="Buscar/listar vehiculos (filtrable por vehicleType/make/model)")
async def admin_list_vehicles(
    db: DbDep,
    vehicle_type: Optional[str] = Query(default=None, alias="vehicleType", description="AUTOMOTIVE/MOTORCYCLE"),
    make: Optional[str] = Query(default=None, description="Coincidencia parcial, sin distinguir mayusculas"),
    model: Optional[str] = Query(default=None, description="Coincidencia parcial, sin distinguir mayusculas"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Busqueda/listado de vehiculos - filtrable por `vehicleType` (exacto) y
    `make`/`model` (ILIKE parcial, sin distinguir mayusculas). Punto de entrada para
    encontrar un fitment ya existente por texto libre; el selector publico en cascada
    (`GET /v1/vehicles/*`) resuelve por pasos, no por busqueda libre."""
    total, vehicles = await vehicle_service.list_vehicles(db, vehicle_type=vehicle_type, make=make, model=model, limit=limit, offset=offset)
    return VehicleListResponse(total=total, docs=[VehiclePublic.model_validate(v) for v in vehicles])


@router.patch("/{vehicle_uuid}", response_model=VehiclePublic, summary="Actualizacion parcial de un vehiculo")
async def admin_update_vehicle(vehicle_uuid: str, db: DbDep, data: VehicleUpdateRequest = Body()):
    """Actualizacion parcial (`exclude_unset`, mismo patron que categorias) - `yearEnd:
    null` explicito marca el fitment como todavia vigente en produccion."""
    vehicle = await vehicle_service.update_vehicle(db, vehicle_uuid, data.model_dump(exclude_unset=True))
    return VehiclePublic.model_validate(vehicle)


@router.delete("/{vehicle_uuid}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar un vehiculo (bloqueado si tiene productos asignados)")
async def admin_delete_vehicle(vehicle_uuid: str, db: DbDep):
    """Borrado real. `409` si tiene productos asignados en `product_vehicles` -
    reasignarlos/quitarlos primero. Sin chequeo de "hijos" (no existen, tabla plana)."""
    await vehicle_service.delete_vehicle(db, vehicle_uuid)


@router.put("/{vehicle_uuid}/products", response_model=ReplaceVehicleProductsResponse, summary="Reemplazar el conjunto completo de productos asignados a un vehiculo")
async def admin_replace_vehicle_products(vehicle_uuid: str, db: DbDep, data: ReplaceVehicleProductsRequest = Body()):
    """Reemplaza el conjunto COMPLETO de productos asignados a un vehiculo - mismo
    comportamiento reemplazo-no-incremental que el equivalente de categorias."""
    product_uuids = await vehicle_service.replace_vehicle_products(db, vehicle_uuid, data.product_uuids)
    return ReplaceVehicleProductsResponse(vehicle_uuid=vehicle_uuid, product_uuids=product_uuids)


@router.get("/{vehicle_uuid}/products", response_model=VehicleProductsResponse, summary="Listar productos asignados directamente a un vehiculo")
async def admin_list_vehicle_products(
    vehicle_uuid: str,
    db: DbDep,
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Lista paginada de productos asignados directamente a un vehiculo. Pensada para
    poblar la UI de edicion antes de un PUT de arriba."""
    total, products = await vehicle_service.list_vehicle_products(db, vehicle_uuid, limit, offset)
    return VehicleProductsResponse(total=total, docs=[ProductBasic.model_validate(p) for p in products])
