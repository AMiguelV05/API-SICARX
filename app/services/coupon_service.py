import logging
from datetime import datetime, timezone
from decimal import Decimal
from fastapi import HTTPException, status
from sqlalchemy import select, func, delete, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.coupon import Coupon, CouponRedemption, coupon_categories, coupon_products, coupon_assigned_clients
from app.models.order import Order
from app.models.product import Product
from app.models.client import ClientAccount
from app.models.taxonomy import Category, product_categories
from app.services import taxonomy_service
from app.services.order_service import compute_subtotal, _to_decimal

logger = logging.getLogger(__name__)

# Uso deliberado en get_coupon_by_code/lock_and_validate_coupon: un codigo inexistente y
# uno inactivo responden exactamente igual, para no filtrar la existencia de codigos
# desactivados/expirados.
GENERIC_INVALID_DETAIL = "Cupón no válido."

_ACTIVE_REDEMPTION_STATUSES = ("PENDING", "CONFIRMED")


def _normalize_code(code: str) -> str:
    return code.strip().upper()


async def get_coupon_by_code(db: AsyncSession, code: str) -> Coupon:
    """Sin lock - usado por el preview del carrito y como primer paso antes del lock real
    en lock_and_validate_coupon."""
    coupon = await db.scalar(select(Coupon).where(Coupon.code == _normalize_code(code)))
    if coupon is None or not coupon.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_INVALID_DETAIL)
    return coupon


async def get_coupon_by_uuid(db: AsyncSession, coupon_uuid: str) -> Coupon:
    """Para /admin/coupons/*. A diferencia de get_coupon_by_code, aqui si es 404 real - no hay nada que ocultar de un admin."""
    coupon = await db.scalar(select(Coupon).where(Coupon.uuid == coupon_uuid))
    if coupon is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cupón no encontrado.")
    return coupon


async def validate_coupon_eligibility(
    db: AsyncSession, coupon: Coupon, client_account_id: int | None, subtotal: Decimal,
) -> None:
    """Corre todas las reglas de negocio. `client_account_id=None` (carrito anonimo) omite
    las reglas especificas del cliente (asignacion, primera compra, tope por cliente) - solo
    son autoritativas en POST /orders, post-login, mismo criterio que precio/stock."""
    now = datetime.now(timezone.utc)

    if not coupon.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_INVALID_DETAIL)
    if coupon.starts_at and now < coupon.starts_at:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Este cupón aún no está vigente.")
    if coupon.ends_at and now > coupon.ends_at:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Este cupón ya expiró.")
    if coupon.min_order_amount is not None and subtotal < _to_decimal(coupon.min_order_amount):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"El pedido debe ser de al menos ${coupon.min_order_amount} para usar este cupón.",
        )

    if client_account_id is not None:
        has_assignment = await db.scalar(
            select(coupon_assigned_clients.c.client_account_id).where(coupon_assigned_clients.c.coupon_id == coupon.id).limit(1)
        )
        if has_assignment:
            is_assigned = await db.scalar(
                select(coupon_assigned_clients.c.client_account_id).where(
                    coupon_assigned_clients.c.coupon_id == coupon.id,
                    coupon_assigned_clients.c.client_account_id == client_account_id,
                ).limit(1)
            )
            if not is_assigned:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_INVALID_DETAIL)

        if coupon.first_purchase_only:
            has_paid_order = await db.scalar(
                select(Order.id).where(Order.client_account_id == client_account_id, Order.status == "PAID").limit(1)
            )
            if has_paid_order:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Este cupón es válido solo para tu primera compra.")

        if coupon.max_uses_per_client is not None:
            client_uses = await db.scalar(
                select(func.count()).select_from(CouponRedemption).where(
                    CouponRedemption.coupon_id == coupon.id,
                    CouponRedemption.client_account_id == client_account_id,
                    CouponRedemption.status.in_(_ACTIVE_REDEMPTION_STATUSES),
                )
            )
            if (client_uses or 0) >= coupon.max_uses_per_client:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya usaste este cupón el máximo de veces permitido.")

    if coupon.max_total_uses is not None:
        total_uses = await db.scalar(
            select(func.count()).select_from(CouponRedemption).where(
                CouponRedemption.coupon_id == coupon.id,
                CouponRedemption.status.in_(_ACTIVE_REDEMPTION_STATUSES),
            )
        )
        if (total_uses or 0) >= coupon.max_total_uses:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Este cupón ya alcanzó su límite de usos.")


