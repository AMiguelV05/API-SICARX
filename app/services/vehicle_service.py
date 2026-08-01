import logging
import uuid as uuid_lib
from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import select, func, delete, insert
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.vehicle import Vehicle, product_vehicles
from app.models.product import Product

logger = logging.getLogger(__name__)


def _validate_year_range(year_start: int, year_end: int | None) -> None:
    if year_end is not None and year_end < year_start:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="yearEnd no puede ser menor que yearStart.")


async def create_vehicle(db: AsyncSession, vehicle_type: str, make: str, model: str, year_start: int, year_end: int | None, engine: str | None) -> Vehicle:
    _validate_year_range(year_start, year_end)

    vehicle = Vehicle(
        uuid=str(uuid_lib.uuid4()),
        vehicle_type=vehicle_type,
        make=make,
        model=model,
        year_start=year_start,
        year_end=year_end,
        engine=engine,
        updated_at=datetime.now(timezone.utc),
    )
    db.add(vehicle)
    await db.commit()
    await db.refresh(vehicle)
    logger.info(f"Vehiculo '{vehicle.make} {vehicle.model}' ({vehicle.uuid}) creado via /admin.")
    return vehicle


async def update_vehicle(db: AsyncSession, vehicle_uuid: str, data: dict) -> Vehicle:
    """`data` viene de `VehicleUpdateRequest.model_dump(exclude_unset=True)` en la ruta -
    mismo patron que `taxonomy_service.update_category`. El rango de anios se valida
    contra los valores EFECTIVOS (lo que trae `data`, o si no, lo que ya tiene la fila)
    ya que un PATCH puede tocar solo uno de los dos campos."""
    vehicle = await db.get(Vehicle, vehicle_uuid)
    if vehicle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehiculo no encontrado.")

    effective_year_start = data.get("year_start", vehicle.year_start)
    effective_year_end = data["year_end"] if "year_end" in data else vehicle.year_end
    _validate_year_range(effective_year_start, effective_year_end)

    for field in ("vehicle_type", "make", "model", "year_start", "year_end", "engine"):
        if field in data:
            setattr(vehicle, field, data[field])

    vehicle.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(vehicle)
    logger.info(f"Vehiculo {vehicle.uuid} actualizado via /admin.")
    return vehicle


async def delete_vehicle(db: AsyncSession, vehicle_uuid: str) -> None:
    vehicle = await db.get(Vehicle, vehicle_uuid)
    if vehicle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehiculo no encontrado.")

    has_products = await db.scalar(select(product_vehicles.c.product_id).where(product_vehicles.c.vehicle_uuid == vehicle_uuid).limit(1))
    if has_products:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Este vehiculo tiene productos asignados; quitalos primero.")

    await db.delete(vehicle)
    await db.commit()
    logger.info(f"Vehiculo {vehicle_uuid} eliminado via /admin.")


def _escape_ilike(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_vehicles(
    db: AsyncSession,
    *,
    vehicle_type: str | None,
    make: str | None,
    model: str | None,
    limit: int,
    offset: int,
) -> tuple[int, list[Vehicle]]:
    """Busqueda/listado para el admin - a diferencia de categories, no hay ningun otro
    endpoint (publico o no) donde el admin pueda ver que vehiculos ya existen antes de
    asignarles productos, asi que este endpoint es el unico punto de entrada para eso."""
    stmt = select(Vehicle)
    if vehicle_type:
        stmt = stmt.where(Vehicle.vehicle_type == vehicle_type)
    if make:
        stmt = stmt.where(Vehicle.make.ilike(f"%{_escape_ilike(make)}%", escape="\\"))
    if model:
        stmt = stmt.where(Vehicle.model.ilike(f"%{_escape_ilike(model)}%", escape="\\"))

    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    result = await db.execute(stmt.order_by(Vehicle.make, Vehicle.model, Vehicle.year_start).limit(limit).offset(offset))
    return total or 0, list(result.scalars().all())


async def replace_vehicle_products(db: AsyncSession, vehicle_uuid: str, product_uuids: list[str]) -> list[str]:
    """Reemplaza el conjunto COMPLETO de productos asignados a `vehicle_uuid` en una sola
    transaccion - mismo patron (y misma limitacion: no es add/remove incremental) que
    taxonomy_service.replace_category_products."""
    vehicle = await db.get(Vehicle, vehicle_uuid)
    if vehicle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehiculo no encontrado.")

    unique_uuids = sorted(set(product_uuids))
    product_ids: list[int] = []
    if unique_uuids:
        result = await db.execute(
            select(Product.id, Product.sicar_uuid).where(Product.sicar_uuid.in_(unique_uuids), Product.is_deleted == False)
        )
        found = {row.sicar_uuid: row.id for row in result.all()}
        missing = [u for u in unique_uuids if u not in found]
        if missing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Productos no encontrados: {', '.join(missing)}")
        product_ids = [found[u] for u in unique_uuids]

    await db.execute(delete(product_vehicles).where(product_vehicles.c.vehicle_uuid == vehicle_uuid))
    if product_ids:
        await db.execute(
            insert(product_vehicles),
            [{"vehicle_uuid": vehicle_uuid, "product_id": pid} for pid in product_ids],
        )
    await db.commit()
    logger.info(f"Vehiculo {vehicle_uuid}: productos asignados reemplazados via /admin ({len(product_ids)} productos).")
    return unique_uuids


async def list_vehicle_products(db: AsyncSession, vehicle_uuid: str, limit: int, offset: int) -> tuple[int, list[Product]]:
    vehicle = await db.get(Vehicle, vehicle_uuid)
    if vehicle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehiculo no encontrado.")

    base = select(Product).join(product_vehicles, Product.id == product_vehicles.c.product_id).where(
        product_vehicles.c.vehicle_uuid == vehicle_uuid,
        Product.is_deleted == False,
    )
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    result = await db.execute(base.order_by(Product.name).limit(limit).offset(offset))
    return total or 0, list(result.scalars().all())
