import logging
from typing import Optional
from fastapi import APIRouter, Depends, Query
from app.core.database import DbDep
from app.core.security import validate_api_key
from app.schemas.vehicle import (
    VehicleType,
    VehiclePublic,
    VehicleListResponse,
    VehicleMakesResponse,
    VehicleModelsResponse,
    VehicleYearsResponse,
    VehicleEnginesResponse,
)
from app.services import vehicle_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/vehicles", tags=["Vehicles"], dependencies=[Depends(validate_api_key)])


@router.get("/makes", response_model=VehicleMakesResponse, summary="Marcas distintas (paso 1 de la cascada)")
async def list_makes(
    db: DbDep,
    vehicle_type: Optional[VehicleType] = Query(default=None, alias="vehicleType"),
):
    docs = await vehicle_service.get_distinct_makes(db, vehicle_type=vehicle_type)
    return VehicleMakesResponse(docs=docs)


@router.get("/years", response_model=VehicleYearsResponse, summary="Anios distintos para una marca (paso 2)")
async def list_years(
    db: DbDep,
    make: str = Query(...),
    vehicle_type: Optional[VehicleType] = Query(default=None, alias="vehicleType"),
):
    docs = await vehicle_service.get_distinct_years(db, make=make, vehicle_type=vehicle_type)
    return VehicleYearsResponse(docs=docs)


@router.get("/models", response_model=VehicleModelsResponse, summary="Modelos distintos para marca+anio (paso 3)")
async def list_models(
    db: DbDep,
    make: str = Query(...),
    year: int = Query(...),
    vehicle_type: Optional[VehicleType] = Query(default=None, alias="vehicleType"),
):
    docs = await vehicle_service.get_distinct_models(db, make=make, year=year, vehicle_type=vehicle_type)
    return VehicleModelsResponse(docs=docs)


@router.get("/engines", response_model=VehicleEnginesResponse, summary="Motores distintos para marca+modelo+anio (paso 4)")
async def list_engines(
    db: DbDep,
    make: str = Query(...),
    model: str = Query(...),
    year: int = Query(...),
    vehicle_type: Optional[VehicleType] = Query(default=None, alias="vehicleType"),
):
    docs = await vehicle_service.get_distinct_engines(db, make=make, model=model, year=year, vehicle_type=vehicle_type)
    return VehicleEnginesResponse(docs=docs)


@router.get("", response_model=VehicleListResponse, summary="Resolver la seleccion a fitment(s) reales (paso final)")
async def list_vehicles(
    db: DbDep,
    vehicle_type: Optional[VehicleType] = Query(default=None, alias="vehicleType"),
    make: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    year: Optional[int] = Query(default=None),
    engine: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    total, vehicles = await vehicle_service.list_matching_vehicles(
        db, vehicle_type=vehicle_type, make=make, model=model, year=year, engine=engine, limit=limit, offset=offset
    )
    return VehicleListResponse(total=total, docs=[VehiclePublic.model_validate(v) for v in vehicles])