async def lock_and_validate_coupon(db: AsyncSession, code: str, client_account_id: int, subtotal: Decimal) -> Coupon:
    """Version autoritativa/bloqueada usada por POST /orders. `SELECT...FOR UPDATE` sobre
    el Coupon serializa reclamos concurrentes del mismo codigo - necesario porque
    max_total_uses/max_uses_per_client son topes via COUNT(*), no una clave unica (a
    diferencia de OrderIdempotencyKey). El lock se sostiene hasta el unico commit de
    routes/orders.py::create_order, haciendo atomico el conteo + el insert de
    CouponRedemption que hace el llamador despues (create_redemption)."""
    coupon = await db.scalar(select(Coupon).where(Coupon.code == _normalize_code(code)).with_for_update())
    if coupon is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_INVALID_DETAIL)
    await validate_coupon_eligibility(db, coupon, client_account_id, subtotal)
    return coupon


async def compute_scoped_subtotal(db: AsyncSession, coupon: Coupon, local_products: dict, quantities: dict) -> Decimal:
    """ORDER: subtotal completo. CATEGORY: interseccion via coupon_categories -> descendientes
    (taxonomy_service.get_descendant_uuids, mismo criterio que /catalog?taxonomyUuid) ->
    product_categories. PRODUCT: interseccion directa via coupon_products. Un scope sin
    ninguna categoria/producto asignado (o sin match en el carrito) computa $0, no un error."""
    if coupon.scope_type == "ORDER":
        return compute_subtotal(local_products, quantities, strict=False)

    product_ids_by_uuid = {uuid: p.id for uuid, p in local_products.items() if p is not None}
    if not product_ids_by_uuid:
        return Decimal("0")

    if coupon.scope_type == "CATEGORY":
        category_uuids = [
            row[0] for row in (await db.execute(
                select(coupon_categories.c.category_uuid).where(coupon_categories.c.coupon_id == coupon.id)
            )).all()
        ]
        if not category_uuids:
            return Decimal("0")
        descendant_uuids: set[str] = set()
        for cat_uuid in category_uuids:
            descendant_uuids.update(await taxonomy_service.get_descendant_uuids(db, cat_uuid))
        matching_ids = {
            row[0] for row in (await db.execute(
                select(product_categories.c.product_id).where(
                    product_categories.c.category_uuid.in_(descendant_uuids),
                    product_categories.c.product_id.in_(product_ids_by_uuid.values()),
                )
            )).all()
        }
    elif coupon.scope_type == "PRODUCT":
        matching_ids = {
            row[0] for row in (await db.execute(
                select(coupon_products.c.product_id).where(
                    coupon_products.c.coupon_id == coupon.id,
                    coupon_products.c.product_id.in_(product_ids_by_uuid.values()),
                )
            )).all()
        }
    else:
        return Decimal("0")

    if not matching_ids:
        return Decimal("0")
    scoped_quantities = {uuid: qty for uuid, qty in quantities.items() if product_ids_by_uuid.get(uuid) in matching_ids}
    return compute_subtotal(local_products, scoped_quantities, strict=False)


def compute_discount_amount(coupon: Coupon, scoped_subtotal: Decimal) -> Decimal:
    """PERCENTAGE: scoped_subtotal * discount_value/100, tope opcional max_discount_amount.
    FIXED_AMOUNT: discount_value tal cual. Siempre `min(resultado, scoped_subtotal)` - un
    descuento nunca puede exceder su propio alcance (y por extension nunca el subtotal
    completo de la orden), asi Order.total nunca queda negativo sin necesitar un chequeo
    defensivo aparte. Decimal sin redondear - el redondeo final ocurre una sola vez en
    build_order_payload/_format_amount."""
    if scoped_subtotal <= 0:
        return Decimal("0")
    if coupon.discount_type == "PERCENTAGE":
        amount = scoped_subtotal * (_to_decimal(coupon.discount_value) / Decimal("100"))
        if coupon.max_discount_amount is not None:
            amount = min(amount, _to_decimal(coupon.max_discount_amount))
    else:  # FIXED_AMOUNT
        amount = _to_decimal(coupon.discount_value)
    return min(amount, scoped_subtotal)


