import logging
from typing import Optional
from fastapi import APIRouter, Depends, Body, Query, status
from app.core.database import DbDep
from app.core.security import get_current_admin, CurrentAdminDep, SuperAdminDep
from app.schemas.coupons import (
    CouponAdminPublic,
    CouponCreateRequest,
    CouponUpdateRequest,
    CouponListResponse,
    CouponCategoriesResponse,
    ReplaceCouponCategoriesRequest,
    ReplaceCouponCategoriesResponse,
    PatchCouponCategoriesRequest,
    PatchCouponCategoriesResponse,
    CouponProductsResponse,
    ReplaceCouponProductsRequest,
    ReplaceCouponProductsResponse,
    PatchCouponProductsRequest,
    PatchCouponProductsResponse,
    CouponAssignedClientPublic,
    CouponClientsResponse,
    ReplaceCouponClientsRequest,
    ReplaceCouponClientsResponse,
    PatchCouponClientsRequest,
    PatchCouponClientsResponse,
    CouponRedemptionAdminPublic,
    CouponRedemptionListResponse,
)
from app.schemas.taxonomy import CategoryAdminPublic
from app.schemas.products import ProductAdminBasic
from app.services import coupon_service, audit_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/coupons", tags=["Admin - Coupons"], dependencies=[Depends(get_current_admin)])


@router.post("", response_model=CouponAdminPublic, status_code=status.HTTP_201_CREATED, summary="Crear un cupón")
async def admin_create_coupon(db: DbDep, current: CurrentAdminDep, data: CouponCreateRequest = Body()):
    """Crea un cupón. `409` si ya existe uno con el mismo código (normalizado a
    mayúsculas). `scopeType=CATEGORY`/`PRODUCT` empieza sin categorías/productos asignados
    - usar `PUT .../categories` o `.../products` para poblarlos."""
    coupon = await coupon_service.create_coupon(db, data.model_dump())
    await audit_service.log_action(db, current, "coupon.create", "coupon", coupon.uuid, {"code": coupon.code})
    await db.commit()
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
async def admin_update_coupon(coupon_uuid: str, db: DbDep, current: CurrentAdminDep, data: CouponUpdateRequest = Body()):
    """Actualización parcial (`exclude_unset`, mismo patrón que categorías/vehículos) - solo
    los campos presentes en el body se tocan."""
    coupon = await coupon_service.update_coupon(db, coupon_uuid, data.model_dump(exclude_unset=True))
    await audit_service.log_action(db, current, "coupon.update", "coupon", coupon.uuid, data.model_dump(exclude_unset=True))
    await db.commit()
    return CouponAdminPublic.model_validate(coupon)


@router.delete("/{coupon_uuid}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar un cupón (bloqueado si ya tiene usos registrados)")
async def admin_delete_coupon(coupon_uuid: str, db: DbDep, current: SuperAdminDep):
    """Borrado real, solo super_admin. `409` si el cupón ya tiene algún uso registrado
    (cualquier status) - usar `PATCH {isActive: false}` para retirarlo sin perder el
    historial de órdenes que lo usaron."""
    await coupon_service.delete_coupon(db, coupon_uuid)
    await audit_service.log_action(db, current, "coupon.delete", "coupon", coupon_uuid)
    await db.commit()


@router.put("/{coupon_uuid}/categories", response_model=ReplaceCouponCategoriesResponse, summary="Reemplazar el conjunto completo de categorías del alcance del cupón")
async def admin_replace_coupon_categories(coupon_uuid: str, db: DbDep, data: ReplaceCouponCategoriesRequest = Body()):
    """Solo válido para cupones con `scopeType=CATEGORY` (`409` si no). Reemplazo completo,
    no incremental - mismo comportamiento que el equivalente de categorías/productos."""
    category_uuids = await coupon_service.replace_coupon_categories(db, coupon_uuid, data.category_uuids)
    return ReplaceCouponCategoriesResponse(coupon_uuid=coupon_uuid, category_uuids=category_uuids)


@router.patch("/{coupon_uuid}/categories", response_model=PatchCouponCategoriesResponse, summary="Agregar/quitar categorías del alcance del cupón de forma incremental")
async def admin_patch_coupon_categories(coupon_uuid: str, db: DbDep, data: PatchCouponCategoriesRequest = Body()):
    """Incremental (a diferencia del `PUT` de arriba): agrega/quita sin necesitar conocer
    el conjunto completo asignado - seguro de usar aunque el cupón tenga más categorías
    asignadas que el límite de `GET .../categories`. Solo válido para cupones con
    `scopeType=CATEGORY` (`409` si no). `add` ya asignadas se ignoran (idempotente);
    `remove` no asignadas o inexistentes también se ignoran (no-op tolerante). `422` si una
    misma categoría aparece en `add` y `remove` a la vez. `404` si alguna de `add` no
    resuelve a una categoría real."""
    added, removed, added_count, removed_count = await coupon_service.patch_coupon_categories(
        db, coupon_uuid, data.add, data.remove
    )
    return PatchCouponCategoriesResponse(
        coupon_uuid=coupon_uuid, added=added, removed=removed, added_count=added_count, removed_count=removed_count
    )


