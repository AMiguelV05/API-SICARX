"""GET /products/{uuid}/related - niveles (comprados juntos, taxonomia, marca, vehiculos),
sin repetir, sin el propio producto ni su grupo de variantes. Ver related_service."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models.attribute import VariantGroup
from app.models.order import Order
from app.models.taxonomy import Category, product_categories
from app.models.vehicle import Vehicle, product_vehicles
from app.services import related_service
from tests.conftest import make_product


@pytest.fixture(autouse=True)
def _clear_cache():
    related_service.clear_cache()
    yield
    related_service.clear_cache()


def _paid_order(uuids, days_ago=1):
    return Order(
        client_account_id=None, guest_email="x@example.com", sicar_order_id=str(uuid.uuid4()),
        status="PAID", branch_id=1, total=Decimal("1"), total_quantity=Decimal("1"),
        delivery_info={"deliveryType": "PICKUP"},
        items=[{"uuid": u, "quantity": "1"} for u in uuids],
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


async def test_levels_order_and_exclusions(db):
    group = VariantGroup(uuid=str(uuid.uuid4()), name="Grupo", updated_at=datetime.now(timezone.utc))
    db.add(group)
    await db.flush()

    target = make_product(name="Martillo", brand="Truper", variant_group_uuid=group.uuid)
    sibling = make_product(name="Martillo grande", brand="Truper", variant_group_uuid=group.uuid)
    bought = make_product(name="Clavos")
    old_bought = make_product(name="Clavos viejos")
    same_cat_no_stock = make_product(name="Mazo", stock=Decimal("0"))
    same_cat = make_product(name="Marro")
    same_brand = make_product(name="Pinza", brand="TRUPER")
    same_vehicle = make_product(name="Filtro")
    inactive = make_product(name="Inactivo", brand="Truper", is_active=False)
    db.add_all([target, sibling, bought, old_bought, same_cat_no_stock, same_cat, same_brand, same_vehicle, inactive])
    await db.flush()

    category = Category(uuid=str(uuid.uuid4()), name="Golpe", slug=f"golpe-{uuid.uuid4().hex[:6]}", updated_at=datetime.now(timezone.utc))
    vehicle = Vehicle(uuid=str(uuid.uuid4()), vehicle_type="AUTOMOTIVE", make="X", model="Y", year_start=2000, updated_at=datetime.now(timezone.utc))
    db.add_all([category, vehicle])
    await db.flush()
    await db.execute(product_categories.insert(), [
        {"category_uuid": category.uuid, "product_id": p.id} for p in (target, same_cat_no_stock, same_cat, sibling)
    ])
    await db.execute(product_vehicles.insert(), [
        {"vehicle_uuid": vehicle.uuid, "product_id": p.id} for p in (target, same_vehicle)
    ])
    db.add_all([
        _paid_order([target.sicar_uuid, bought.sicar_uuid]),
        _paid_order([target.sicar_uuid, bought.sicar_uuid, same_brand.sicar_uuid]),
        _paid_order([target.sicar_uuid, old_bought.sicar_uuid], days_ago=200),
    ])
    await db.flush()

    related = [p.sicar_uuid for p in await related_service.get_related_products(db, target, 24)]

    assert related[:2] == [bought.sicar_uuid, same_brand.sicar_uuid]  # comprados juntos, por frecuencia
    assert related[2:4] == [same_cat.sicar_uuid, same_cat_no_stock.sicar_uuid]  # con existencia primero
    assert related[4:] == [same_vehicle.sicar_uuid]  # same_brand ya salio, no se repite
    for excluded in (target, sibling, old_bought, inactive):
        assert excluded.sicar_uuid not in related


async def test_endpoint_limit_and_404(api, db):
    target = make_product(brand="Surtek")
    others = [make_product(brand="Surtek") for _ in range(3)]
    db.add_all([target, *others])
    await db.flush()

    response = await api.get(f"/v1/products/{target.sicar_uuid}/related", params={"limit": 2})
    assert response.status_code == 200, response.text
    docs = response.json()["docs"]
    assert len(docs) == 2 and {"sicarUuid", "price", "stock", "compareAtPrice"} <= set(docs[0])

    assert (await api.get("/v1/products/no-existe/related")).status_code == 404
    assert (await api.get(f"/v1/products/{target.sicar_uuid}/related", params={"limit": 25})).status_code == 422