async def create_redemption(db: AsyncSession, coupon: Coupon, client_account_id: int, order_id: int) -> CouponRedemption:
    """INSERT status=PENDING + flush (no commit) - debe llamarse DESPUES de que la Order ya
    tenga `order.id` (post create_local_order) y DENTRO de la misma transaccion/lock que
    lock_and_validate_coupon, para que el conteo de topes y este insert sean atomicos entre si."""
    redemption = CouponRedemption(coupon_id=coupon.id, client_account_id=client_account_id, order_id=order_id, status="PENDING")
    db.add(redemption)
    await db.flush()
    return redemption


async def confirm_redemption(db: AsyncSession, order: Order) -> None:
    """No-op si la orden no tiene cupon. Llamada SOLO en la transicion real TO_PAY->PAID de
    finalize_order_payment - nunca en un reintento de webhook para una orden ya PAID."""
    if not order.coupon_id:
        return
    redemption = await db.scalar(select(CouponRedemption).where(CouponRedemption.order_id == order.id))
    if redemption is not None and redemption.status == "PENDING":
        redemption.status = "CONFIRMED"


async def release_redemption(db: AsyncSession, order: Order) -> None:
    """No-op si la orden no tiene cupon. Solo libera una redencion que sigue PENDING (la
    orden nunca llego a PAID) - una ya CONFIRMED no se toca, decision deliberada anti-abuso:
    una vez pagada, el uso queda consumido para siempre aunque la orden se cancele/reembolse
    despues. Se decide por el estado de la propia redencion, no el de la orden, asi que es
    seguro llamarla incondicionalmente desde prepare_local_cancellation."""
    if not order.coupon_id:
        return
    redemption = await db.scalar(select(CouponRedemption).where(CouponRedemption.order_id == order.id))
    if redemption is not None and redemption.status == "PENDING":
        redemption.status = "RELEASED"


# Desde aqui: muta coupons/coupon_*, solo llamado por admin_coupons.py (validate_admin_key).

def _decimal_or_none(value) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


async def create_coupon(db: AsyncSession, data: dict) -> Coupon:
    code = _normalize_code(data["code"])
    existing = await db.scalar(select(Coupon.id).where(Coupon.code == code))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya existe un cupón con este código.")

    coupon = Coupon(
        code=code,
        discount_type=data["discount_type"],
        discount_value=_decimal_or_none(data["discount_value"]),
        max_discount_amount=_decimal_or_none(data.get("max_discount_amount")),
        scope_type=data.get("scope_type", "ORDER"),
        min_order_amount=_decimal_or_none(data.get("min_order_amount")),
        starts_at=data.get("starts_at"),
        ends_at=data.get("ends_at"),
        is_active=data.get("is_active", True),
        max_total_uses=data.get("max_total_uses"),
        max_uses_per_client=data.get("max_uses_per_client"),
        first_purchase_only=data.get("first_purchase_only", False),
    )
    db.add(coupon)
    await db.commit()
    await db.refresh(coupon)
    logger.info(f"Cupón '{coupon.code}' ({coupon.uuid}) creado via /admin.")
    return coupon


async def update_coupon(db: AsyncSession, coupon_uuid: str, data: dict) -> Coupon:
    """`data` es exclude_unset=True: solo los campos presentes se tocan."""
    coupon = await get_coupon_by_uuid(db, coupon_uuid)

    if "code" in data:
        new_code = _normalize_code(data["code"])
        if new_code != coupon.code:
            existing = await db.scalar(select(Coupon.id).where(Coupon.code == new_code, Coupon.id != coupon.id))
            if existing:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya existe un cupón con este código.")
        coupon.code = new_code

    for field in ("discount_type", "scope_type", "starts_at", "ends_at", "is_active", "max_total_uses", "max_uses_per_client", "first_purchase_only"):
        if field in data:
            setattr(coupon, field, data[field])
    for field in ("discount_value", "max_discount_amount", "min_order_amount"):
        if field in data:
            setattr(coupon, field, _decimal_or_none(data[field]))

    coupon.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(coupon)
    logger.info(f"Cupón {coupon.uuid} actualizado via /admin.")
    return coupon