@router.get("/{coupon_uuid}/categories", response_model=CouponCategoriesResponse, summary="Listar las categorías asignadas al alcance del cupón")
async def admin_list_coupon_categories(coupon_uuid: str, db: DbDep):
    """Lectura del alcance CATEGORY actual, para poblar la UI de edición antes de un `PUT`
    de arriba. Sin paginar (acotada por naturaleza) - `[]` si el cupón no tiene ninguna
    asignada todavía (incluye un cupón que no es `scopeType=CATEGORY`, no es un error)."""
    categories = await coupon_service.list_coupon_categories(db, coupon_uuid)
    return CouponCategoriesResponse(docs=[CategoryAdminPublic.model_validate(c) for c in categories])


@router.put("/{coupon_uuid}/products", response_model=ReplaceCouponProductsResponse, summary="Reemplazar el conjunto completo de productos del alcance del cupón")
async def admin_replace_coupon_products(coupon_uuid: str, db: DbDep, data: ReplaceCouponProductsRequest = Body()):
    """Solo válido para cupones con `scopeType=PRODUCT` (`409` si no). Reemplazo completo."""
    product_uuids = await coupon_service.replace_coupon_products(db, coupon_uuid, data.product_uuids)
    return ReplaceCouponProductsResponse(coupon_uuid=coupon_uuid, product_uuids=product_uuids)


@router.patch("/{coupon_uuid}/products", response_model=PatchCouponProductsResponse, summary="Agregar/quitar productos del alcance del cupón de forma incremental")
async def admin_patch_coupon_products(coupon_uuid: str, db: DbDep, data: PatchCouponProductsRequest = Body()):
    """Incremental (a diferencia del `PUT` de arriba) - mismo patrón que el equivalente de
    categorías. Solo válido para cupones con `scopeType=PRODUCT` (`409` si no)."""
    added, removed, added_count, removed_count = await coupon_service.patch_coupon_products(
        db, coupon_uuid, data.add, data.remove
    )
    return PatchCouponProductsResponse(
        coupon_uuid=coupon_uuid, added=added, removed=removed, added_count=added_count, removed_count=removed_count
    )


@router.get("/{coupon_uuid}/products", response_model=CouponProductsResponse, summary="Listar los productos asignados al alcance del cupón")
async def admin_list_coupon_products(
    coupon_uuid: str,
    db: DbDep,
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Paginada (a diferencia de `.../categories`) - un cupón `scopeType=PRODUCT` puede
    tener muchos productos asignados. Pensada para poblar la UI antes de un `PUT` de arriba."""
    total, products = await coupon_service.list_coupon_products(db, coupon_uuid, limit, offset)
    return CouponProductsResponse(total=total, docs=[ProductAdminBasic.model_validate(p) for p in products])


@router.put("/{coupon_uuid}/clients", response_model=ReplaceCouponClientsResponse, summary="Reemplazar la lista de clientes elegibles para un cupón asignado")
async def admin_replace_coupon_clients(coupon_uuid: str, db: DbDep, data: ReplaceCouponClientsRequest = Body()):
    """Resuelto por email (no por uuid de cliente). Lista vacía = cupón público, cualquier
    cliente puede intentar redimirlo sujeto a las demás reglas. `404` si algún email no
    resuelve a una cuenta existente."""
    client_emails = await coupon_service.replace_coupon_clients(db, coupon_uuid, data.client_emails)
    return ReplaceCouponClientsResponse(coupon_uuid=coupon_uuid, client_emails=client_emails)


@router.patch("/{coupon_uuid}/clients", response_model=PatchCouponClientsResponse, summary="Agregar/quitar clientes elegibles de un cupón asignado de forma incremental")
async def admin_patch_coupon_clients(coupon_uuid: str, db: DbDep, data: PatchCouponClientsRequest = Body()):
    """Incremental (a diferencia del `PUT` de arriba) - mismo patrón que el equivalente de
    categorías/productos. Resuelto por email, igual que el `PUT`. `add` a un cupón que
    todavía era público (sin nadie asignado) lo vuelve restringido a esos clientes, mismo
    efecto que el `PUT` - la diferencia es que no hace falta reenviar a nadie más ya
    asignado."""
    added, removed, added_count, removed_count = await coupon_service.patch_coupon_clients(
        db, coupon_uuid, data.add, data.remove
    )
    return PatchCouponClientsResponse(
        coupon_uuid=coupon_uuid, added=added, removed=removed, added_count=added_count, removed_count=removed_count
    )


@router.get("/{coupon_uuid}/clients", response_model=CouponClientsResponse, summary="Listar los clientes elegibles para un cupón asignado")
async def admin_list_coupon_clients(
    coupon_uuid: str,
    db: DbDep,
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Paginada - una campaña de códigos asignados puede apuntar a muchos clientes. `[]` si
    el cupón es público (nada asignado) - no distingue eso de `scopeType` no-CATEGORY/PRODUCT,
    la elegibilidad por cliente es independiente del alcance de descuento."""
    total, clients = await coupon_service.list_coupon_clients(db, coupon_uuid, limit, offset)
    docs = [CouponAssignedClientPublic(uuid=c.uuid, email=c.email, name=c.name) for c in clients]
    return CouponClientsResponse(total=total, docs=docs)


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
