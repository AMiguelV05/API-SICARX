import logging
from typing import Optional
from fastapi import APIRouter, Depends, Body, Query, status
from app.core.database import DbDep
from app.core.security import validate_admin_key
from app.schemas.coupons import (
    CouponAdminPublic,
    CouponCreateRequest,
    CouponUpdateRequest,
    CouponListResponse,
    ReplaceCouponCategoriesRequest,
    ReplaceCouponCategoriesResponse,
    ReplaceCouponProductsRequest,
    ReplaceCouponProductsResponse,
    ReplaceCouponClientsRequest,
    ReplaceCouponClientsResponse,
    CouponRedemptionAdminPublic,
    CouponRedemptionListResponse,
)
from app.services import coupon_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/coupons", tags=["Admin - Coupons"], dependencies=[Depends(validate_admin_key)])


@router.post("", response_model=CouponAdminPublic, status_code=status.HTTP_201_CREATED, summary="Crear un cupón")
async def admin_create_coupon(db: DbDep, data: CouponCreateRequest = Body()):
    """Crea un cupón. `409` si ya existe uno con el mismo código (normalizado a
    mayúsculas). `scopeType=CATEGORY`/`PRODUCT` empieza sin categorías/productos asignados
    - usar `PUT .../categories` o `.../products` para poblarlos."""
    coupon = await coupon_service.create_coupon(db, data.model_dump())
    return CouponAdminPublic.model_validate(coupon)


@router.get("", response_model=CouponListResponse, summary="Buscar/listar cupones")
async def admin_list_coupons(
    db: DbDep,
    is_active: Optional[bool] = Query(default=None, alias="isActive"),
    code: Optional[str] = Query(default=None, description="Coincidencia parcial, sin distinguir mayúsculas"),
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    total, coupons = await coupon_service.list_coupons(db, limit, offset, is_active=is_active, code=code)
    return CouponListResponse(total=total, docs=[CouponAdminPublic.model_validate(c) for c in coupons])


@router.get("/{coupon_uuid}", response_model=CouponAdminPublic, summary="Detalle de un cupón")
async def admin_get_coupon(coupon_uuid: str, db: DbDep):
    coupon = await coupon_service.get_coupon_by_uuid(db, coupon_uuid)
    return CouponAdminPublic.model_validate(coupon)


@router.patch("/{coupon_uuid}", response_model=CouponAdminPublic, summary="Actualización parcial de un cupón")
async def admin_update_coupon(coupon_uuid: str, db: DbDep, data: CouponUpdateRequest = Body()):
    """Actualización parcial (`exclude_unset`, mismo patrón que categorías/vehículos) - solo
    los campos presentes en el body se tocan."""
    coupon = await coupon_service.update_coupon(db, coupon_uuid, data.model_dump(exclude_unset=True))
    return CouponAdminPublic.model_validate(coupon)


@router.delete("/{coupon_uuid}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar un cupón (bloqueado si ya tiene usos registrados)")
async def admin_delete_coupon(coupon_uuid: str, db: DbDep):
    """Borrado real. `409` si el cupón ya tiene algún uso registrado (cualquier status) -
    usar `PATCH {isActive: false}` para retirarlo sin perder el historial de órdenes que lo
    usaron."""
    await coupon_service.delete_coupon(db, coupon_uuid)


@router.put("/{coupon_uuid}/categories", response_model=ReplaceCouponCategoriesResponse, summary="Reemplazar el conjunto completo de categorías del alcance del cupón")
async def admin_replace_coupon_categories(coupon_uuid: str, db: DbDep, data: ReplaceCouponCategoriesRequest = Body()):
    """Solo válido para cupones con `scopeType=CATEGORY` (`409` si no). Reemplazo completo,
    no incremental - mismo comportamiento que el equivalente de categorías/productos."""
    category_uuids = await coupon_service.replace_coupon_categories(db, coupon_uuid, data.category_uuids)
    return ReplaceCouponCategoriesResponse(coupon_uuid=coupon_uuid, category_uuids=category_uuids)


@router.put("/{coupon_uuid}/products", response_model=ReplaceCouponProductsResponse, summary="Reemplazar el conjunto completo de productos del alcance del cupón")
async def admin_replace_coupon_products(coupon_uuid: str, db: DbDep, data: ReplaceCouponProductsRequest = Body()):
    """Solo válido para cupones con `scopeType=PRODUCT` (`409` si no). Reemplazo completo."""
    product_uuids = await coupon_service.replace_coupon_products(db, coupon_uuid, data.product_uuids)
    return ReplaceCouponProductsResponse(coupon_uuid=coupon_uuid, product_uuids=product_uuids)


@router.put("/{coupon_uuid}/clients", response_model=ReplaceCouponClientsResponse, summary="Reemplazar la lista de clientes elegibles para un cupón asignado")
async def admin_replace_coupon_clients(coupon_uuid: str, db: DbDep, data: ReplaceCouponClientsRequest = Body()):
    """Resuelto por email (no por uuid de cliente). Lista vacía = cupón público, cualquier
    cliente puede intentar redimirlo sujeto a las demás reglas. `404` si algún email no
    resuelve a una cuenta existente."""
    client_emails = await coupon_service.replace_coupon_clients(db, coupon_uuid, data.client_emails)
    return ReplaceCouponClientsResponse(coupon_uuid=coupon_uuid, client_emails=client_emails)


@router.get("/{coupon_uuid}/redemptions", response_model=CouponRedemptionListResponse, summary="Listar el historial de usos de un cupón")
async def admin_list_coupon_redemptions(
    coupon_uuid: str,
    db: DbDep,
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Incluye redenciones en cualquier status (PENDING/CONFIRMED/RELEASED) - una PENDING
    sigue esperando a que la orden llegue a PAID; una RELEASED corresponde a una orden que
    nunca llegó a pagarse."""
    total, rows = await coupon_service.list_redemptions(db, coupon_uuid, limit, offset)
    docs = [
        CouponRedemptionAdminPublic(
            id=redemption.id,
            client_account_id=redemption.client_account_id,
            client_email=client_email,
            order_uuid=order_uuid,
            status=redemption.status,
            created_at=redemption.created_at,
            updated_at=redemption.updated_at,
        )
        for redemption, client_email, order_uuid in rows
    ]
    return CouponRedemptionListResponse(total=total, docs=docs)