async def delete_coupon(db: AsyncSession, coupon_uuid: str) -> None:
    """Borrado real. 409 si ya tiene usos registrados (cualquier status) - preserva la
    integridad historica de las ordenes que lo usaron; desactivar (`isActive=false`) es el
    camino para retirar un cupon que ya se uso."""
    coupon = await get_coupon_by_uuid(db, coupon_uuid)
    has_redemptions = await db.scalar(select(CouponRedemption.id).where(CouponRedemption.coupon_id == coupon.id).limit(1))
    if has_redemptions:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Este cupón ya tiene usos registrados; desactívalo (isActive=false) en vez de eliminarlo.",
        )
    await db.delete(coupon)
    await db.commit()
    logger.info(f"Cupón {coupon_uuid} eliminado via /admin.")


async def list_coupons(db: AsyncSession, limit: int, offset: int, *, is_active: bool | None = None, code: str | None = None) -> tuple[int, list[Coupon]]:
    stmt = select(Coupon)
    if is_active is not None:
        stmt = stmt.where(Coupon.is_active == is_active)
    if code:
        stmt = stmt.where(Coupon.code.ilike(f"%{_normalize_code(code)}%"))
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    result = await db.execute(stmt.order_by(Coupon.created_at.desc()).limit(limit).offset(offset))
    return total or 0, list(result.scalars().all())


