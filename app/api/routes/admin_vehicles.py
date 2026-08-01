import logging
from typing import Optional
from fastapi import APIRouter, Depends, Body, Query, status
from app.core.database import DbDep
from app.core.security import validate_admin_key
from app.schemas.vehicle import (
    VehicleAdminPublic,
    VehicleCreateRequest,
    VehicleUpdateRequest,
    VehicleListResponse,
    ReplaceVehicleProductsRequest,
    ReplaceVehicleProductsResponse,
    VehicleProductsResponse,
)
from app.schemas.products import ProductBasic
from app.services import vehicle_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/vehicles", tags=["Admin - Vehicles"], dependencies=[Depends(validate_admin_key)])


@router.post("", response_model=VehicleAdminPublic, status_code=status.HTTP_201_CREATED, summary="Crear un fitment de vehiculo (make/model/year-range/engine)")
async def admin_create_vehicle(db: DbDep, data: VehicleCreateRequest = Body()):
    vehicle = await vehicle_service.create_vehicle(db, data.vehicle_type, data.make, data.model, data.year_start, data.year_end, data.engine)
    return VehicleAdminPublic.model_validate(vehicle)


@router.get("", response_model=VehicleListResponse, summary="Buscar/listar vehiculos (filtrable por vehicleType/make/model)")
async def admin_list_vehicles(
    db: DbDep,
    vehicle_type: Optional[str] = Query(default=None, alias="vehicleType", description="AUTOMOTIVE/MOTORCYCLE"),
    make: Optional[str] = Query(default=None, description="Coincidencia parcial, sin distinguir mayusculas"),
    model: Optional[str] = Query(default=None, description="Coincidencia parcial, sin distinguir mayusculas"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    total, vehicles = await vehicle_service.list_vehicles(db, vehicle_type=vehicle_type, make=make, model=model, limit=limit, offset=offset)
    return VehicleListResponse(total=total, docs=[VehicleAdminPublic.model_validate(v) for v in vehicles])


@router.patch("/{vehicle_uuid}", response_model=VehicleAdminPublic, summary="Actualizacion parcial de un vehiculo")
async def admin_update_vehicle(vehicle_uuid: str, db: DbDep, data: VehicleUpdateRequest = Body()):
    vehicle = await vehicle_service.update_vehicle(db, vehicle_uuid, data.model_dump(exclude_unset=True))
    return VehicleAdminPublic.model_validate(vehicle)


@router.delete("/{vehicle_uuid}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar un vehiculo (bloqueado si tiene productos asignados)")
async def admin_delete_vehicle(vehicle_uuid: str, db: DbDep):
    await vehicle_service.delete_vehicle(db, vehicle_uuid)


@router.put("/{vehicle_uuid}/products", response_model=ReplaceVehicleProductsResponse, summary="Reemplazar el conjunto completo de productos asignados a un vehiculo")
async def admin_replace_vehicle_products(vehicle_uuid: str, db: DbDep, data: ReplaceVehicleProductsRequest = Body()):
    product_uuids = await vehicle_service.replace_vehicle_products(db, vehicle_uuid, data.product_uuids)
    return ReplaceVehicleProductsResponse(vehicle_uuid=vehicle_uuid, product_uuids=product_uuids)


@router.get("/{vehicle_uuid}/products", response_model=VehicleProductsResponse, summary="Listar productos asignados directamente a un vehiculo")
async def admin_list_vehicle_products(
    vehicle_uuid: str,
    db: DbDep,
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    total, products = await vehicle_service.list_vehicle_products(db, vehicle_uuid, limit, offset)
    return VehicleProductsResponse(total=total, docs=[ProductBasic.model_validate(p) for p in products])