async def list_coupon_categories(db: AsyncSession, coupon_uuid: str) -> list[Category]:
    """Lectura para poblar la UI de edicion antes de un PUT .../categories. Sin paginar
    (acotada por naturaleza, mismo criterio que get_categories_for_product en
    taxonomy_service) - el conjunto de categorias asignadas a UN cupon es chico."""
    coupon = await get_coupon_by_uuid(db, coupon_uuid)
    stmt = (
        select(Category)
        .join(coupon_categories, Category.uuid == coupon_categories.c.category_uuid)
        .where(coupon_categories.c.coupon_id == coupon.id)
        .order_by(func.lower(Category.name))
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_coupon_products(db: AsyncSession, coupon_uuid: str, limit: int, offset: int) -> tuple[int, list[Product]]:
    """Paginada (a diferencia de list_coupon_categories) - un cupon PRODUCT puede tener
    muchos productos asignados, mismo criterio que list_category_products."""
    coupon = await get_coupon_by_uuid(db, coupon_uuid)
    base = select(Product).join(coupon_products, Product.id == coupon_products.c.product_id).where(
        coupon_products.c.coupon_id == coupon.id,
        Product.is_deleted == False,
    )
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    result = await db.execute(base.order_by(Product.name).limit(limit).offset(offset))
    return total or 0, list(result.scalars().all())


async def list_coupon_clients(db: AsyncSession, coupon_uuid: str, limit: int, offset: int) -> tuple[int, list[ClientAccount]]:
    """Paginada - una campana de codigos asignados puede apuntar a muchos clientes."""
    coupon = await get_coupon_by_uuid(db, coupon_uuid)
    base = select(ClientAccount).join(coupon_assigned_clients, ClientAccount.id == coupon_assigned_clients.c.client_account_id).where(
        coupon_assigned_clients.c.coupon_id == coupon.id
    )
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    result = await db.execute(base.order_by(ClientAccount.email).limit(limit).offset(offset))
    return total or 0, list(result.scalars().all())


async def replace_coupon_categories(db: AsyncSession, coupon_uuid: str, category_uuids: list[str]) -> list[str]:
    """Reemplaza el conjunto COMPLETO (no add/remove incremental) - mismo patron que replace_category_products."""
    coupon = await get_coupon_by_uuid(db, coupon_uuid)
    if coupon.scope_type != "CATEGORY":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Este cupón no tiene scopeType=CATEGORY.")

    unique_uuids = sorted(set(category_uuids))
    if unique_uuids:
        result = await db.execute(select(Category.uuid).where(Category.uuid.in_(unique_uuids)))
        found = {row[0] for row in result.all()}
        missing = [u for u in unique_uuids if u not in found]
        if missing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Categorías no encontradas: {', '.join(missing)}")

    await db.execute(delete(coupon_categories).where(coupon_categories.c.coupon_id == coupon.id))
    if unique_uuids:
        await db.execute(insert(coupon_categories), [{"coupon_id": coupon.id, "category_uuid": u} for u in unique_uuids])
    await db.commit()
    logger.info(f"Cupón {coupon_uuid}: categorías reemplazadas via /admin ({len(unique_uuids)}).")
    return unique_uuids


async def patch_coupon_categories(db: AsyncSession, coupon_uuid: str, add_uuids: list[str], remove_uuids: list[str]) -> tuple[list[str], list[str], int, int]:
    """Incremental (a diferencia de replace_coupon_categories) - mismo patron que
    taxonomy_service.patch_category_products. `remove_uuids` es tolerante (no valida
    existencia)."""
    coupon = await get_coupon_by_uuid(db, coupon_uuid)
    if coupon.scope_type != "CATEGORY":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Este cupón no tiene scopeType=CATEGORY.")

    unique_add = sorted(set(add_uuids))
    if unique_add:
        result = await db.execute(select(Category.uuid).where(Category.uuid.in_(unique_add)))
        found = {row[0] for row in result.all()}
        missing = [u for u in unique_add if u not in found]
        if missing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Categorías no encontradas: {', '.join(missing)}")

    added_count = 0
    if unique_add:
        insert_stmt = pg_insert(coupon_categories).values(
            [{"coupon_id": coupon.id, "category_uuid": u} for u in unique_add]
        ).on_conflict_do_nothing(index_elements=["coupon_id", "category_uuid"]).returning(coupon_categories.c.category_uuid)
        insert_result = await db.execute(insert_stmt)
        added_count = len(insert_result.all())

    unique_remove = sorted(set(remove_uuids))
    removed_count = 0
    if unique_remove:
        delete_stmt = delete(coupon_categories).where(
            coupon_categories.c.coupon_id == coupon.id,
            coupon_categories.c.category_uuid.in_(unique_remove),
        ).returning(coupon_categories.c.category_uuid)
        delete_result = await db.execute(delete_stmt)
        removed_count = len(delete_result.all())

    await db.commit()
    logger.info(f"Cupón {coupon_uuid}: {added_count} categoría(s) agregada(s), {removed_count} quitada(s) via PATCH /admin.")
    return unique_add, unique_remove, added_count, removed_count


async def replace_coupon_products(db: AsyncSession, coupon_uuid: str, product_uuids: list[str]) -> list[str]:
    coupon = await get_coupon_by_uuid(db, coupon_uuid)
    if coupon.scope_type != "PRODUCT":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Este cupón no tiene scopeType=PRODUCT.")

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

    await db.execute(delete(coupon_products).where(coupon_products.c.coupon_id == coupon.id))
    if product_ids:
        await db.execute(insert(coupon_products), [{"coupon_id": coupon.id, "product_id": pid} for pid in product_ids])
    await db.commit()
    logger.info(f"Cupón {coupon_uuid}: productos reemplazados via /admin ({len(product_ids)}).")
    return unique_uuids


async def patch_coupon_products(db: AsyncSession, coupon_uuid: str, add_uuids: list[str], remove_uuids: list[str]) -> tuple[list[str], list[str], int, int]:
    """Incremental (a diferencia de replace_coupon_products) - mismo patron que
    taxonomy_service.patch_category_products."""
    coupon = await get_coupon_by_uuid(db, coupon_uuid)
    if coupon.scope_type != "PRODUCT":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Este cupón no tiene scopeType=PRODUCT.")

    unique_add = sorted(set(add_uuids))
    add_product_ids: list[int] = []
    if unique_add:
        result = await db.execute(
            select(Product.id, Product.sicar_uuid).where(Product.sicar_uuid.in_(unique_add), Product.is_deleted == False)
        )
        found = {row.sicar_uuid: row.id for row in result.all()}
        missing = [u for u in unique_add if u not in found]
        if missing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Productos no encontrados: {', '.join(missing)}")
        add_product_ids = [found[u] for u in unique_add]

    added_count = 0
    if add_product_ids:
        insert_stmt = pg_insert(coupon_products).values(
            [{"coupon_id": coupon.id, "product_id": pid} for pid in add_product_ids]
        ).on_conflict_do_nothing(index_elements=["coupon_id", "product_id"]).returning(coupon_products.c.product_id)
        insert_result = await db.execute(insert_stmt)
        added_count = len(insert_result.all())

    unique_remove = sorted(set(remove_uuids))
    removed_count = 0
    if unique_remove:
        delete_stmt = delete(coupon_products).where(
            coupon_products.c.coupon_id == coupon.id,
            coupon_products.c.product_id.in_(select(Product.id).where(Product.sicar_uuid.in_(unique_remove))),
        ).returning(coupon_products.c.product_id)
        delete_result = await db.execute(delete_stmt)
        removed_count = len(delete_result.all())

    await db.commit()
    logger.info(f"Cupón {coupon_uuid}: {added_count} producto(s) agregado(s), {removed_count} quitado(s) via PATCH /admin.")
    return unique_add, unique_remove, added_count, removed_count


async def replace_coupon_clients(db: AsyncSession, coupon_uuid: str, client_emails: list[str]) -> list[str]:
    """Vacio = cupon publico. Resuelto por email (no hay uuid de cliente a mano en la UI de admin) - mismo lowercase que client_service."""
    coupon = await get_coupon_by_uuid(db, coupon_uuid)

    unique_emails = sorted({e.strip().lower() for e in client_emails})
    client_ids: list[int] = []
    if unique_emails:
        result = await db.execute(select(ClientAccount.id, ClientAccount.email).where(ClientAccount.email.in_(unique_emails)))
        found = {row.email: row.id for row in result.all()}
        missing = [e for e in unique_emails if e not in found]
        if missing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Clientes no encontrados: {', '.join(missing)}")
        client_ids = [found[e] for e in unique_emails]

    await db.execute(delete(coupon_assigned_clients).where(coupon_assigned_clients.c.coupon_id == coupon.id))
    if client_ids:
        await db.execute(insert(coupon_assigned_clients), [{"coupon_id": coupon.id, "client_account_id": cid} for cid in client_ids])
    await db.commit()
    logger.info(f"Cupón {coupon_uuid}: clientes asignados reemplazados via /admin ({len(client_ids)}).")
    return unique_emails


async def patch_coupon_clients(db: AsyncSession, coupon_uuid: str, add_emails: list[str], remove_emails: list[str]) -> tuple[list[str], list[str], int, int]:
    """Incremental (a diferencia de replace_coupon_clients) - mismo patron que
    taxonomy_service.patch_category_products. Sin chequeo de scope_type - la elegibilidad
    por cliente es independiente del alcance de descuento, mismo criterio que
    list_coupon_clients."""
    coupon = await get_coupon_by_uuid(db, coupon_uuid)

    unique_add = sorted({e.strip().lower() for e in add_emails})
    add_client_ids: list[int] = []
    if unique_add:
        result = await db.execute(select(ClientAccount.id, ClientAccount.email).where(ClientAccount.email.in_(unique_add)))
        found = {row.email: row.id for row in result.all()}
        missing = [e for e in unique_add if e not in found]
        if missing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Clientes no encontrados: {', '.join(missing)}")
        add_client_ids = [found[e] for e in unique_add]

    added_count = 0
    if add_client_ids:
        insert_stmt = pg_insert(coupon_assigned_clients).values(
            [{"coupon_id": coupon.id, "client_account_id": cid} for cid in add_client_ids]
        ).on_conflict_do_nothing(index_elements=["coupon_id", "client_account_id"]).returning(coupon_assigned_clients.c.client_account_id)
        insert_result = await db.execute(insert_stmt)
        added_count = len(insert_result.all())

    unique_remove = sorted({e.strip().lower() for e in remove_emails})
    removed_count = 0
    if unique_remove:
        delete_stmt = delete(coupon_assigned_clients).where(
            coupon_assigned_clients.c.coupon_id == coupon.id,
            coupon_assigned_clients.c.client_account_id.in_(select(ClientAccount.id).where(ClientAccount.email.in_(unique_remove))),
        ).returning(coupon_assigned_clients.c.client_account_id)
        delete_result = await db.execute(delete_stmt)
        removed_count = len(delete_result.all())

    await db.commit()
    logger.info(f"Cupón {coupon_uuid}: {added_count} cliente(s) agregado(s), {removed_count} quitado(s) via PATCH /admin.")
    return unique_add, unique_remove, added_count, removed_count


async def list_redemptions(db: AsyncSession, coupon_uuid: str, limit: int, offset: int) -> tuple[int, list]:
    coupon = await get_coupon_by_uuid(db, coupon_uuid)
    base = (
        select(CouponRedemption, ClientAccount.email, Order.uuid)
        .join(ClientAccount, ClientAccount.id == CouponRedemption.client_account_id)
        .join(Order, Order.id == CouponRedemption.order_id)
        .where(CouponRedemption.coupon_id == coupon.id)
    )
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    result = await db.execute(base.order_by(CouponRedemption.created_at.desc()).limit(limit).offset(offset))
    return total or 0, list(result.all())
